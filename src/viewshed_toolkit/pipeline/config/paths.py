"""Path, artifact-name, and metadata helpers for viewshed runs.

Scientific and operational role
-------------------------------
The viewshed pipeline depends on several artifacts that must agree on the same
scientific run context: bbox, H3 resolution, DEM resolution, observer/target
heights, distance model, and run version. This module centralizes the small,
stable helpers that turn YAML configuration into reproducible paths, names, and
metadata sidecars.

This file intentionally contains no terrain, distance, or vegetation modeling
logic. Its job is to make every stage describe and validate the context in which
an artifact was created, so stale or mismatched products are easier to detect
before they create silent scientific errors.

Key responsibilities
--------------------
- Resolve paths relative to the viewshed folder, repository root, or current
  working directory.
- Build versioned artifact names for terrain and vegetation path outputs.
- Write and read lightweight metadata sidecars.
- Validate metadata compatibility for finalization and reuse checks.
- Provide small shared utilities such as config hashing, memory logging, and
  DEM/land-H3 path derivation.

"""

from __future__ import annotations

import json
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.config.data import load_data_config
from viewshed_toolkit._internal.config.paths import project_root, resolve_config_path
from viewshed_toolkit.resources import default_config_path

REPO_ROOT = project_root()
VIEWSHED_ROOT = REPO_ROOT
VIEWSHED_DOMAIN_ROOT_RELATIVE = Path("data/processed/domain/human/viewshed")
VIEWSHED_DATA_ROOT = REPO_ROOT / VIEWSHED_DOMAIN_ROOT_RELATIVE
LANDSCAPE_DOMAIN_ROOT_RELATIVE = Path("data/processed/domain/environmental_layer/landscape")
LANDSCAPE_DEM_ROOT_RELATIVE = LANDSCAPE_DOMAIN_ROOT_RELATIVE / "DEM"
LANDSCAPE_CHM_ROOT_RELATIVE = LANDSCAPE_DOMAIN_ROOT_RELATIVE / "CHM"
LANDSCAPE_LAND_COVER_ROOT_RELATIVE = LANDSCAPE_DOMAIN_ROOT_RELATIVE / "LAND_COVER"
DEFAULT_CONFIG = default_config_path()
DEFAULT_WATER_POLYGON_RELATIVE = (
    "data/processed/domain/environmental_layer/seascape/spatial_support/"
    "water_geometry/TERRITORIAL_WATER_POLYGON.parquet"
)
DEFAULT_LAND_POLYGON_RELATIVE = "data/raw/gis/land/ne_10m_land.shp"
DEFAULT_RAW_DEM_DIR_RELATIVE = "data/tmp/elevation"
DEFAULT_DEM_PATH_TEMPLATE = (LANDSCAPE_DEM_ROOT_RELATIVE / "DEM_{resolution_m}M.tif").as_posix()
DEFAULT_LAND_H3_PATH_TEMPLATE = "data/processed/gis/LAND_AREA_H3R{resolution}.parquet"

DEFAULT_CANOPY_CONFIG = {
    "provider": "eth_global_canopy_height_2020",
    "product_year": 2020,
    "resampling": "max",
    "raw_dir": "data/raw/canopy/eth_10m",
    "processed_dir": LANDSCAPE_CHM_ROOT_RELATIVE.as_posix(),
    "final_dir": LANDSCAPE_CHM_ROOT_RELATIVE.as_posix(),
    "tile_manifest_path": "data/cache/chm_tile_manifest.json",
}
DEFAULT_LANDCOVER_CONFIG = {
    "temp_dir": "data/_tmp/landcover",
    "raw_dir": "data/raw/landcover/esa_worldcover_v200",
    "final_dir": LANDSCAPE_LAND_COVER_ROOT_RELATIVE.as_posix(),
    "output_path": "data/_tmp/landcover/esa_worldcover_v200_landcover_clip.tif",
    "metadata_path": (LANDSCAPE_LAND_COVER_ROOT_RELATIVE / "LAND_COVER_METADATA.json").as_posix(),
    "cache_dir": "data/cache",
    "tile_manifest_path": "data/cache/landcover_tile_manifest.json",
}
DEFAULT_VEGETATION_CLASS_LOOKUP_PATH = (
    LANDSCAPE_LAND_COVER_ROOT_RELATIVE / "LAND_COVER_CLASS_LOOKUP.csv"
).as_posix()
DEFAULT_VEGETATION_WEIGHT_INPUTS = {
    "dem": DEFAULT_DEM_PATH_TEMPLATE,
    "chm": (LANDSCAPE_CHM_ROOT_RELATIVE / "CHM_{resolution_m}M.tif").as_posix(),
    "chm_obstruction": (
        LANDSCAPE_CHM_ROOT_RELATIVE / "CHM_OBSTRUCTION_{resolution_m}M.tif"
    ).as_posix(),
    "landcover": (LANDSCAPE_LAND_COVER_ROOT_RELATIVE / "LAND_COVER_{resolution_m}M.tif").as_posix(),
    "landcover_lookup": DEFAULT_VEGETATION_CLASS_LOOKUP_PATH,
}


