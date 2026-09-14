"""Load and normalize viewshed pipeline configuration.

Scientific and operational role
-------------------------------
This module supports the viewshed scientific stages without owning their core
model equations. It provides common configuration dataclasses, raster helpers,
H3 geometry helpers, batching logic, water-domain preparation, input validation,
Parquet utilities, and final lookup materialization.

The central scientific contract supported here is:

    viewability_weight =
        weight_terrain * weight_vegetation

The terrain kernel integrates observer-to-pixel distance decay. The centroid
distance artifact is retained as a diagnostic; final composition multiplies
the terrain kernel only by conditional vegetation attenuation.

Current compact factor-table schemas are:

    terrain:    source_h3, target_h3, weight_terrain
    distance:   source_h3, target_h3, distance_km, weight_distance
    vegetation: source_h3, target_h3, source_type, weight_vegetation,
                vegetation_status

Major responsibilities
----------------------
- Load the viewshed YAML config into typed runtime dataclasses.
- Prepare projected DEM and batch-level water masks for terrain viewsheds.
- Generate source-cell sample points and source-cell batches.
- Validate required DEM, water, and land/source H3 inputs.
- Stream/merge Parquet partition outputs without eager full Pandas reads where
  possible.
- Compose final terrain, distance, vegetation, and viewability lookup tables.
- Optionally clean intermediate partitions after safe finalization.

Boundary of responsibility
--------------------------
Do not add terrain line-of-sight math, distance-decay models, or vegetation
attenuation science here. Those belong in `weights.terrain`, `weights.distance`,
and `weights.vegetation`. This module should remain shared plumbing plus the
explicit final composition step.
"""

from __future__ import annotations

import logging
import math
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import polars as pl

from .paths import (
    DEFAULT_DEM_PATH_TEMPLATE,
    DEFAULT_LAND_H3_PATH_TEMPLATE,
    DEFAULT_LAND_POLYGON_RELATIVE,
    DEFAULT_RAW_DEM_DIR_RELATIVE,
    DEFAULT_WATER_POLYGON_RELATIVE,
    resolve_existing_or_relative_path,
    resolve_path,
    seascape_water_polygon_path,
    stable_config_hash,
    versioned_viewshed_name,
    viewshed_domain_path,
    viewshed_domain_relative,
    write_metadata_sidecar,
)
from .schema import load_yaml_config

GEOTIFF_BLOCK_SIZE = 256
LOGGER = logging.getLogger(__name__)
SOURCE_SAMPLING_CANDIDATE_GRID_SIDE = 32
SOURCE_SAMPLING_ALGORITHM_VERSION = "component_aware_projected_nested_maximin_v4"
_PROJECTED_DEM_READY_THIS_PROCESS: set[Path] = set()


# -----------------------------------------------------------------------------
# Config dataclasses
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class StageTelemetry:
    stage: str
    elapsed_seconds: float
    input_rows: int | None = None
    output_rows: int | None = None
    unique_source_h3_cell: int | None = None
    unique_target_h3_cell: int | None = None
    output_path: str | None = None
    output_file_size_bytes: int | None = None


def log_stage_telemetry(stage: str, **kwargs: Any) -> StageTelemetry:
    output_path = kwargs.get("output_path")
    size = None
    if output_path is not None:
        p = Path(output_path)
        if p.exists() and p.is_file():
            size = p.stat().st_size
    t = StageTelemetry(
        stage=stage,
        elapsed_seconds=float(kwargs.get("elapsed_seconds", 0.0)),
        input_rows=kwargs.get("input_rows"),
        output_rows=kwargs.get("output_rows"),
        unique_source_h3_cell=kwargs.get("unique_source_h3_cell"),
        unique_target_h3_cell=kwargs.get("unique_target_h3_cell"),
        output_path=str(output_path) if output_path is not None else None,
        output_file_size_bytes=size,
    )
    LOGGER.info(
        "stage_telemetry stage=%s elapsed_seconds=%.3f input_rows=%s output_rows=%s unique_source_h3_cell=%s unique_target_h3_cell=%s output_path=%s output_file_size_bytes=%s",
        t.stage,
        t.elapsed_seconds,
        t.input_rows,
        t.output_rows,
        t.unique_source_h3_cell,
        t.unique_target_h3_cell,
        t.output_path,
        t.output_file_size_bytes,
    )
    return t


