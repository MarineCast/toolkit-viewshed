"""Producer-owned evidence for working factors; replacement never proves production."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config.distance import load_distance_weight_config
from ..config.paths import get_dem_settings, resolve_existing_or_relative_path
from .artifacts import final_artifact_paths_from_raw
from .components import fingerprint, input_checksums, validate_pairs, write_json

PRODUCER_CONTRACT = "viewshed_factor_producer_v1"
REBUILD_STAGES = {
    "distance": "build-distance-weights",
    "terrain": "terrain-weight",
    "clear_sky": "terrain-weight",
    "canopy": "build-dual-surface-canopy-weights",
    "vegetation": "build-vegetation-path-weights",
}


def producer_receipt_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".producer.json")


def producer_revision() -> str | None:
    root = Path(__file__).resolve().parents[4]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def factor_contract(
    raw: Mapping[str, Any], config_dir: Path, role: str, kind: str
) -> dict[str, Any]:
    """Dependency-scoped effective settings and current content identities."""
    if role not in {"land", "water"} or kind not in REBUILD_STAGES:
        raise ValueError("Invalid producer role/product")
    from ..config.loader import SOURCE_SAMPLING_ALGORITHM_VERSION, H3Config, ViewshedSettings
    from ..config.paths import (
        DEFAULT_DEM_PATH_TEMPLATE,
        DEFAULT_LAND_H3_PATH_TEMPLATE,
        DEFAULT_LAND_POLYGON_RELATIVE,
        DEFAULT_WATER_POLYGON_RELATIVE,
    )
    from .generation import byte_checksum

    paths = final_artifact_paths_from_raw(dict(raw), config_dir)
    inputs = {"lookup": paths.source_target_lookup}
    dem = {
        **asdict(ViewshedSettings()),
        **{key: value for key, value in get_dem_settings(dict(raw)).items() if value is not None},
        **dict(raw.get("viewshed", {})),
    }
    h3_settings = {**asdict(H3Config()), **dict(raw.get("h3", {}))}
    h3_settings["target_resolution"] = (
        h3_settings.get("target_resolution") or h3_settings["output_resolution"]
    )
    if "target_resolution" not in raw.get("h3", {}) and "output_resolution" not in raw.get(
        "h3", {}
    ):
        h3_settings["target_resolution"] = h3_settings["source_resolution"]
    h3_settings["output_resolution"] = h3_settings["target_resolution"]
    if h3_settings["aggregation_mode"] == "full":
        h3_settings["pixel_stride"] = 1
    settings: dict[str, Any] = {
        "h3": (
            {key: h3_settings[key] for key in ("source_resolution", "target_resolution")}
            if kind == "distance"
            else h3_settings
        ),
        "source_target_lookup": dict(raw.get("source_target_lookup", {})),
    }
    distance = asdict(load_distance_weight_config(dict(raw)))
    for key in ("overwrite", "source_chunk_size", "compute_exact_p90", "p90_method", "version"):
        distance.pop(key, None)
    settings["distance"] = distance
    settings["distance_extent_km"] = (
        distance.get("hard_cutoff_km") or float(dem.get("max_distance_m") or 30000) / 1000
    )
    if kind != "distance":
        settings.update(
            viewshed=dem,
            region=dict(raw.get("region", {})),
            target_denominator="complete_h3_water_intersection_v1",
            matched_population="same_role_lookup_and_deterministic_observer_water_design_v1",
            source_sampling_algorithm=SOURCE_SAMPLING_ALGORITHM_VERSION,
        )
        configured = raw.get("paths", {})
        names = ["land_polygon_path", "water_polygon_path"]
        if role == "land":
            names += ["land_h3_path", "source_cells_path", "regional_dem_path"]
            if kind in {"canopy", "vegetation"}:
                names += ["canopy_height_path"]
                settings["canopy_visibility"] = dict(raw.get("canopy_visibility", {}))
                settings["vegetation_weights"] = dict(raw.get("vegetation_weights", {}))
            else:
                settings["viewshed"] = {
                    key: value
                    for key, value in dem.items()
                    if "canopy" not in key
                    and key not in {"minimum_canopy_height_m", "surface_model"}
                }
        else:
            settings["water_viewing"] = dict(raw.get("water_viewing", {}))
            settings["viewshed"] = {
                key: value
                for key, value in dem.items()
                if key
                in {"max_distance_m", "curvature_coefficient", "earth_radius_m", "crs_projected"}
            }
        defaults: dict[str, str | Path] = {
            "land_polygon_path": DEFAULT_LAND_POLYGON_RELATIVE,
            "water_polygon_path": DEFAULT_WATER_POLYGON_RELATIVE,
            "regional_dem_path": DEFAULT_DEM_PATH_TEMPLATE.format(
                resolution_m=dem["dem_resolution_m"]
            ),
            "land_h3_path": DEFAULT_LAND_H3_PATH_TEMPLATE.format(
                resolution=h3_settings["source_resolution"]
            ),
            "source_cells_path": DEFAULT_LAND_H3_PATH_TEMPLATE.format(
                resolution=h3_settings["source_resolution"]
            ),
        }
        defaults["canopy_height_path"] = resolve_existing_or_relative_path(
            configured.get("regional_dem_path", defaults["regional_dem_path"]), config_dir
        ).with_name(f"CHM_{dem['dem_resolution_m']}M.tif")
        for name in names:
            inputs[name] = resolve_existing_or_relative_path(
                configured.get(name, defaults[name]), config_dir
            )
    identities = input_checksums(inputs)
    grids = {}
    for name, path in inputs.items():
        if path.suffix.lower() in {".tif", ".tiff"}:
            import rasterio

            with rasterio.open(path) as raster:
                grids[name] = {
                    "crs": str(raster.crs),
                    "affine": list(raster.transform)[:6],
                    "shape": [raster.height, raster.width],
                    "nodata": raster.nodata,
                }
    return {
        "contract": PRODUCER_CONTRACT,
        "method": "direct_unweighted_los_v9",
        "source_type": role,
        "product": kind,
        "settings": settings,
        "inputs": identities,
        "input_byte_sha256": {name: byte_checksum(path) for name, path in inputs.items()},
        "grids": grids,
    }


def record_factor(
    path: Path, contract: Mapping[str, Any], *, dependencies: Mapping[str, Path] | None = None
) -> dict[str, Any]:
    """Call only after a producer has validated successful execution and its product."""
    import polars as pl

    pairs = pl.read_parquet(path).select("source_h3", "target_h3").sort("source_h3", "target_h3")
    validate_pairs(pairs)
    payload = {
        **contract,
        "fingerprint": fingerprint(contract),
        "artifact": path.name,
        "output_checksum": input_checksums({"product": path})["product"],
        "producer_revision": producer_revision(),
        "dependencies": input_checksums(dependencies or {}),
        "evaluated_pair_count": pairs.height,
        "evaluated_pair_identity": fingerprint({"pairs": pairs.rows()}),
    }
    payload["receipt_id"] = fingerprint(payload)
    write_json(producer_receipt_path(path), payload)
    return payload


def validate_factor(path: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    import json

    stage = REBUILD_STAGES[str(contract["product"])]
    try:
        record = json.loads(producer_receipt_path(path).read_text())
        sealed = {key: value for key, value in record.items() if key != "receipt_id"}
        if record.get("receipt_id") != fingerprint(sealed):
            raise ValueError("receipt integrity mismatch")
        for key, value in contract.items():
            if record.get(key) != value:
                raise ValueError(f"{key} mismatch")
        if record.get("fingerprint") != fingerprint(contract):
            raise ValueError("identity mismatch")
        if (
            record.get("artifact") != path.name
            or record.get("output_checksum") != input_checksums({"product": path})["product"]
        ):
            raise ValueError("artifact checksum mismatch")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(
            f"Stale or missing producer lineage for {path.name}: {exc}; rebuild {stage}"
        ) from exc
    return dict(record)


def validate_role_factors(
    raw: Mapping[str, Any], config_dir: Path, role: str, *, geometry: bool = True
) -> dict[str, Any]:
    paths = final_artifact_paths_from_raw(dict(raw), config_dir)
    factors = {
        "terrain": (paths.weights_path("terrain_weights", source_type=role), "terrain"),
        "distance": (paths.weights_path("distance_weights", source_type=role), "distance"),
        "vegetation": (
            paths.weights_path("vegetation_weights", source_type=role),
            "canopy" if role == "land" else "vegetation",
        ),
    }
    if geometry:
        factors["clear_sky"] = (
            (
                paths.source_target_clear_sky
                if role == "land"
                else paths.ocean_source_target_clear_sky
            ),
            "clear_sky",
        )
        if role == "land":
            factors["dual_surface"] = (paths.dual_surface_factors, "canopy")
    result = {}
    contracts = {}
    for name, (path, kind) in factors.items():
        if kind not in contracts:
            try:
                contracts[kind] = factor_contract(raw, config_dir, role, kind)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Producer lineage inputs unavailable for {path.name}: {exc}; rebuild {REBUILD_STAGES[kind]}"
                ) from exc
        result[name] = validate_factor(path, contracts[kind])
    return result