def resolve_path(value: str | Path, base_dir: Path = VIEWSHED_ROOT) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    # Historical domain includes used ../data/... relative to config/data even
    # though they meant repository-root data/.... Normalize that syntax
    # explicitly so resolution never depends on config nesting or cwd.
    if (
        len(path.parts) >= 2
        and path.parts[0] == ".."
        and path.parts[1]
        in {
            "config",
            "data",
            "notebooks",
            "outputs",
            "src",
        }
    ):
        return (REPO_ROOT / Path(*path.parts[1:])).resolve()
    if path.parts and path.parts[0] in {
        "config",
        "data",
        "notebooks",
        "analysis",
        "outputs",
        "src",
    }:
        return (REPO_ROOT / path).resolve()
    return (base_dir / path).resolve()


def viewshed_domain_relative(*parts: str | Path) -> str:
    """Return a repo-relative path under the canonical viewshed domain root."""

    return VIEWSHED_DOMAIN_ROOT_RELATIVE.joinpath(*map(Path, parts)).as_posix()


DEFAULT_VEGETATION_PATH_INPUTS = {
    "clear_sky_pairs": None,
    "source_cells": "data/processed/gis/LAND_AREA.parquet",
    "water_cells": None,
    "source_cell_weights": None,
    "water_polygon": DEFAULT_WATER_POLYGON_RELATIVE,
    "surface_transmission": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "SURFACE_VISIBILITY_TRANSMISSION_{resolution_m}M.tif",
    ),
    "surface_obstruction": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "SURFACE_OBSTRUCTION_{resolution_m}M.tif",
    ),
    "chm_obstruction": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "CHM_OBSTRUCTION_{resolution_m}M.tif",
    ),
    "landcover": (LANDSCAPE_LAND_COVER_ROOT_RELATIVE / "LAND_COVER_{resolution_m}M.tif").as_posix(),
}
DEFAULT_VEGETATION_WEIGHT_OUTPUTS = {
    "class_weights_csv": viewshed_domain_relative(
        "land", "vegetation", "support", "LAND_COVER_CLASS_WEIGHTS.csv"
    ),
    "lc_obstruction_multiplier_raster": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "LAND_COVER_OBSTRUCTION_MULTIPLIER_{resolution_m}M.tif",
    ),
    "lc_obstruction_floor_raster": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "LAND_COVER_OBSTRUCTION_FLOOR_{resolution_m}M.tif",
    ),
    "lc_source_access_weight_raster": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "LAND_COVER_SOURCE_ACCESS_WEIGHT_{resolution_m}M.tif",
    ),
    "chm_obstruction_raster": viewshed_domain_relative(
        "land", "vegetation", "support", "CHM_OBSTRUCTION_{resolution_m}M.tif"
    ),
    "surface_obstruction_raster": viewshed_domain_relative(
        "land", "vegetation", "support", "SURFACE_OBSTRUCTION_{resolution_m}M.tif"
    ),
    "surface_transmission_raster": viewshed_domain_relative(
        "land",
        "vegetation",
        "support",
        "SURFACE_VISIBILITY_TRANSMISSION_{resolution_m}M.tif",
    ),
    "h3_cell_weights_parquet": viewshed_domain_relative(
        "land",
        "vegetation",
        "LAND_SOURCE_CELL_VEGETATION_WEIGHTS_{resolution_m}M_R{h3_resolution}.parquet",
    ),
    "h3_cell_weights_csv": None,
}
DEFAULT_VEGETATION_PATH_OUTPUTS = {
    "candidate_pairs": viewshed_domain_relative(
        "land",
        "vegetation_path_weights",
        "{version}",
        "VEGETATION_CANDIDATE_PAIRS.parquet",
    ),
    "pair_chunks": viewshed_domain_relative(
        "land", "vegetation_path_weights", "{version}", "pair_chunks"
    ),
    "target_summary": viewshed_domain_relative(
        "land",
        "vegetation_path_weights",
        "{version}",
        "target_vegetation_summary.parquet",
    ),
    "source_summary": viewshed_domain_relative(
        "land",
        "vegetation_path_weights",
        "{version}",
        "source_vegetation_summary.parquet",
    ),
    "manifest": viewshed_domain_relative(
        "land",
        "vegetation_path_weights",
        "{version}",
        "vegetation_path_weight_manifest_{version}.csv",
    ),
    "debug_rays": None,
}