@contextmanager
def stage_timer(stage: str, **meta: Any):
    start = time.perf_counter()
    payload = dict(meta)
    try:
        yield payload
    finally:
        payload["elapsed_seconds"] = time.perf_counter() - start
        log_stage_telemetry(stage, **payload)


def scan_parquet_lazy(paths: Sequence[Path], columns: Sequence[str] | None = None) -> pl.LazyFrame:
    parquet_paths = [str(Path(p)) for p in paths]
    if not parquet_paths:
        raise ValueError("scan_parquet_lazy requires at least one input parquet path.")
    lf = pl.scan_parquet(parquet_paths)
    return lf.select(list(columns)) if columns is not None else lf


@dataclass(frozen=True)
class RunConfig:
    name: str
    version: str
    overwrite: bool = False

    # Debug / artifact controls.
    keep_intermediate_rasters: bool = False
    keep_batch_intermediates: bool = True
    write_geojson: bool = False
    write_maps: bool = False
    write_cumulative_rasters: bool = False
    write_pair_geometry: bool = False

    # Production output controls.
    skip_existing_partitions: bool = True
    combine_final_parquet: bool = False


@dataclass(frozen=True)
class PathsConfig:
    water_polygon_path: Path
    land_polygon_path: Path
    raw_dem_dir: Path
    regional_dem_path: Path
    canopy_height_path: Path
    output_dir: Path
    final_output_dir: Path
    map_dir: Path
    source_cells_path: Path
    land_h3_path: Path
    final_visibility_path: Path
    partitioned_visibility_dir: Path
    manifest_path: Path
    projected_dem_path: Path


@dataclass(frozen=True)
class ViewshedSettings:
    observer_eye_height_m: float = 1.7
    target_height_m: float = 1.0
    max_distance_m: float = 15_000
    dem_resolution_m: int = 10
    aoi_margin_m: float = 1_000
    curvature_coefficient: float = 0.85714
    earth_radius_m: float = 6_378_137.0
    crs_projected: str = "EPSG:32610"
    sea_level_m: float = 0.0
    dem_nodata_policy: str = "error"
    dem_nodata_barrier_height_m: float = 100_000.0
    backend: str = "gdal"
    surface_model: str = "bare_earth"
    canopy_resampling: str = "max"
    canopy_nodata_policy: str = "error"
    minimum_canopy_height_m: float = 0.0
    observer_canopy_clearance_radius_m: float = 0.0


@dataclass(frozen=True)
class H3Config:
    source_resolution: int = 6
    source_sampling_mode: str = "fixed"
    sample_points_per_source_cell: int = 5
    min_sample_points_per_source_cell: int = 1
    output_resolution: int = 6
    target_resolution: int | None = None
    pixel_stride: int = 1
    aggregation_mode: str = "full"
    min_visible_sampled_pixel_count: int = 1
    min_visible_area_km2: float = 0.0
    include_centroid: bool = True


@dataclass(frozen=True)
class BatchConfig:
    max_workers: int = 8
    batch_size_cells: int = 25
    accumulator_tile_size: int = 512
    max_batch_aoi_pixels: int | None = None
    max_estimated_batch_memory_mb: float | None = None
    strategy: str = "sequential"


@dataclass(frozen=True)
class RegionConfig:
    bbox_wgs84: dict[str, float]
    name: str = "region"
    coastal_buffer_m: float = 6_000
    min_source_cell_land_fraction: float = 0.01
    min_source_cell_water_fraction: float = 0.0
    max_source_cell_water_fraction: float | None = 0.95
    cleanup_natural_earth: bool = False


