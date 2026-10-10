"""Portable MarineCast study v1 adapter; loading and planning are read-only."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from jsonschema import Draft202012Validator, FormatChecker

_P = ParamSpec("_P")
_R = TypeVar("_R")

_SELECTED_STUDY: ContextVar[str | Path | None] = ContextVar("viewshed_study", default=None)
SOURCE_POLICY = "land_and_water_sources_within_reporting_rectangle_plus_los_distance"
RASTER_POLICY = "cover_source_target_paths_plus_aoi_margin"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate study JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Nonfinite study JSON value: {value}")


def selected_study_path(explicit: str | Path | None = None) -> Path | None:
    value = explicit if explicit is not None else _SELECTED_STUDY.get()
    if value is None:
        value = os.environ.get("MARINECAST_STUDY_CONFIG")
    if value is None:
        return None
    if not str(value).strip():
        raise ValueError("Study config path cannot be empty")
    return Path(value).expanduser().resolve()


@contextmanager
def study_selection(path: str | Path | None) -> Iterator[None]:
    """Scope an explicit CLI/request selection without changing process environment."""
    if path is None:
        yield
        return
    token = _SELECTED_STUDY.set(path)
    try:
        yield
    finally:
        _SELECTED_STUDY.reset(token)


@dataclass(frozen=True)
class StudyConfig:
    path: Path
    config: dict[str, Any]
    config_sha256: str
    geometry_sha256: str
    raw_file_sha256: str
    data_root: Path

    def provenance(self) -> dict[str, Any]:
        return {
            "study_config": copy.deepcopy(self.config),
            "schema_version": self.config["schema_version"],
            "study_id": self.config["study_id"],
            "domain_revision": self.config["domain"]["revision"],
            "domain_status": self.config["domain"]["status"],
            "domain_approval": copy.deepcopy(self.config["domain"].get("approval")),
            "domain_revision_note": self.config["domain"].get("revision_note"),
            "config_sha256": self.config_sha256,
            "geometry_sha256": self.geometry_sha256,
            "raw_file_sha256": self.raw_file_sha256,
            "requested_time": copy.deepcopy(self.config["time"]),
            "producer_buffers": copy.deepcopy(self.config["producer_buffers"]["viewshed"]),
            "reporting_bbox_wgs84": list(self.config["domain"]["bbox_wgs84"]),
            "grid_registry": copy.deepcopy(self.config["grid_registry"]),
            "static_time_policy": "requested_window_is_not_source_vintage_or_daily_coverage",
            "land_source_eligibility": "positive_mapped_land_area_in_source_extent; legacy_coastal_filter_not_applied",
        }


def load_study_config(path: str | Path | None = None, *, planning: bool = False) -> StudyConfig:
    selected = selected_study_path(path)
    if selected is None:
        raise ValueError("Supply --study-config PATH or MARINECAST_STUDY_CONFIG")
    content = selected.read_bytes()
    config = json.loads(
        content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    schema = json.loads(
        files("viewshed_toolkit.resources").joinpath("study.schema.json").read_text("utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(config),
        key=lambda error: str(error.json_path),
    )
    if errors:
        raise ValueError(f"Invalid study config at {errors[0].json_path}: {errors[0].message}")
    required_coastal_fields = {"bbox_role", "geometry_status", "selection_policy"}
    if not required_coastal_fields.issubset(config["domain"]):
        raise ValueError(
            "Incomplete coastal study contract: envelope, geometry and policy required"
        )
    west, south, east, north = config["domain"]["bbox_wgs84"]
    if not all(math.isfinite(v) for v in (west, south, east, north)) or not (
        -180 <= west < east <= 180 and -90 < south < north < 90
    ):
        raise ValueError("Invalid non-antimeridian study rectangle")
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }
    identity = {
        "crs": config["domain"]["crs"],
        "boundary_semantics": config["domain"]["boundary_semantics"],
        "geometry": geometry,
    }
    geometry_hash = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    if geometry_hash != config["domain"]["geometry_sha256"]:
        raise ValueError("Study geometry_sha256 mismatch")
    if config["domain"]["status"] == "approved" and not config["domain"].get("approval"):
        raise ValueError("Approved study requires explicit approval provenance")
    policy = config["domain"]["selection_policy"]
    if policy["status"] == "approved" and not policy["approval"]:
        raise ValueError("Approved selection policy requires approval provenance")
    if date.fromisoformat(config["time"]["start"]) >= date.fromisoformat(
        config["time"]["end_exclusive"]
    ):
        raise ValueError("Study time interval must have start < end_exclusive")
    root = Path(config["storage"]["data_root"])
    if root.is_absolute():
        raise ValueError("Study data_root must be relative to the config file")
    registry = config["grid_registry"]
    if registry["status"] == "validated" and not all(
        registry[key] for key in ("mask_revision", "mask_sha256", "memberships")
    ):
        raise ValueError("Validated study registry requires pinned mask and memberships")
    roles = [(entry["resolution"], entry["role"]) for entry in registry["memberships"]]
    if len(roles) != len(set(roles)):
        raise ValueError("Duplicate study registry resolution/role")
    buffers = config["producer_buffers"]["viewshed"]
    if (
        buffers["source_policy"] != SOURCE_POLICY
        or buffers["native_raster_policy"] != RASTER_POLICY
    ):
        raise ValueError("Unsupported Viewshed study source/raster policy")
    if not planning and config["domain"]["status"] != "approved":
        raise ValueError("Study domain remains proposed; production requires approved geometry")
    if not planning and registry["status"] != "validated":
        raise ValueError("Study production requires a validated marine mask and H3 registry")
    if not planning and (
        policy["status"] != "approved"
        or policy["mask_status"] != "source_relative_validated"
        or config["domain"]["geometry_status"] != "source_relative_validated"
    ):
        raise ValueError("Study production requires validated coastal mask and geometry")
    return StudyConfig(
        selected,
        config,
        hashlib.sha256(canonical_bytes(config)).hexdigest(),
        geometry_hash,
        hashlib.sha256(content).hexdigest(),
        (selected.parent / root).resolve(),
    )


def support_polygons(
    bbox: list[float] | tuple[float, ...], crs: str, los_m: float, margin_m: float
) -> tuple[Any, Any, Any]:
    """Reporting rectangle, eligible source extent, and minimum native path envelope.

    Densification preserves constant-latitude/longitude edges when projecting;
    shared geometry identity remains the exact five-vertex semantic rectangle.
    """
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import Polygon, box

    west, south, east, north = bbox
    corners = [(west, south), (east, south), (east, north), (west, north), (west, south)]
    ring: list[tuple[float, float]] = []
    for start, end in pairwise(corners):
        steps = max(1, math.ceil(max(abs(end[0] - start[0]), abs(end[1] - start[1])) / 0.02))
        ring.extend(
            (start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1]))
            for t in np.linspace(0, 1, steps, endpoint=False)
        )
    projected = gpd.GeoSeries([Polygon(ring)], crs="EPSG:4326").to_crs(crs).iloc[0]
    source = projected.buffer(los_m)
    native = projected.buffer(los_m + margin_m)
    source_wgs, native_wgs = gpd.GeoSeries([source, native], crs=crs).to_crs(4326)
    return box(*bbox), source_wgs, native_wgs


def planning_report(study: StudyConfig, *, crs: str = "EPSG:32610") -> dict[str, Any]:
    buffers = study.config["producer_buffers"]["viewshed"]
    reporting, source, native = support_polygons(
        study.config["domain"]["bbox_wgs84"],
        crs,
        buffers["line_of_sight_m"],
        buffers["aoi_margin_m"],
    )
    return {
        **study.provenance(),
        "resolved_data_root": str(study.data_root),
        "reporting_target_bbox_wgs84": (
            None if study.config["domain"].get("selection_policy") else list(reporting.bounds)
        ),
        "acquisition_planning_envelope_bbox_wgs84": list(reporting.bounds),
        "observer_source_envelope_kind": (
            "conservative_planning_bbox_plus_los"
            if study.config["domain"].get("selection_policy")
            else "rectangular_reporting_support_plus_los"
        ),
        "observer_source_envelope_wgs84": list(source.bounds),
        "native_path_envelope_wgs84": list(native.bounds),
        "producer_projected_crs": crs,
        "projected_boundary_max_step_degrees": 0.02,
        "planning_only": True,
        "bbox_role": study.config["domain"].get("bbox_role", "reporting_rectangle"),
        "reporting_selection_policy": copy.deepcopy(study.config["domain"].get("selection_policy")),
        "reporting_geometry_status": study.config["domain"].get(
            "geometry_status", "rectangle_only"
        ),
        "source_coverage": "not_established_by_requested_time_or_geometry",
    }


def adapt_raw_config(raw: dict[str, Any], study: StudyConfig) -> dict[str, Any]:
    """Apply validated shared selection to a normalized standalone config."""
    adapted = copy.deepcopy(raw)
    resolution = study.config["products"]["viewshed"]["h3_resolution"]
    h3 = adapted.setdefault("h3", {})
    for key in ("source_resolution", "target_resolution", "output_resolution"):
        if key in h3 and h3[key] != resolution:
            raise ValueError(f"Viewshed {key} conflicts with shared study R{resolution}")
        h3[key] = resolution
    viewshed = adapted.setdefault("viewshed", {})
    buffers = study.config["producer_buffers"]["viewshed"]
    for key, common_key in (
        ("max_distance_m", "line_of_sight_m"),
        ("aoi_margin_m", "aoi_margin_m"),
    ):
        if key in viewshed and float(viewshed[key]) != float(buffers[common_key]):
            raise ValueError(f"Viewshed {key} conflicts with shared producer buffer")
        viewshed[key] = buffers[common_key]
    lookup = adapted.setdefault("source_target_lookup", {})
    for key in (
        "include_land_sources",
        "include_water_sources",
        "include_mixed_as_land_sources",
        "include_mixed_as_water_sources",
        "include_water_targets",
        "include_mixed_as_water_targets",
    ):
        if lookup.get(key, True) is not True:
            raise ValueError(f"Shared study requires {key}; land/water support cannot be discarded")
        lookup[key] = True
    lookup["min_water_fraction_for_target"] = math.nextafter(0.0, 1.0)
    lookup["min_land_fraction_for_source"] = math.nextafter(0.0, 1.0)
    for key in ("max_distance_km_land", "max_distance_km_water"):
        if key in lookup and float(lookup[key]) != float(buffers["line_of_sight_m"]) / 1000:
            raise ValueError(f"Viewshed {key} conflicts with shared LOS buffer")
        lookup[key] = float(buffers["line_of_sight_m"]) / 1000
    adapted.pop("area", None)
    adapted["region"] = {
        **adapted.get("region", {}),
        "min_source_cell_land_fraction": math.nextafter(0.0, 1.0),
        "max_source_cell_water_fraction": None,
        "name": study.config["study_id"],
        "bbox_wgs84": dict(
            zip(
                ("min_lon", "min_lat", "max_lon", "max_lat"),
                study.config["domain"]["bbox_wgs84"],
                strict=True,
            )
        ),
    }
    adapted["marinecast_study"] = study.provenance()
    adapted["marinecast_study"].update(
        resolved_data_root=str(study.data_root), study_config_directory=str(study.path.parent)
    )
    paths = adapted.setdefault("paths", {})
    for key in (
        "water_polygon_path",
        "land_polygon_path",
        "regional_dem_path",
        "canopy_height_path",
    ):
        if key not in paths:
            raise ValueError(f"Shared mode requires explicit paths.{key}; no input discovery")
        value = Path(paths[key]).expanduser()
        if not value.is_absolute():
            parts = value.parts[1:] if value.parts and value.parts[0] == "data" else value.parts
            value = study.data_root.joinpath(*parts)
        paths[key] = str(value.resolve())
    if "reporting_water_polygon_path" in paths:
        value = Path(paths["reporting_water_polygon_path"]).expanduser()
        if not value.is_absolute():
            value = study.data_root / value
        paths["reporting_water_polygon_path"] = str(value.resolve())
    for dataset in adapted.get("datasets", {}).values():
        if dataset.get("provider") == "local":
            assets = []
            for asset in dataset.get("assets", []):
                value = Path(asset).expanduser()
                if not value.is_absolute():
                    parts = (
                        value.parts[1:] if value.parts and value.parts[0] == "data" else value.parts
                    )
                    value = study.data_root.joinpath(*parts)
                assets.append(str(value.resolve()))
            dataset["assets"] = assets
    work = study.data_root / "viewshed" / "work" / study.config_sha256
    final = study.data_root / "viewshed" / "generations" / study.config_sha256
    for key, value in {
        "output_dir": work,
        "raw_dem_dir": work / "inputs" / "dem",
        "final_output_dir": final,
        "map_dir": final / "maps",
        "land_h3_path": work / "inputs" / "land_h3.parquet",
        "source_cells_path": work / "inputs" / "land_h3.parquet",
        "projected_dem_path": work / "cache" / "projected_dem.tif",
        "final_visibility_path": work / "terrain" / "terrain_visibility.parquet",
        "partitioned_visibility_dir": work / "terrain" / "partitions",
        "manifest_path": work / "terrain" / "manifest.csv",
    }.items():
        paths[key] = str(value)
    return adapted


def domain_polygons_from_raw(raw: dict[str, Any], crs: str) -> tuple[Any, Any, Any] | None:
    """Return shared reporting/source/native support; standalone callers get None."""
    study = raw.get("marinecast_study")
    if study is None:
        return None
    from .reporting import load_reporting_support

    support = load_reporting_support(raw, crs)
    return support.reporting_water, support.source_extent, support.native_extent


def with_study_config(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Preserve explicit AppConfig selection through APIs that reload stage YAML."""
    from functools import wraps

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        config = args[0] if args else kwargs.get("config")
        validate_study_app(config)
        selected = getattr(config, "study_config_path", None)
        if selected is None:
            return function(*args, **kwargs)
        with study_selection(selected):
            return function(*args, **kwargs)

    return wrapped