def viewshed_domain_path(*parts: str | Path, config_dir: Path = VIEWSHED_ROOT) -> Path:
    """Resolve a path under the canonical processed viewshed domain root."""

    return resolve_path(viewshed_domain_relative(*parts), config_dir)


def resolve_existing_or_relative_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()

    canonical = resolve_path(path, base_dir)
    candidates = [canonical, (base_dir / path).resolve(), (REPO_ROOT / path).resolve()]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    aliases = {
        "environment": "environmental_layer",
        "human": "human_layer",
    }
    for candidate in candidates:
        parts = list(candidate.parts)
        try:
            domain_index = parts.index("domain")
        except ValueError:
            continue
        if domain_index + 1 >= len(parts):
            continue
        legacy_name = aliases.get(parts[domain_index + 1])
        if legacy_name is None:
            continue
        legacy_parts = list(parts)
        legacy_parts[domain_index + 1] = legacy_name
        legacy = Path(*legacy_parts)
        if legacy.exists():
            warnings.warn(
                "Using a declared legacy domain-path compatibility alias: "
                f"{candidate} -> {legacy}. Migrate the artifact to the canonical "
                "domain/environment or domain/human tree.",
                DeprecationWarning,
                stacklevel=2,
            )
            return legacy.resolve()
    return canonical


def seascape_water_polygon_path(config_path: str | Path = DEFAULT_CONFIG) -> Path:
    raw = load_data_config(resolve_config_path(config_path), domains="SEASCAPE_LAYER")
    water_geometry = raw.get("water_geometry", {}) or {}
    if not isinstance(water_geometry, dict):
        raise ValueError("Config section 'water_geometry' must be a mapping.")
    build_cfg = water_geometry.get("build", water_geometry.get("polygons", {})) or {}
    if not isinstance(build_cfg, dict):
        raise ValueError("Config section 'water_geometry.build' must be a mapping.")
    processed_out_dir = water_geometry.get(
        "processed_out_dir",
        raw.get(
            "water_polygon_processed_out_dir", str(Path(DEFAULT_WATER_POLYGON_RELATIVE).parent)
        ),
    )
    output_filename = build_cfg.get(
        "output_filename",
        Path(DEFAULT_WATER_POLYGON_RELATIVE).name,
    )
    return resolve_existing_or_relative_path(
        Path(str(processed_out_dir)) / str(output_filename), REPO_ROOT
    )