@dataclass(frozen=True)
class RasterConfig:
    intermediate_compress: str = "none"
    final_compress: str = "deflate"
    block_size: int = GEOTIFF_BLOCK_SIZE


@dataclass(frozen=True)
class AppConfig:
    raw_config: dict[str, Any]
    config_path: Path
    config_hash: str
    run: RunConfig
    paths: PathsConfig
    viewshed: ViewshedSettings
    h3: H3Config
    batch: BatchConfig
    region: RegionConfig
    raster: RasterConfig
    source_type: str = "land"
    observer_height_class: str | None = None


def apply_source_type_policy(app: AppConfig, source_type: str) -> AppConfig:
    """Apply source-specific sampling and observer-height policy.

    Water LOS must not inherit the generic land observer and target heights.
    The selected water observer class is recorded on ``AppConfig`` so terrain
    partition metadata and downstream artifact validation can distinguish
    kernels built for different viewing platforms.
    """

    normalized = str(source_type).strip().lower()
    if normalized not in {"land", "water"}:
        raise ValueError("source_type must be land or water")
    configured = replace(app, source_type=normalized)
    if normalized == "land":
        return configured

    water = app.raw_config.get("water_viewing", {}) or {}
    source_samples = int(water.get("source_samples_per_cell", 3))
    if source_samples < 1 or source_samples > 63:
        raise ValueError("water_viewing.source_samples_per_cell must be between 1 and 63")

    height_classes = water.get("observer_height_classes", {}) or {}
    if not isinstance(height_classes, dict) or not height_classes:
        raise ValueError(
            "water_viewing.observer_height_classes must define at least one platform class"
        )
    height_class = str(
        app.observer_height_class
        or water.get("default_observer_height_class")
        or (next(iter(height_classes)) if len(height_classes) == 1 else "")
    ).strip()
    if not height_class or height_class not in height_classes:
        raise ValueError(
            "water_viewing.default_observer_height_class must name a configured "
            f"observer height class; configured={sorted(height_classes)}"
        )
    class_config = height_classes[height_class]
    if not isinstance(class_config, dict):
        raise ValueError(f"water_viewing.observer_height_classes.{height_class} must be a mapping")
    observer_height_m = float(class_config.get("eye_height_m", math.nan))
    target_height_m = float(water.get("target_visibility_height_m", math.nan))
    if not math.isfinite(observer_height_m) or observer_height_m <= 0:
        raise ValueError(f"water observer height for class {height_class!r} must be finite and > 0")
    if not math.isfinite(target_height_m) or target_height_m <= 0:
        raise ValueError("water_viewing.target_visibility_height_m must be finite and > 0")
    return replace(
        configured,
        observer_height_class=height_class,
        viewshed=replace(
            configured.viewshed,
            observer_eye_height_m=observer_height_m,
            target_height_m=target_height_m,
        ),
        h3=replace(
            configured.h3,
            source_sampling_mode="fixed",
            sample_points_per_source_cell=source_samples,
            min_sample_points_per_source_cell=source_samples,
        ),
    )


@dataclass(frozen=True)
class ViewshedConfig:
    observer_eye_height_m: float
    target_height_m: float
    max_distance_m: float
    dem_resolution_m: int
    aoi_margin_m: float
    curvature_coefficient: float
    earth_radius_m: float
    crs_projected: str
    sea_level_m: float
    dem_nodata_policy: str
    dem_nodata_barrier_height_m: float
    backend: str
    surface_model: str
    canopy_resampling: str
    canopy_nodata_policy: str
    minimum_canopy_height_m: float
    observer_canopy_clearance_radius_m: float
    water_polygon_path: Path
    land_polygon_path: Path
    canopy_height_path: Path
    raw_dem_dir: Path
    output_dir: Path
    map_dir: Path
    run_name: str
    viewshed_version: str