def require_shared_owned_output(raw: dict[str, Any], path: Path) -> None:
    """Shared preparation cannot overwrite a configured external reusable input."""
    if "marinecast_study" not in raw:
        return
    root = Path(raw["paths"]["output_dir"]).parents[1]
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Shared preparation output must stay under {root}: {path}")


def reject_shared_legacy_acquisition() -> None:
    """Legacy acquisition does not implement the shared path/halo contract."""
    if selected_study_path() is not None:
        load_study_config()
        raise ValueError(
            "Shared studies use explicit component download/prepare stages; "
            "legacy acquisition does not implement the shared input halo/ownership contract"
        )


def validate_study_app(app: Any) -> None:
    """Reject changed study selections before executing an already loaded app."""
    selected = getattr(app, "study_config_path", None)
    if selected is not None:
        current = load_study_config(selected)
        recorded = app.raw_config["marinecast_study"]["config_sha256"]
        if current.config_sha256 != recorded:
            raise ValueError("Study config changed after AppConfig was loaded; reload explicitly")
    elif hasattr(app, "raw_config") and selected_study_path() is not None:
        load_study_config()
        raise ValueError("Study selected for a standalone AppConfig; reload explicitly")


def validate_shared_mask(raw: dict[str, Any], path: Path) -> None:
    """Bind reporting mask bytes to the registry's raw SHA-256 identity."""
    study = raw.get("marinecast_study")
    if study is None:
        return
    expected = study["grid_registry"]["mask_sha256"]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("Configured marine water geometry does not match shared mask_sha256")