def stable_config_hash(config: dict[str, Any], length: int = 8) -> str:
    text = json.dumps(config, sort_keys=True, default=str, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()[: int(length)]


def get_run_version(config: dict[str, Any]) -> str:
    version = config.get("run", {}).get("version")
    return str(version) if version not in {None, ""} else "unversioned"


def get_h3_settings(config: dict[str, Any]) -> dict[str, Any]:
    h3 = config.get("h3", {}) or {}
    target_resolution = h3.get("target_resolution", h3.get("output_resolution"))
    return {
        "source_resolution": h3.get("source_resolution"),
        "target_resolution": target_resolution,
        "output_resolution": target_resolution,
        "source_sampling_mode": h3.get("source_sampling_mode", "fixed"),
        "sample_points_per_source_cell": h3.get("sample_points_per_source_cell"),
        "min_sample_points_per_source_cell": h3.get("min_sample_points_per_source_cell", 1),
        "pixel_stride": h3.get("pixel_stride"),
        "aggregation_mode": h3.get("aggregation_mode", "full"),
    }


def get_dem_settings(config: dict[str, Any]) -> dict[str, Any]:
    viewshed = config.get("viewshed", {}) or {}
    return {
        "dem_resolution_m": viewshed.get("dem_resolution_m"),
        "observer_height_m": viewshed.get(
            "observer_height_m", viewshed.get("observer_eye_height_m")
        ),
        "target_height_m": viewshed.get("target_height_m"),
        "max_distance_m": viewshed.get("max_distance_m"),
        "backend": viewshed.get("backend", "gdal"),
        "surface_model": viewshed.get("surface_model", "bare_earth"),
        "dem_nodata_policy": viewshed.get("dem_nodata_policy", "error"),
        "dem_nodata_barrier_height_m": viewshed.get("dem_nodata_barrier_height_m", 100_000.0),
        "canopy_resampling": viewshed.get("canopy_resampling", "max"),
        "canopy_nodata_policy": viewshed.get("canopy_nodata_policy", "error"),
        "minimum_canopy_height_m": viewshed.get("minimum_canopy_height_m", 0.0),
        "observer_canopy_clearance_radius_m": viewshed.get(
            "observer_canopy_clearance_radius_m", 0.0
        ),
        "curvature_coefficient": viewshed.get("curvature_coefficient", 0.85714),
        "earth_radius_m": viewshed.get("earth_radius_m", 6_378_137.0),
    }


def bbox_from_config(
    raw: dict[str, Any], buffer_deg: float = 0.0
) -> tuple[float, float, float, float]:
    bbox = raw.get("region", {}).get("bbox_wgs84")
    if not bbox:
        raise ValueError("Missing required config field: region.bbox_wgs84")
    min_lon = float(bbox["min_lon"]) - float(buffer_deg)
    min_lat = float(bbox["min_lat"]) - float(buffer_deg)
    max_lon = float(bbox["max_lon"]) + float(buffer_deg)
    max_lat = float(bbox["max_lat"]) + float(buffer_deg)
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ValueError(f"Invalid buffered bbox: {(min_lon, min_lat, max_lon, max_lat)}")
    return (min_lon, min_lat, max_lon, max_lat)


def versioned_viewshed_name(config: dict[str, Any]) -> str:
    h3 = get_h3_settings(config)
    dem = get_dem_settings(config)
    run_version = get_run_version(config)
    source_resolution = int(h3.get("source_resolution") or 6)
    target_resolution = int(h3.get("target_resolution") or source_resolution)
    sampling_mode = str(h3.get("source_sampling_mode") or "fixed").strip().lower()
    max_samples = int(h3.get("sample_points_per_source_cell") or 5)
    min_samples = int(h3.get("min_sample_points_per_source_cell") or 1)
    aggregation_mode = str(h3.get("aggregation_mode") or "full").strip().upper()
    stride = 1 if aggregation_mode == "FULL" else int(h3.get("pixel_stride") or 1)
    return (
        f"BASE_VIEWSHED_{run_version}"
        f"_DEM{int(dem.get('dem_resolution_m') or dem_resolution_from_config(config))}M"
        f"_SRC_R{source_resolution}"
        f"_TGT_R{target_resolution}"
        f"_SAMP{sampling_mode.upper()}_N{max_samples}_MIN{min_samples}"
        f"_AGG{aggregation_mode}_STRIDE{stride}"
    )


def versioned_vegetation_path_name(config: dict[str, Any]) -> str:
    h3 = get_h3_settings(config)
    source_resolution = int(h3.get("source_resolution") or 6)
    target_resolution = int(h3.get("target_resolution") or source_resolution)
    return (
        f"VEGETATION_PATH_WEIGHTS_{get_run_version(config)}"
        f"_SRC_R{source_resolution}"
        f"_TGT_R{target_resolution}"
    )


def write_metadata_sidecar(
    output_path: Path,
    config: dict[str, Any],
    extra_metadata: dict[str, Any] | None = None,
    filename: str = "_metadata.json",
) -> Path:
    output_path = Path(output_path)
    if output_path.suffix:
        metadata_path = output_path.with_name(f"{output_path.stem}{filename}")
    else:
        output_path.mkdir(parents=True, exist_ok=True)
        metadata_path = output_path / filename
    metadata = {
        "run_version": get_run_version(config),
        "config_hash": stable_config_hash(config),
        "created_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "h3": get_h3_settings(config),
        "dem": get_dem_settings(config),
        "config": config,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n")
    return metadata_path


def metadata_sidecar_candidates(path: Path) -> list[Path]:
    """Return canonical then legacy metadata sidecar paths for an artifact."""
    path = Path(path)
    if path.suffix:
        return [
            path.with_name(f"{path.stem}_metadata.json"),
            Path(f"{path}.metadata.json"),
            path.with_suffix(".metadata.json"),
        ]
    return [
        path / "_metadata.json",
        Path(f"{path}.metadata.json"),
    ]


def load_metadata_sidecar(path: Path) -> dict[str, Any] | None:
    for meta_path in metadata_sidecar_candidates(path):
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def validate_land_h3_resolution(
    land_df: Any,
    *,
    requested_resolution: int,
    path: Path | None = None,
) -> None:
    if "h3_resolution" not in land_df.columns:
        raise ValueError(
            "Existing land H3 file does not include h3_resolution metadata; "
            "delete the file or rerun with overwrite=True."
        )
    existing = sorted({int(v) for v in land_df["h3_resolution"].dropna().unique()})
    if existing != [int(requested_resolution)]:
        label = existing[0] if len(existing) == 1 else existing
        where = f" {path}" if path is not None else ""
        raise ValueError(
            f"Existing land H3 file{where} was built at resolution {label}, "
            f"but config requested resolution {int(requested_resolution)}. "
            "Delete the file or rerun with overwrite=True."
        )


def current_process_memory_mb() -> float | None:
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        return float(psutil.Process().memory_info().rss) / (1024 * 1024)
    except Exception:
        return None


@contextmanager
def timer(label: str | None = None):
    start = time.perf_counter()
    state: dict[str, Any] = {"label": label, "elapsed_seconds": None}
    try:
        yield state
    finally:
        state["elapsed_seconds"] = time.perf_counter() - start


def dem_resolution_from_config(raw: dict[str, Any]) -> int:
    return int(raw.get("viewshed", {}).get("dem_resolution_m", 30))


def raw_dem_dir_from_config(raw: dict[str, Any], config_dir: Path) -> Path:
    paths = raw.get("paths", {}) or {}
    return resolve_path(paths.get("raw_dem_dir", DEFAULT_RAW_DEM_DIR_RELATIVE), config_dir)


def dem_path_from_config(raw: dict[str, Any], config_dir: Path, resolution_m: int) -> Path:
    paths = raw.get("paths", {}) or {}
    requested_resolution = int(resolution_m)
    primary_resolution = dem_resolution_from_config(raw)
    configured = paths.get(f"dem_{requested_resolution}m_path")
    if configured is None and requested_resolution == primary_resolution:
        configured = paths.get("regional_dem_path")
    value = configured or DEFAULT_DEM_PATH_TEMPLATE.format(resolution_m=int(resolution_m))
    return resolve_path(str(value).format(resolution_m=int(resolution_m)), config_dir)


def output_dir_from_config(raw: dict[str, Any], config_dir: Path) -> Path:
    paths = raw.get("paths", {}) or {}
    return resolve_path(paths.get("output_dir", viewshed_domain_relative()), config_dir)


def land_h3_path_from_config(raw: dict[str, Any], config_dir: Path) -> Path:
    paths = raw.get("paths", {}) or {}
    if paths.get("land_h3_path"):
        path = resolve_path(paths["land_h3_path"], config_dir)
    else:
        resolution = int(raw.get("h3", {}).get("source_resolution", 6))
        path = resolve_path(
            DEFAULT_LAND_H3_PATH_TEMPLATE.format(resolution=resolution),
            config_dir,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def dem_path_template_from_config(raw: dict[str, Any], resolution_m: int) -> str:
    requested_resolution = int(resolution_m)
    paths = raw.get("paths", {}) or {}
    configured = paths.get(f"dem_{requested_resolution}m_path")
    if configured is None and requested_resolution == dem_resolution_from_config(raw):
        configured = paths.get("regional_dem_path")
    return str(configured or DEFAULT_DEM_PATH_TEMPLATE).format(resolution_m=requested_resolution)


def canopy_config_with_defaults(config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_CANOPY_CONFIG, **dict(config or {})}


def landcover_config_with_defaults(config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_LANDCOVER_CONFIG, **dict(config or {})}


def vegetation_path_input_defaults(
    *,
    area_lookup: Path,
    source_resolution: int,
    resolution_m: int,
) -> dict[str, str | None]:
    defaults = dict(DEFAULT_VEGETATION_PATH_INPUTS)
    defaults["clear_sky_pairs"] = str(area_lookup)
    defaults["source_cells"] = DEFAULT_LAND_H3_PATH_TEMPLATE.format(
        resolution=int(source_resolution)
    )
    defaults["source_cell_weights"] = (
        viewshed_domain_relative("land", "vegetation")
        + f"/LAND_SOURCE_CELL_VEGETATION_WEIGHTS_{int(resolution_m)}M_R{int(source_resolution)}.parquet"
    )
    return defaults