@dataclass(frozen=True)
class BatchContext:
    batch_id: str
    batch_index: int
    source_cells: list[str]
    all_sample_points: gpd.GeoDataFrame
    aoi_wgs84: gpd.GeoDataFrame
    aoi_projected: gpd.GeoDataFrame
    water_mask_path: Path
    analysis_dem_path: Path
    endpoint_dem_path: Path
    water_mask_arr: np.ndarray
    water_transform: Any
    source_cell_metadata: pd.DataFrame
    raw_dem_path: Path
    dem_projected_path: Path
    dem_clip_path: Path
    water_crs: Any | None = None
    water_shape: tuple[int, int] | None = None
    water_pixel_area_m2: float | None = None
    canonical_water_mask_path: Path | None = None
    aligned_canopy_height_path: Path | None = None
    surface_metadata: dict[str, Any] | None = None
    xrspatial_dem_da: Any | None = None
    # Batch-scoped runtime indexes. These deliberately live on the context so
    # regional runs do not retain one large pixel/lookup cache per completed
    # batch for the lifetime of the Python process.
    water_pixel_h3_index: Any | None = None
    lookup_targets_by_source: dict[str, frozenset[str]] | None = None
    lookup_distances_by_source: dict[str, dict[str, float]] | None = None


def normalize_viewshed_backend(value: str | None) -> str:
    backend = str(value or "gdal").strip().lower()
    if backend == "xarray":
        return "xrspatial"
    if backend not in {"gdal", "xrspatial"}:
        raise ValueError("viewshed.backend must be one of: 'gdal', 'xrspatial', or alias 'xarray'.")
    return backend


def normalize_terrain_surface_model(value: str | None) -> str:
    """Normalize the elevation surface used by the radius viewshed."""

    surface_model = str(value or "bare_earth").strip().lower()
    aliases = {
        "dem": "bare_earth",
        "dtm": "bare_earth",
        "dsm": "canopy",
        "dem_plus_chm": "canopy",
        "dtm_plus_chm": "canopy",
    }
    surface_model = aliases.get(surface_model, surface_model)
    if surface_model not in {"bare_earth", "canopy"}:
        raise ValueError("viewshed.surface_model must be one of: 'bare_earth', 'canopy'.")
    return surface_model


def _find_upward(start: Path, relative_path: str) -> Path | None:
    """Search start and its parents for a repo-relative file."""
    start = start.expanduser().resolve()
    for root in [start, *start.parents]:
        candidate = root / relative_path
        if candidate.exists():
            return candidate.resolve()
    return None


def _infer_water_polygon_path(paths: dict[str, Any], config_dir: Path) -> Path:
    """
    Resolve the water polygon path.

    Priority:
    1. Explicit YAML override: paths.water_polygon_path
    2. Inferred OrcaCast repo-level processed GIS path
    3. Inferred local viewshed data path
    4. Clear failure with searched candidates
    """
    explicit = paths.get("water_polygon_path")
    if explicit:
        return resolve_existing_or_relative_path(explicit, config_dir)

    try:
        return seascape_water_polygon_path()
    except Exception:
        pass

    relative_candidates = [
        # Preferred OrcaCast project data location.
        DEFAULT_WATER_POLYGON_RELATIVE,
        # Local viewshed/notebook fallback locations.
        "data/TERRITORIAL_WATER_POLYGON.parquet",
        DEFAULT_WATER_POLYGON_RELATIVE,
        "data/marine/TERRITORIAL_WATER_POLYGON.parquet",
    ]

    searched: list[Path] = []
    for relative in relative_candidates:
        found = _find_upward(config_dir, relative)
        if found is not None:
            return found

        for root in [config_dir.resolve(), *config_dir.resolve().parents]:
            searched.append(root / relative)

    searched_preview = "\n".join(f"  - {path}" for path in searched[:30])
    raise FileNotFoundError(
        "Could not infer water polygon path. Either place the water polygon in the "
        "standard OrcaCast data location or set paths.water_polygon_path in the YAML.\n"
        "Searched:\n"
        f"{searched_preview}"
    )


def load_app_config(config_path: str | Path) -> AppConfig:
    """Load and validate viewshed configuration without mutating the filesystem.

    Runtime commands that need output directories or partition metadata must
    call :func:`initialize_app_config` explicitly after applying any command-line
    overrides.
    """

    config_path = resolve_existing_or_relative_path(config_path, Path.cwd())
    config_dir = config_path.parent
    raw = load_yaml_config(config_path)
    h3_raw = dict(raw.get("h3", {}) or {})
    h3_raw["source_sampling_mode"] = (
        str(h3_raw.get("source_sampling_mode", "fixed")).strip().lower()
    )
    if "target_resolution" in h3_raw:
        if "output_resolution" in h3_raw and int(h3_raw["output_resolution"]) != int(
            h3_raw["target_resolution"]
        ):
            raise ValueError(
                "h3.output_resolution and h3.target_resolution disagree. "
                "Use target_resolution and remove the deprecated output_resolution alias."
            )
        h3_raw["output_resolution"] = h3_raw["target_resolution"]
    else:
        h3_raw["target_resolution"] = h3_raw.get(
            "output_resolution", h3_raw.get("source_resolution", 6)
        )
        h3_raw["output_resolution"] = h3_raw["target_resolution"]

    paths = raw.get("paths", {})
    viewshed_raw = dict(raw.get("viewshed", {}) or {})
    viewshed_raw["backend"] = normalize_viewshed_backend(viewshed_raw.get("backend"))
    viewshed_raw["surface_model"] = normalize_terrain_surface_model(
        viewshed_raw.get("surface_model")
    )
    canopy_nodata_policy = str(viewshed_raw.get("canopy_nodata_policy", "error")).strip().lower()
    if canopy_nodata_policy not in {"error", "zero"}:
        raise ValueError("viewshed.canopy_nodata_policy must be one of: 'error', 'zero'.")
    viewshed_raw["canopy_nodata_policy"] = canopy_nodata_policy
    dem_nodata_policy = str(viewshed_raw.get("dem_nodata_policy", "error")).strip().lower()
    if dem_nodata_policy not in {"error", "opaque_barrier"}:
        raise ValueError("viewshed.dem_nodata_policy must be one of: 'error', 'opaque_barrier'.")
    viewshed_raw["dem_nodata_policy"] = dem_nodata_policy
    dem_nodata_barrier_height_m = float(viewshed_raw.get("dem_nodata_barrier_height_m", 100_000.0))
    if not math.isfinite(dem_nodata_barrier_height_m) or dem_nodata_barrier_height_m <= 0:
        raise ValueError("viewshed.dem_nodata_barrier_height_m must be finite and > 0.")
    viewshed_raw["dem_nodata_barrier_height_m"] = dem_nodata_barrier_height_m
    batch_raw = dict(raw.get("batch", {}))
    if "parent_resolution" in batch_raw:
        warnings.warn(
            "batch.parent_resolution is deprecated and ignored; parent resolution is derived from h3.source_resolution.",
            DeprecationWarning,
            stacklevel=2,
        )
        batch_raw.pop("parent_resolution", None)
    dem_resolution_m = int(raw.get("viewshed", {}).get("dem_resolution_m", 10))
    regional_dem_path = resolve_existing_or_relative_path(
        paths.get(
            "regional_dem_path",
            DEFAULT_DEM_PATH_TEMPLATE.format(resolution_m=dem_resolution_m),
        ),
        config_dir,
    )
    canopy_height_path = resolve_existing_or_relative_path(
        paths.get(
            "canopy_height_path",
            regional_dem_path.with_name(f"CHM_{dem_resolution_m}M.tif"),
        ),
        config_dir,
    )
    base_output_name = versioned_viewshed_name(raw)
    default_base_dir = Path(viewshed_domain_relative())
    final_visibility_path = resolve_path(
        paths.get(
            "final_visibility_path",
            default_base_dir / f"{base_output_name}.parquet",
        ),
        config_dir,
    )
    default_partition_dir = final_visibility_path.parent / f"{base_output_name}_PARTITIONS"

    partitioned_visibility_dir = resolve_path(
        paths.get("partitioned_visibility_dir", default_partition_dir),
        config_dir,
    )
    manifest_path = resolve_path(
        paths.get("manifest_path", final_visibility_path.parent / f"{base_output_name}.csv"),
        config_dir,
    )
    default_projected_dem_path = projected_dem_cache_path_for_config(raw, config_dir)

    cfg = AppConfig(
        raw_config=raw,
        config_path=config_path,
        config_hash=stable_config_hash(raw),
        run=RunConfig(**raw.get("run", {})),
        paths=PathsConfig(
            water_polygon_path=_infer_water_polygon_path(paths, config_dir),
            land_polygon_path=resolve_existing_or_relative_path(
                paths.get("land_polygon_path", DEFAULT_LAND_POLYGON_RELATIVE),
                config_dir,
            ),
            raw_dem_dir=resolve_path(
                paths.get("raw_dem_dir", DEFAULT_RAW_DEM_DIR_RELATIVE), config_dir
            ),
            regional_dem_path=resolve_path(
                regional_dem_path,
                config_dir,
            ),
            canopy_height_path=canopy_height_path,
            output_dir=resolve_path(
                paths.get("output_dir", viewshed_domain_relative()), config_dir
            ),
            final_output_dir=resolve_path(
                paths.get(
                    "final_output_dir",
                    paths.get("output_dir", viewshed_domain_relative()),
                ),
                config_dir,
            ),
            map_dir=resolve_path(paths.get("map_dir", "outputs/viewshed/maps"), config_dir),
            source_cells_path=resolve_existing_or_relative_path(
                paths.get(
                    "source_cells_path",
                    DEFAULT_LAND_H3_PATH_TEMPLATE.format(
                        resolution=int(raw.get("h3", {}).get("source_resolution", 6))
                    ),
                ),
                config_dir,
            ),
            land_h3_path=resolve_existing_or_relative_path(
                paths.get(
                    "land_h3_path",
                    DEFAULT_LAND_H3_PATH_TEMPLATE.format(
                        resolution=int(raw.get("h3", {}).get("source_resolution", 6))
                    ),
                ),
                config_dir,
            ),
            final_visibility_path=final_visibility_path,
            partitioned_visibility_dir=partitioned_visibility_dir,
            manifest_path=manifest_path,
            projected_dem_path=resolve_path(
                paths.get(
                    "projected_dem_path",
                    default_projected_dem_path,
                ),
                config_dir,
            ),
        ),
        viewshed=ViewshedSettings(**viewshed_raw),
        h3=H3Config(**h3_raw),
        batch=BatchConfig(**batch_raw),
        region=RegionConfig(**raw.get("region", {})),
        raster=RasterConfig(**raw.get("raster", {})),
    )

    if cfg.h3.aggregation_mode not in {"sampled", "full"}:
        raise ValueError("h3.aggregation_mode must be one of: sampled, full")
    sampling_mode = str(cfg.h3.source_sampling_mode).strip().lower()
    if sampling_mode not in {"fixed", "active_fraction"}:
        raise ValueError("h3.source_sampling_mode must be one of: fixed, active_fraction")
    if int(cfg.h3.sample_points_per_source_cell) < 1:
        raise ValueError("h3.sample_points_per_source_cell must be >= 1")
    if int(cfg.h3.min_sample_points_per_source_cell) < 1:
        raise ValueError("h3.min_sample_points_per_source_cell must be >= 1")
    if int(cfg.h3.min_sample_points_per_source_cell) > int(cfg.h3.sample_points_per_source_cell):
        raise ValueError(
            "h3.min_sample_points_per_source_cell must be <= " "h3.sample_points_per_source_cell"
        )
    if int(cfg.h3.sample_points_per_source_cell) > 63:
        raise ValueError(
            "h3.sample_points_per_source_cell must be <= 63 because exact observer "
            "support diagnostics use a uint64 observer mask."
        )
    if cfg.batch.strategy not in {"sequential", "spatial_sort", "h3_parent"}:
        raise ValueError("batch.strategy must be one of: sequential, spatial_sort, h3_parent")
    if int(cfg.batch.accumulator_tile_size) < 1:
        raise ValueError("batch.accumulator_tile_size must be a positive integer")
    if cfg.h3.aggregation_mode == "sampled" and int(cfg.h3.pixel_stride) > 1:
        warnings.warn(
            "h3.aggregation_mode=sampled with pixel_stride > 1 is approximate and should be validated against full aggregation.",
            RuntimeWarning,
            stacklevel=2,
        )

    return cfg


def initialize_app_config(config: AppConfig) -> AppConfig:
    """Prepare filesystem state required by terrain viewshed execution.

    Keeping this operation separate makes configuration loading safe for
    inspection, validation, and tests while preserving the directory and
    metadata setup expected by runtime commands.
    """

    config.paths.output_dir.mkdir(parents=True, exist_ok=True)
    if config.run.write_maps:
        config.paths.map_dir.mkdir(parents=True, exist_ok=True)
    config.paths.final_visibility_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.partitioned_visibility_dir.mkdir(parents=True, exist_ok=True)
    config.paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.projected_dem_path.parent.mkdir(parents=True, exist_ok=True)

    write_metadata_sidecar(
        config.paths.partitioned_visibility_dir,
        config.raw_config,
        {"step": "viewshed_clear_sky_terrain"},
    )
    return config


def projected_dem_cache_path_for_config(raw: dict[str, Any], config_dir: Path) -> Path:
    dem_resolution_m = int(raw.get("viewshed", {}).get("dem_resolution_m", 10))
    run_version = str(raw.get("run", {}).get("version", "unversioned"))
    crs_label = str(raw.get("viewshed", {}).get("crs_projected", "EPSG:32610")).replace(":", "")
    return viewshed_domain_path(
        "dem_cache",
        f"DEM_PROJECTED_{run_version}_{crs_label}_{dem_resolution_m}M.tif",
        config_dir=config_dir,
    )


def viewshed_config_from_app_config(app: AppConfig) -> ViewshedConfig:
    return ViewshedConfig(
        observer_eye_height_m=app.viewshed.observer_eye_height_m,
        target_height_m=app.viewshed.target_height_m,
        max_distance_m=app.viewshed.max_distance_m,
        dem_resolution_m=app.viewshed.dem_resolution_m,
        aoi_margin_m=app.viewshed.aoi_margin_m,
        curvature_coefficient=app.viewshed.curvature_coefficient,
        earth_radius_m=app.viewshed.earth_radius_m,
        crs_projected=app.viewshed.crs_projected,
        sea_level_m=app.viewshed.sea_level_m,
        dem_nodata_policy=app.viewshed.dem_nodata_policy,
        dem_nodata_barrier_height_m=app.viewshed.dem_nodata_barrier_height_m,
        backend=app.viewshed.backend,
        surface_model=app.viewshed.surface_model,
        canopy_resampling=app.viewshed.canopy_resampling,
        canopy_nodata_policy=app.viewshed.canopy_nodata_policy,
        minimum_canopy_height_m=app.viewshed.minimum_canopy_height_m,
        observer_canopy_clearance_radius_m=app.viewshed.observer_canopy_clearance_radius_m,
        water_polygon_path=app.paths.water_polygon_path,
        land_polygon_path=app.paths.land_polygon_path,
        canopy_height_path=app.paths.canopy_height_path,
        raw_dem_dir=app.paths.raw_dem_dir,
        output_dir=app.paths.output_dir,
        map_dir=app.paths.map_dir,
        run_name=app.run.name,
        viewshed_version=app.run.version,
    )


# -----------------------------------------------------------------------------
# Raster utilities
# -----------------------------------------------------------------------------
