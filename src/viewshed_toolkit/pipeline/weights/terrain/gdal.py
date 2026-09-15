"""Terrain clear-sky visibility support for viewshed weights.

Scientific role
---------------
This module estimates clear-sky bare-earth visibility and integrates the
configured distance curve at observer/target-water-pixel resolution.

The terrain factor is intentionally kept separate from later attenuation
layers. Its persisted output is a sparse pair table:

    source_h3
        Source-domain H3 cell.

    target_h3
        Water/target H3 cell.

    weight_terrain
        Mean ``LOS * D(distance)`` across modeled observer/pixel pairs.

Method summary
--------------
For each source H3 cell, the pipeline samples one or more observer points,
runs a DEM-backed viewshed from each point, masks the result to water, and
aggregates visible water pixels into target H3 cells. Internally it keeps richer
diagnostics including any-observer support, union-visible target support, joint
LOS support, visible pixel counts, and near/mean/far view distances. Partitions
retain weighted and unweighted numerators plus denominators for auditability.

Core assumptions
----------------
- The DEM is the terrain surface used for line-of-sight obstruction.
- Observer eye height and target height come from the active viewshed config.
- Source and target H3 resolutions are treated as aligned for this workflow.
- Water targets are derived from the configured water/land domain, not from a
  dense all-source/all-target matrix.
- `weight_terrain` is mean LOS times distance detectability over observer/pixel
  pairs. It is clipped to [0, 1] before being written.

Downstream contract
-------------------
The final viewability stage multiplies this distance-integrated factor only by
conditional vegetation attenuation:

    physical_view_score =
        weight_terrain * weight_vegetation

The centroid ``weight_distance`` is retained only as a diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import polars as pl
import pyarrow.parquet as pq
import rasterio
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import Point
from shapely.ops import transform as shapely_transform

from viewshed_toolkit._internal.geo.geometry import CRS_WGS84
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

from ...config import (
    SOURCE_SAMPLING_ALGORITHM_VERSION,
    SOURCE_SAMPLING_CANDIDATE_GRID_SIDE,
    AppConfig,
    BatchContext,
    metadata_sidecar_candidates,
    stable_config_hash,
    timer,
)
from ...contracts.artifacts import final_artifact_paths
from ...prepare.area import domains, sample_points_in_source_geometry
from ...prepare.area.geometry import (
    h3_geometry_artifact_path,
    h3_geometry_metadata,
    load_h3_geometry_lookup,
)
from ...prepare.area.inputs import load_source_cells
from .. import distance

LOGGER = logging.getLogger(__name__)
_TARGET_WATER_AREA_BY_H3_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_WATER_TERRAIN_DOMAIN_CACHE: dict[str, domains.DomainGeometries] = {}
_PROJECTED_LAND_DOMAIN_CACHE: dict[tuple[str, str, str, int], Any] = {}
_PROJECTED_LAND_EXCLUSION_CACHE: dict[tuple[int, float], Any] = {}
_PROJECTED_TRANSFORMER_CACHE: dict[tuple[str, str, str], Transformer] = {}
_PROJECTED_H3_BOUND_CACHE: dict[tuple[str, str, str, str], tuple[float, float, float]] = {}
_PROJECTED_H3_BOUNDS_CACHE: dict[tuple[str, str, str], dict[str, tuple[float, float, float]]] = {}
_PROJECTED_WATER_TARGET_SAMPLE_CACHE: dict[tuple[Any, ...], tuple[Point, ...]] = {}
_WATER_TERRAIN_PREFILTER_CACHE: dict[tuple[Any, ...], tuple[pd.DataFrame, set[str] | None]] = {}
_WATER_TERRAIN_PREFILTER_CACHE_LIMIT = 128

try:
    import xarray as xr
except ImportError:
    xr = None

try:
    import rioxarray
except ImportError:
    rioxarray = None

_XR_VIEW_SHED = None
_XR_VIEW_SHED_IMPORT_ERROR: Exception | None = None


from .summarize import domain_target_water_area_by_h3


def _cached_h3_geometry_for_app(
    app: AppConfig,
    column: str,
) -> dict[str, Any] | None:
    """Return the process-cached canonical geometry lookup when available."""

    paths = getattr(app, "paths", None)
    h3_config = getattr(app, "h3", None)
    if paths is None or h3_config is None:
        return None
    path = h3_geometry_artifact_path(paths.output_dir, h3_config.output_resolution)
    if not path.exists():
        return None
    if column in {"geometry_projected", "water_geometry_projected"}:
        metadata = h3_geometry_metadata(path)
        artifact_crs = metadata.get("geometry_projected_crs")
        if CRS.from_user_input(artifact_crs) != CRS.from_user_input(app.viewshed.crs_projected):
            raise ValueError(
                "H3 geometry artifact projection does not match the terrain projection: "
                f"artifact={artifact_crs} terrain={app.viewshed.crs_projected}"
            )
    return load_h3_geometry_lookup(path, column)


def _partition_path_for_source(app: AppConfig, source_cell: str) -> Path:
    return _partitioned_visibility_dir_for_app(app) / f"source_h3_cell={source_cell}.parquet"


def _source_type_for_app(app: AppConfig) -> str:
    source_type = str(getattr(app, "source_type", "land") or "land")
    if source_type not in {"land", "water"}:
        raise ValueError(f"source_type must be 'land' or 'water'; got {source_type!r}")
    return source_type


def _partitioned_visibility_dir_for_app(app: AppConfig) -> Path:
    source_type = _source_type_for_app(app)
    if source_type == "land":
        return app.paths.partitioned_visibility_dir / "source=land"
    if source_type == "water":
        return app.paths.partitioned_visibility_dir / "source=water"
    raise ValueError("source_type must be land or water")


def _terrain_partition_config_hash(app: AppConfig) -> str:
    """Hash model inputs while excluding presentation and publishing settings."""

    payload = dict(app.raw_config)
    payload.pop("static_maps", None)
    payload.pop("publishing", None)

    paths = dict(payload.get("paths", {}) or {})
    paths.pop("map_dir", None)
    payload["paths"] = paths

    run = dict(payload.get("run", {}) or {})
    for key in (
        "overwrite",
        "skip_existing_partitions",
        "combine_final_parquet",
        "keep_intermediate_rasters",
        "keep_batch_intermediates",
        "write_geojson",
        "write_maps",
        "write_cumulative_rasters",
        "write_pair_geometry",
    ):
        run.pop(key, None)
    payload["run"] = run
    return stable_config_hash(payload)


def expected_partition_metadata(app: AppConfig) -> dict[str, Any]:
    water_viewing = app.raw_config.get("water_viewing", {}) or {}
    metadata = {
        "run_version": app.run.version,
        "config_hash": _terrain_partition_config_hash(app),
        "source_resolution": int(app.h3.source_resolution),
        "target_resolution": int(app.h3.output_resolution),
        "source_sampling_mode": str(app.h3.source_sampling_mode),
        "max_sample_points_per_source_cell": int(app.h3.sample_points_per_source_cell),
        "min_sample_points_per_source_cell": int(app.h3.min_sample_points_per_source_cell),
        "source_sampling_algorithm_version": SOURCE_SAMPLING_ALGORITHM_VERSION,
        "source_sampling_projected_crs": str(app.viewshed.crs_projected),
        "source_sampling_candidate_grid_side": SOURCE_SAMPLING_CANDIDATE_GRID_SIDE,
        "source_sampling_max_design_points": int(app.h3.sample_points_per_source_cell),
        "sample_points_per_source_cell": int(app.h3.sample_points_per_source_cell),
        "pixel_stride": (1 if app.h3.aggregation_mode == "full" else int(app.h3.pixel_stride)),
        "aggregation_mode": app.h3.aggregation_mode,
        "observer_eye_height_m": float(app.viewshed.observer_eye_height_m),
        "observer_height_m": float(app.viewshed.observer_eye_height_m),
        "target_height_m": float(app.viewshed.target_height_m),
        "max_distance_m": float(app.viewshed.max_distance_m),
        "dem_resolution_m": int(app.viewshed.dem_resolution_m),
        "viewshed_backend": getattr(app.viewshed, "backend", "gdal"),
        "viewshed_backend_experimental": getattr(app.viewshed, "backend", "gdal") == "xrspatial",
        "terrain_surface_model": str(getattr(app.viewshed, "surface_model", "bare_earth")),
        "canopy_height_path": str(app.paths.canopy_height_path),
        "canopy_resampling": str(getattr(app.viewshed, "canopy_resampling", "max")),
        "canopy_nodata_policy": str(getattr(app.viewshed, "canopy_nodata_policy", "error")),
        "minimum_canopy_height_m": float(getattr(app.viewshed, "minimum_canopy_height_m", 0.0)),
        "observer_canopy_clearance_radius_m": float(
            getattr(app.viewshed, "observer_canopy_clearance_radius_m", 0.0)
        ),
        "landcover_in_terrain_los": False,
        "curvature_applied": float(getattr(app.viewshed, "curvature_coefficient", 0.0)) != 0.0,
        "curvature_coefficient": float(getattr(app.viewshed, "curvature_coefficient", 0.0)),
        "earth_radius_m": float(getattr(app.viewshed, "earth_radius_m", 6378137.0)),
        "viewshed_model_version": app.run.version,
        "target_water_area_model_version": "complete_h3_water_intersection_v1",
        "terrain_weight_method": str(
            app.raw_config.get("viewshed", {}).get(
                "terrain_weight_method",
                "distance_weighted_observer_target_water_pixel_kernel",
            )
        ),
        "dem_nodata_los_policy": str(getattr(app.viewshed, "dem_nodata_policy", "error")),
        "dem_nodata_barrier_height_m": float(
            getattr(app.viewshed, "dem_nodata_barrier_height_m", 100_000.0)
        ),
        "target_water_area_equal_area_crs": str(
            water_viewing.get("target_area_equal_area_crs", "EPSG:6933")
        ),
        "terrain_partition_schema_version": "adaptive_active_fraction_v8",
        "source_type": _source_type_for_app(app),
    }
    if _source_type_for_app(app) == "land":
        from ...prepare.area.raster_stack import _canonical_dataset_signature

        signatures = {}
        for name in (
            "regional_dem_path",
            "water_polygon_path",
            "land_polygon_path",
            "land_h3_path",
        ):
            path = getattr(app.paths, name, None)
            if path is not None:
                signatures[name] = (
                    _canonical_dataset_signature(Path(path))
                    if Path(path).exists()
                    else {"unavailable": str(path)}
                )
        metadata["land_scientific_input_signatures"] = json.dumps(signatures, sort_keys=True)
    if metadata["terrain_surface_model"] == "canopy":
        from ...prepare.elevation.cache import input_signature
        from ...prepare.elevation.canopy import OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION

        metadata["canopy_observer_algorithm_version"] = OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION
        metadata["canopy_input_signature"] = json.dumps(
            input_signature(Path(app.paths.canopy_height_path)), sort_keys=True
        )
    if metadata["viewshed_backend"] == "gdal":
        metadata["gdal_grid_alignment_version"] = "strict_parent_grid_v1"
    if _source_type_for_app(app) == "water":
        land_buffer_m, target_samples = _water_land_mask_settings(app)
        metadata["water_terrain_model"] = "opaque_land_mask_v1"
        metadata["water_terrain_prefilter_version"] = "land_mask_los_v1"
        metadata["water_land_buffer_m"] = land_buffer_m
        metadata["water_target_samples_per_cell"] = target_samples
        metadata["observer_height_class"] = str(app.observer_height_class or "default")
    return metadata


def _partition_metadata_sidecar_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_metadata.json")


def _write_partition_metadata_sidecar(path: Path, expected: dict[str, Any]) -> None:
    sidecar = _partition_metadata_sidecar_path(path)
    sidecar.write_text(json.dumps(expected, indent=2, sort_keys=True, default=str) + "\n")


def _read_partition_metadata_sidecar(path: Path) -> dict[str, Any] | None:
    for sidecar in metadata_sidecar_candidates(path):
        if not sidecar.exists():
            continue
        try:
            raw = json.loads(sidecar.read_text())
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None
    return None


def _metadata_values_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for column, value in expected.items():
        if column not in actual:
            return False
        actual_value = actual[column]
        if pd.isna(actual_value) and value is not None:
            return False
        if isinstance(value, float):
            if not np.isclose(float(actual_value), value):
                return False
        else:
            if str(actual_value) != str(value):
                return False
    return True


def partition_metadata_matches(path: Path, expected: dict[str, Any]) -> bool:
    """Return True when compact partition sidecar metadata matches expected values."""
    if not path.exists():
        return False
    sidecar = _read_partition_metadata_sidecar(path)
    return bool(sidecar and _metadata_values_match(sidecar, expected))


def _partition_row_count(path: Path) -> int:
    try:
        return int(pq.read_metadata(str(path)).num_rows)
    except Exception:
        return int(len(pd.read_parquet(path, columns=[])))


def _area_lookup_path_for_app(app: AppConfig) -> Path:
    """Return the canonical slim source-target lookup for the active H3 resolution.

    The terrain stage accepts only the active working directory's lookup. This
    avoids accidentally consuming stale artifacts from older pipeline layouts.
    """

    path = (
        app.paths.output_dir
        / "lookup"
        / f"SOURCE_TARGET_LOOKUP_H3R{int(app.h3.source_resolution)}.parquet"
    )
    if not path.exists():
        raise FileNotFoundError(
            "Missing canonical source-target lookup. Expected clean-standard lookup at: " f"{path}"
        )
    return path


def _terrain_weights_final_path(app: AppConfig) -> Path:
    paths = final_artifact_paths(app.config_path)
    if _source_type_for_app(app) == "water":
        return paths.ocean_terrain_weights
    return paths.terrain_weights


def _lookup_source_cells(app: AppConfig) -> set[str] | None:
    lookup_path = _area_lookup_path_for_app(app)
    source_type = _source_type_for_app(app)
    return set(
        pl.scan_parquet(str(lookup_path))
        .filter(pl.col("source_type") == source_type)
        .select(pl.col("source_h3").cast(pl.Utf8))
        .unique()
        .collect()["source_h3"]
        .to_list()
    )


def load_batch_lookup(
    app: AppConfig,
    source_cells: Sequence[str],
    *,
    include_distances: bool = False,
) -> tuple[dict[str, frozenset[str]], dict[str, dict[str, float]] | None]:
    """Load only the canonical lookup rows needed by one terrain batch."""

    requested = tuple(dict.fromkeys(str(cell) for cell in source_cells))
    if not requested:
        return {}, {} if include_distances else None
    lookup_path = _area_lookup_path_for_app(app)
    source_type = _source_type_for_app(app)
    columns: list[pl.Expr] = [
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
    ]
    if include_distances:
        columns.append(pl.col("distance_km").cast(pl.Float64))
    frame = (
        pl.scan_parquet(str(lookup_path))
        .filter(
            (pl.col("source_type") == source_type)
            & pl.col("source_h3").cast(pl.Utf8).is_in(list(requested))
        )
        .select(columns)
        .collect(engine="streaming")
    )
    targets: dict[str, frozenset[str]] = {source: frozenset() for source in requested}
    distances: dict[str, dict[str, float]] | None = (
        {source: {} for source in requested} if include_distances else None
    )
    for source, group in frame.group_by("source_h3", maintain_order=False):
        source_key = str(source[0] if isinstance(source, tuple) else source)
        target_values = tuple(str(value) for value in group["target_h3"].to_list())
        targets[source_key] = frozenset(target_values)
        if distances is not None:
            distances[source_key] = {
                target: float(value)
                for target, value in zip(
                    target_values,
                    group["distance_km"].to_list(),
                    strict=True,
                )
            }
    LOGGER.info(
        "terrain_batch_lookup source_type=%s requested_sources=%d rows=%d "
        "include_distances=%s lookup=%s",
        source_type,
        len(requested),
        frame.height,
        include_distances,
        lookup_path,
    )
    return targets, distances


def _lookup_target_cells_for_source(app: AppConfig, source_cell: str) -> set[str] | None:
    targets, _ = load_batch_lookup(app, [str(source_cell)])
    return set(targets.get(str(source_cell), frozenset()))


def _load_terrain_source_cells(app: AppConfig) -> gpd.GeoDataFrame:
    source_type = _source_type_for_app(app)
    if source_type == "land":
        return load_source_cells(app)

    lookup_path = _area_lookup_path_for_app(app)
    if not lookup_path.exists():
        raise FileNotFoundError(f"Missing source-target lookup: {lookup_path}")
    source_cells = sorted(_lookup_source_cells(app) or [])
    if not source_cells:
        raise ValueError(f"No water source cells found in lookup: {lookup_path}")

    runtime = distance.load_distance_runtime(app.config_path)
    domain_geometries = domains.load_land_water_domains(runtime, extent="target")
    full_geometry_lookup = _cached_h3_geometry_for_app(app, "geometry_wgs84")
    water_projected_lookup = _cached_h3_geometry_for_app(app, "water_geometry_projected")
    to_wgs84 = Transformer.from_crs(app.viewshed.crs_projected, CRS_WGS84, always_xy=True)
    rows: list[dict[str, Any]] = []
    geoms = []
    projected_water_geometries = []
    for cell in source_cells:
        geom = (
            full_geometry_lookup.get(str(cell))
            if full_geometry_lookup is not None
            else cell_to_polygon(str(cell))
        )
        water_projected = (
            water_projected_lookup.get(str(cell)) if water_projected_lookup is not None else None
        )
        if water_projected is None:
            water_geom = geom.intersection(domain_geometries.water_domain)
            water_projected = shapely_transform(
                _projected_transformer_for_app(app).transform, water_geom
            )
        else:
            water_geom = shapely_transform(to_wgs84.transform, water_projected)
        if water_projected.is_empty:
            continue
        point = shapely_transform(to_wgs84.transform, water_projected.representative_point())
        rows.append(
            {
                "h3_cell": str(cell),
                "source_type": "water",
                "centroid_lon": float(point.x),
                "centroid_lat": float(point.y),
                "water_geometry": water_geom,
            }
        )
        geoms.append(geom)
        projected_water_geometries.append(water_projected)
    if not rows:
        raise ValueError(f"No water-overlapping source cells found in lookup: {lookup_path}")
    out = gpd.GeoDataFrame(rows, geometry=geoms, crs=CRS_WGS84)
    projected_full_lookup = _cached_h3_geometry_for_app(app, "geometry_projected")
    if projected_full_lookup is None:
        full_area_m2 = out.to_crs(app.viewshed.crs_projected).geometry.area.to_numpy(
            dtype="float64"
        )
    else:
        full_area_m2 = np.asarray(
            [projected_full_lookup[cell].area for cell in out["h3_cell"].astype(str)],
            dtype="float64",
        )
    water_area_m2 = np.asarray(
        [geometry.area for geometry in projected_water_geometries], dtype="float64"
    )
    water_fraction = np.divide(
        water_area_m2,
        full_area_m2,
        out=np.zeros_like(water_area_m2),
        where=full_area_m2 > 0,
    )
    out["water_fraction"] = np.clip(water_fraction, 0.0, 1.0)
    out["land_fraction"] = 1.0 - out["water_fraction"]
    return out


def _water_terrain_domains_for_app(app: AppConfig) -> domains.DomainGeometries:
    cache_key = f"{app.config_path}:{app.config_hash}"
    cached = _WATER_TERRAIN_DOMAIN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    runtime = distance.load_distance_runtime(app.config_path)
    domain_geometries = domains.load_land_water_domains(runtime, extent="target")
    _WATER_TERRAIN_DOMAIN_CACHE[cache_key] = domain_geometries
    return domain_geometries


def refracted_horizon_distance_m(
    observer_height_m: float,
    target_height_m: float,
    *,
    curvature_coefficient: float,
    earth_radius_m: float,
) -> float:
    """Return the two-ended geometric horizon under GDAL's curvature convention.

    GDAL lowers terrain by ``c * distance^2 / (2R)``. This is equivalent to an
    effective Earth radius of ``R / c`` when ``c > 0``. A zero curvature
    coefficient models a flat surface and therefore has no geometric horizon.
    """

    coefficient = float(curvature_coefficient)
    if coefficient <= 0.0:
        return math.inf
    radius = float(earth_radius_m) / coefficient

    def one_sided(height_m: float) -> float:
        height = max(0.0, float(height_m))
        return math.sqrt(max(0.0, 2.0 * radius * height + height * height))

    return one_sided(observer_height_m) + one_sided(target_height_m)


def _water_land_mask_settings(app: AppConfig) -> tuple[float, int]:
    """Return the opaque-land buffer and target sample count for water LOS."""

    water = app.raw_config.get("water_viewing", {}) or {}
    section = water.get("land_mask", {}) or {}
    land_buffer_m = max(0.0, float(section.get("land_buffer_m", 30.0)))
    target_samples = int(section.get("target_samples_per_cell", 3))
    if target_samples < 1 or target_samples > 63:
        raise ValueError("water_viewing.land_mask.target_samples_per_cell must be between 1 and 63")
    return land_buffer_m, target_samples


def _projected_land_domain_for_app(app: AppConfig, domain_geometries: domains.DomainGeometries):
    crs = str(app.viewshed.crs_projected)
    cache_key = (str(app.config_path), app.config_hash, crs, id(domain_geometries.land_domain))
    cached = _PROJECTED_LAND_DOMAIN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    transformer = Transformer.from_crs(CRS_WGS84, crs, always_xy=True)
    projected = shapely_transform(transformer.transform, domain_geometries.land_domain)
    _PROJECTED_LAND_DOMAIN_CACHE[cache_key] = projected
    return projected


def _projected_transformer_for_app(app: AppConfig) -> Transformer:
    """Return the shared WGS84-to-analysis transformer for one model config."""

    crs = str(app.viewshed.crs_projected)
    cache_key = (str(app.config_path), str(app.config_hash), crs)
    cached = _PROJECTED_TRANSFORMER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    transformer = Transformer.from_crs(CRS_WGS84, crs, always_xy=True)
    _PROJECTED_TRANSFORMER_CACHE[cache_key] = transformer
    return transformer


def _projected_h3_bound_for_app(
    app: AppConfig,
    cell: str,
) -> tuple[float, float, float]:
    """Return a projected H3 center and conservative enclosing radius.

    The bound is used only to prove that an entire source-target pair lies
    beyond the refracted horizon. It never declares a pair visible and cannot
    bypass the coastal/terrain ambiguity checks.
    """

    crs = str(app.viewshed.crs_projected)
    cache_key = (str(app.config_path), str(app.config_hash), crs, str(cell))
    cached = _PROJECTED_H3_BOUND_CACHE.get(cache_key)
    if cached is not None:
        return cached

    bounds_lookup = _projected_h3_bounds_for_app(app)
    if bounds_lookup is not None and str(cell) in bounds_lookup:
        result = bounds_lookup[str(cell)]
        _PROJECTED_H3_BOUND_CACHE[cache_key] = result
        return result

    projected_lookup = _cached_h3_geometry_for_app(app, "geometry_projected")
    polygon = projected_lookup.get(str(cell)) if projected_lookup is not None else None
    if polygon is None:
        transformer = _projected_transformer_for_app(app)
        polygon = shapely_transform(transformer.transform, cell_to_polygon(str(cell)))
    if polygon.is_empty:
        raise ValueError(f"Cannot construct projected H3 bound for empty cell: {cell}")
    center = polygon.centroid
    coordinates = np.asarray(polygon.exterior.coords, dtype="float64")
    radius_m = float(
        np.max(
            np.hypot(
                coordinates[:, 0] - float(center.x),
                coordinates[:, 1] - float(center.y),
            )
        )
    )
    # One metre protects the lower-bound test from affine/projection rounding.
    result = (float(center.x), float(center.y), radius_m + 1.0)
    _PROJECTED_H3_BOUND_CACHE[cache_key] = result
    return result


def _projected_h3_bounds_for_app(
    app: AppConfig,
) -> dict[str, tuple[float, float, float]] | None:
    """Return conservative projected bounds for every cached H3 geometry."""

    crs = str(app.viewshed.crs_projected)
    cache_key = (str(app.config_path), str(app.config_hash), crs)
    cached = _PROJECTED_H3_BOUNDS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    projected_lookup = _cached_h3_geometry_for_app(app, "geometry_projected")
    if projected_lookup is None:
        return None
    result: dict[str, tuple[float, float, float]] = {}
    for cell, polygon in projected_lookup.items():
        if polygon is None or polygon.is_empty:
            continue
        min_x, min_y, max_x, max_y = (float(value) for value in polygon.bounds)
        center_x = (min_x + max_x) * 0.5
        center_y = (min_y + max_y) * 0.5
        # The half-diagonal of the axis-aligned bounds encloses the complete
        # polygon. One metre protects the horizon proof from rounding.
        radius_m = math.hypot(max_x - min_x, max_y - min_y) * 0.5 + 1.0
        result[str(cell)] = (center_x, center_y, radius_m)
    _PROJECTED_H3_BOUNDS_CACHE[cache_key] = result
    return result


def _target_entirely_beyond_horizon(
    app: AppConfig,
    target_cell: str,
    *,
    projected_source_points: Sequence[Point],
    horizon_m: float,
    transition_m: float,
) -> bool:
    """Prove that every point in a target H3 cell is beyond the horizon."""

    if not math.isfinite(horizon_m) or not projected_source_points:
        return False
    try:
        center_x, center_y, target_radius_m = _projected_h3_bound_for_app(app, str(target_cell))
    except (TypeError, ValueError):
        # Test doubles and callers with invalid IDs must continue through the
        # authoritative kernel, which owns target-geometry validation.
        return False
    nearest_center_distance_m = min(
        math.hypot(float(point.x) - center_x, float(point.y) - center_y)
        for point in projected_source_points
    )
    minimum_possible_distance_m = max(0.0, nearest_center_distance_m - target_radius_m)
    return minimum_possible_distance_m > float(horizon_m) + float(transition_m)


def _projected_land_exclusion_for_app(
    app: AppConfig,
    domain_geometries: domains.DomainGeometries,
    land_clearance_m: float,
):
    """Return prepared land plus the configured coastal-clearance buffer."""

    projected_land = _projected_land_domain_for_app(app, domain_geometries)
    clearance_m = max(0.0, float(land_clearance_m))
    cache_key = (id(projected_land), clearance_m)
    cached = _PROJECTED_LAND_EXCLUSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    exclusion = projected_land.buffer(clearance_m) if clearance_m > 0.0 else projected_land
    shapely.prepare(exclusion)
    _PROJECTED_LAND_EXCLUSION_CACHE[cache_key] = exclusion
    return exclusion


def _water_geometry_sample_points(
    target_cell: str,
    geometry: Any,
    *,
    max_samples: int,
    projected_crs: str,
    geometry_crs: str = CRS_WGS84,
) -> list[Point]:
    """Return deterministic interior maximin samples in WGS84.

    ``sample_points_in_source_geometry`` performs its maximin design in the
    requested projected CRS but deliberately returns the selected points in
    WGS84.  Callers that perform metric calculations must project the returned
    points explicitly, even when ``geometry`` was already projected.
    """

    if geometry is None or geometry.is_empty:
        return []
    sampled = sample_points_in_source_geometry(
        str(target_cell),
        geometry,
        int(max_samples),
        include_centroid=True,
        geometry_crs=geometry_crs,
        projected_crs=projected_crs,
        max_design_points=int(max_samples),
    )
    return [point for point in sampled.geometry if point is not None and not point.is_empty]


def _projected_water_target_samples_for_app(
    app: AppConfig,
    target_cell: str,
    *,
    water_domain: Any,
    max_samples: int,
) -> tuple[Point, ...]:
    """Build each target's water samples once and reuse them across sources."""

    cache_key = (
        str(app.config_path),
        str(app.config_hash),
        str(app.viewshed.crs_projected),
        str(target_cell),
        int(max_samples),
        id(water_domain),
        id(_water_geometry_sample_points),
    )
    cached = _PROJECTED_WATER_TARGET_SAMPLE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    water_lookup = _cached_h3_geometry_for_app(app, "water_geometry_projected")
    target_polygon = water_lookup.get(str(target_cell)) if water_lookup is not None else None
    transformer = _projected_transformer_for_app(app)
    if target_polygon is not None:
        target_points = _water_geometry_sample_points(
            str(target_cell),
            target_polygon,
            max_samples=max_samples,
            projected_crs=app.viewshed.crs_projected,
            geometry_crs=app.viewshed.crs_projected,
        )
        projected = tuple(
            shapely_transform(transformer.transform, point) for point in target_points
        )
        _PROJECTED_WATER_TARGET_SAMPLE_CACHE[cache_key] = projected
        return projected

    target_polygon = cell_to_polygon(str(target_cell)).intersection(water_domain)
    target_points = _water_geometry_sample_points(
        str(target_cell),
        target_polygon,
        max_samples=max_samples,
        projected_crs=app.viewshed.crs_projected,
    )
    projected = tuple(shapely_transform(transformer.transform, point) for point in target_points)
    _PROJECTED_WATER_TARGET_SAMPLE_CACHE[cache_key] = projected
    return projected


def _water_sample_kernel_result(
    distances_m: np.ndarray,
    visible: np.ndarray,
    distance_weights: np.ndarray,
    *,
    target_water_area_m2: float,
) -> dict[str, Any]:
    """Summarize one sampled water source-target kernel."""

    weighted_visible = np.where(visible, distance_weights, 0.0)
    n_observers, n_target_samples = visible.shape
    pair_denominator = int(n_observers * n_target_samples)
    visible_pair_count = int(np.count_nonzero(visible))
    visible_targets = np.any(visible, axis=0)
    visible_observers = np.any(visible, axis=1)
    visible_target_count = int(np.count_nonzero(visible_targets))
    los_distance_weight_sum = float(np.sum(weighted_visible, dtype="float64"))
    joint_fraction = visible_pair_count / pair_denominator
    weighted_fraction = los_distance_weight_sum / pair_denominator
    union_fraction = visible_target_count / n_target_samples
    observer_fraction = float(np.count_nonzero(visible_observers)) / n_observers
    target_area_km2 = max(0.0, float(target_water_area_m2)) / 1_000_000.0

    return {
        "terrain_visible_clear_sky": bool(visible_pair_count > 0),
        "aggregation_method": "water_land_mask_sample_kernel_v1",
        "any_observer_support_fraction": np.float32(observer_fraction),
        "union_visible_target_fraction": np.float32(union_fraction),
        "joint_los_fraction": np.float32(joint_fraction),
        "distance_weighted_los_fraction": np.float32(weighted_fraction),
        "los_distance_weight_sum": np.float32(los_distance_weight_sum),
        "observer_sample_fraction": np.float32(observer_fraction),
        "visible_area_fraction": np.float32(union_fraction),
        "visible_sampled_pixel_count": visible_target_count,
        "visible_observer_pixel_count_sum": visible_pair_count,
        "n_observers": n_observers,
        "target_water_pixel_count": n_target_samples,
        "target_water_sample_count": n_target_samples,
        "pixel_stride": 1,
        "visible_area_km2_approx_sum": np.float32(union_fraction * target_area_km2),
        "target_water_area_km2": np.float32(target_area_km2),
        "terrain_visibility_support": np.float32(weighted_fraction),
    }


def _water_land_mask_supports_for_targets(
    app: AppConfig,
    *,
    target_cells: Sequence[str],
    projected_source_points: Sequence[Point],
    domain_geometries: domains.DomainGeometries,
    distance_weight_config: Any,
    distance_weight_max_km: float,
    target_area_lookup: Mapping[str, float],
) -> dict[str, dict[str, Any]]:
    """Evaluate all water targets for one source with one vectorized land test."""

    if not projected_source_points:
        raise ValueError("Water source has no valid projected sample points")
    land_buffer_m, max_target_samples = _water_land_mask_settings(app)
    horizon_m = refracted_horizon_distance_m(
        app.viewshed.observer_eye_height_m,
        app.viewshed.target_height_m,
        curvature_coefficient=app.viewshed.curvature_coefficient,
        earth_radius_m=app.viewshed.earth_radius_m,
    )
    source_xy = np.asarray(
        [(float(point.x), float(point.y)) for point in projected_source_points],
        dtype="float64",
    )
    bounded_targets = [str(cell) for cell in target_cells]
    bounds_lookup = _projected_h3_bounds_for_app(app)
    used_bulk_horizon_bounds = bool(
        math.isfinite(horizon_m)
        and bounds_lookup is not None
        and all(cell in bounds_lookup for cell in bounded_targets)
    )
    if used_bulk_horizon_bounds:
        bounds = np.asarray(
            [bounds_lookup[cell] for cell in bounded_targets],
            dtype="float64",
        )
        center_delta = source_xy[:, None, :] - bounds[None, :, :2]
        nearest_center_distance_m = np.min(
            np.hypot(center_delta[..., 0], center_delta[..., 1]),
            axis=0,
        )
        minimum_possible_distance_m = np.maximum(
            0.0,
            nearest_center_distance_m - bounds[:, 2],
        )
        bounded_targets = [
            cell
            for cell, keep in zip(
                bounded_targets,
                minimum_possible_distance_m <= horizon_m,
                strict=True,
            )
            if bool(keep)
        ]
    target_specs: list[tuple[str, tuple[int, int], int, int]] = []
    distance_chunks: list[np.ndarray] = []
    line_chunks: list[np.ndarray] = []
    offset = 0
    for target_cell in bounded_targets:
        if not used_bulk_horizon_bounds and _target_entirely_beyond_horizon(
            app,
            str(target_cell),
            projected_source_points=projected_source_points,
            horizon_m=horizon_m,
            transition_m=0.0,
        ):
            continue
        projected_targets = _projected_water_target_samples_for_app(
            app,
            str(target_cell),
            water_domain=domain_geometries.water_domain,
            max_samples=max_target_samples,
        )
        if not projected_targets:
            raise ValueError(f"Water target has no valid sample points: {target_cell}")
        target_xy = np.asarray(
            [(float(point.x), float(point.y)) for point in projected_targets],
            dtype="float64",
        )
        coordinate_delta = source_xy[:, None, :] - target_xy[None, :, :]
        distances = np.hypot(coordinate_delta[..., 0], coordinate_delta[..., 1])
        flat_count = int(distances.size)
        distance_chunks.append(distances.reshape(-1))
        line_chunks.append(
            np.stack(
                (
                    np.repeat(source_xy, len(target_xy), axis=0),
                    np.tile(target_xy, (len(source_xy), 1)),
                ),
                axis=1,
            )
        )
        target_specs.append(
            (str(target_cell), tuple(int(v) for v in distances.shape), offset, offset + flat_count)
        )
        offset += flat_count

    if not target_specs:
        return {}
    all_distances_m = np.concatenate(distance_chunks)
    all_visible = (
        np.ones(all_distances_m.shape, dtype=bool)
        if math.isinf(horizon_m)
        else all_distances_m < horizon_m
    )
    visible_indices = np.flatnonzero(all_visible)
    if visible_indices.size:
        all_lines = np.concatenate(line_chunks, axis=0)
        land = _projected_land_exclusion_for_app(
            app,
            domain_geometries,
            land_buffer_m,
        )
        all_visible[visible_indices] &= ~np.asarray(
            shapely.intersects(land, shapely.linestrings(all_lines[visible_indices])),
            dtype=bool,
        )
    all_weights = distance.distance_weight_values(
        all_distances_m / 1_000.0,
        distance_weight_config,
        max_distance_km=distance_weight_max_km,
    )

    results: dict[str, dict[str, Any]] = {}
    for target_cell, shape_, start, stop in target_specs:
        results[target_cell] = _water_sample_kernel_result(
            all_distances_m[start:stop].reshape(shape_),
            all_visible[start:stop].reshape(shape_),
            all_weights[start:stop].reshape(shape_),
            target_water_area_m2=target_area_lookup.get(target_cell, 0.0),
        )
    return results


def _cache_water_prefilter_result(
    key: tuple[Any, ...], result: tuple[pd.DataFrame, set[str] | None]
) -> None:
    """Retain a bounded FIFO cache for repeated single-source inspection."""
    if key not in _WATER_TERRAIN_PREFILTER_CACHE:
        while len(_WATER_TERRAIN_PREFILTER_CACHE) >= _WATER_TERRAIN_PREFILTER_CACHE_LIMIT:
            del _WATER_TERRAIN_PREFILTER_CACHE[next(iter(_WATER_TERRAIN_PREFILTER_CACHE))]
    _WATER_TERRAIN_PREFILTER_CACHE[key] = result


def _water_target_area_lookup(app: AppConfig, targets: Sequence[str]) -> dict[str, float]:
    """Materialize only this source's candidate denominators as Python objects.

    The regional frame is already cached. Converting its entire domain to
    dictionaries for every source causes quadratic object allocation and GC.
    Filtering first preserves the same canonical areas and bounds that work.
    """
    frame = domain_target_water_area_by_h3(app, app.h3.output_resolution)
    subset = frame.filter(pl.col("target_h3_cell").is_in(list(targets))).select(
        "target_h3_cell", "target_water_area_m2"
    )
    return {str(cell): float(area) for cell, area in subset.iter_rows()}


def _water_terrain_prefilter_for_source(
    app: AppConfig,
    source_cell: str,
    *,
    source_points_wgs84: Sequence[Point],
    target_distances_override: Mapping[str, float] | None = None,
) -> tuple[pd.DataFrame, set[str] | None]:
    """Return opaque-land-mask rows for one water source.

    The second tuple item is always an empty set for water sources: mapped land
    is authoritative and the water model never delegates to the DEM pipeline.
    Rows are intentionally lean; pair-level obstruction details belong in
    diagnostics rather than the production factor table.
    """

    if _source_type_for_app(app) != "water":
        return pd.DataFrame(), None

    lookup_path = _area_lookup_path_for_app(app)
    lookup_stat = lookup_path.stat()
    land_mask_settings = _water_land_mask_settings(app)
    source_point_signature = tuple(
        (round(float(point.x), 12), round(float(point.y), 12)) for point in source_points_wgs84
    )

    if target_distances_override is not None:
        target_distances = dict(target_distances_override)
    else:
        _, source_distances = load_batch_lookup(
            app,
            [str(source_cell)],
            include_distances=True,
        )
        target_distances = (source_distances or {}).get(str(source_cell), {})
    targets = sorted(target_distances)

    distance_digest = hashlib.blake2b(digest_size=16)
    for target_cell in targets:
        distance_digest.update(str(target_cell).encode("utf-8"))
        distance_digest.update(b"\0")
        distance_digest.update(struct.pack("!d", float(target_distances[str(target_cell)])))
    cache_key = (
        str(app.config_path),
        app.config_hash,
        str(source_cell),
        int(lookup_stat.st_mtime_ns),
        int(lookup_stat.st_size),
        float(app.viewshed.observer_eye_height_m),
        float(app.viewshed.target_height_m),
        float(app.viewshed.curvature_coefficient),
        float(app.viewshed.earth_radius_m),
        str(app.viewshed.crs_projected),
        land_mask_settings,
        source_point_signature,
        len(targets),
        distance_digest.digest(),
    )
    cached = _WATER_TERRAIN_PREFILTER_CACHE.get(cache_key)
    if cached is not None:
        rows, dem_targets = cached
        return rows.copy(), None if dem_targets is None else set(dem_targets)

    if not targets:
        out = (pd.DataFrame(), set())
        _cache_water_prefilter_result(cache_key, out)
        return pd.DataFrame(), set()

    domain_geometries = _water_terrain_domains_for_app(app)
    distance_weight_config = distance.load_distance_weight_config(app.raw_config)
    distance_weight_max_km = (
        float(distance_weight_config.hard_cutoff_km)
        if distance_weight_config.hard_cutoff_km is not None
        else float(app.viewshed.max_distance_m) / 1_000.0
    )
    target_area_lookup = _water_target_area_lookup(app, targets)
    open_rows: list[dict[str, Any]] = []
    dem_targets: set[str] = set()
    transformer = _projected_transformer_for_app(app)
    projected_source_points = [
        shapely_transform(transformer.transform, point)
        for point in source_points_wgs84
        if point is not None and not point.is_empty
    ]
    kernels = _water_land_mask_supports_for_targets(
        app,
        target_cells=targets,
        projected_source_points=projected_source_points,
        domain_geometries=domain_geometries,
        distance_weight_config=distance_weight_config,
        distance_weight_max_km=distance_weight_max_km,
        target_area_lookup=target_area_lookup,
    )
    for target_cell, kernel in kernels.items():
        # Terrain partitions are sparse. Zero-support pairs are represented by
        # absence and restored as zero when the authoritative lookup is joined
        # with the water factor table.
        if float(kernel["terrain_visibility_support"]) > 0.0:
            open_rows.append(
                {
                    "source_h3_cell": str(source_cell),
                    "target_h3_cell": str(target_cell),
                    **kernel,
                }
            )

    out_rows = pd.DataFrame(open_rows)
    out = (out_rows, dem_targets)
    _cache_water_prefilter_result(cache_key, out)
    return out_rows.copy(), set(dem_targets)


def _write_water_open_shortcut_partition(
    app: AppConfig,
    source_cell: str,
    open_water_rows: pd.DataFrame,
) -> Path:
    partition_path = _partition_path_for_source(app, source_cell)
    expected_metadata = expected_partition_metadata(app)
    partition_path.parent.mkdir(parents=True, exist_ok=True)
    combined_h3 = _add_partition_metadata_columns(open_water_rows.copy(), expected_metadata)
    partition_df = _terrain_weight_partition_for_storage(combined_h3)
    with timer(f"water_open_terrain_partition_write:{source_cell}"):
        partition_df.to_parquet(partition_path, index=False)
        _write_partition_metadata_sidecar(partition_path, expected_metadata)
    return partition_path


def _add_partition_metadata_columns(
    df: gpd.GeoDataFrame,
    expected: dict[str, Any],
) -> gpd.GeoDataFrame:
    out = df.copy()
    for column, value in expected.items():
        if len(out) == 0:
            dtype = "object"
            if isinstance(value, bool):
                dtype = "bool"
            elif isinstance(value, int):
                dtype = "int64"
            elif isinstance(value, float):
                dtype = "float64"
            out[column] = pd.Series(dtype=dtype)
        else:
            out[column] = value
    return out


def _terrain_weight_partition_for_storage(
    df: pd.DataFrame | gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Return the pair-level terrain factor and reproducibility diagnostics.

    ``weight_terrain`` is sourced from ``distance_weighted_los_fraction``. The
    persisted weighted numerator, denominators, and unweighted LOS diagnostics
    allow the terrain factor to be independently recomputed without rerunning
    GDAL.
    """

    index = getattr(df, "index", None)
    terrain_support = df.get(
        "distance_weighted_los_fraction",
        df.get(
            "joint_los_fraction",
            df.get(
                "terrain_visibility_support",
                pd.Series(index=index, dtype="float32"),
            ),
        ),
    )
    out = pd.DataFrame(
        {
            "source_h3": df.get(
                "source_h3_cell",
                pd.Series(index=index, dtype="string"),
            ).astype("string"),
            "target_h3": df.get(
                "target_h3_cell",
                pd.Series(index=index, dtype="string"),
            ).astype("string"),
            "weight_terrain": pd.to_numeric(
                terrain_support,
                errors="coerce",
            )
            .fillna(0.0)
            .clip(0.0, 1.0)
            .astype("float32"),
        }
    )
    # Keep clear-sky diagnostics with the sparse per-source partitions so final
    # materialization and scientific audits do not require rerunning GDAL.
    optional_numeric = {
        "any_observer_support_fraction": "any_observer_support_fraction",
        "union_visible_target_fraction": "union_visible_target_fraction",
        "joint_los_fraction": "joint_los_fraction",
        "distance_weighted_los_fraction": "distance_weighted_los_fraction",
        "los_distance_weight_sum": "los_distance_weight_sum",
        "observer_sample_fraction": "observer_sample_fraction",
        "visible_area_fraction": "visible_area_fraction",
        "visible_area_km2": "visible_area_km2_approx_sum",
        "target_water_area_km2": "target_water_area_km2",
    }
    for out_col, in_col in optional_numeric.items():
        if in_col in df.columns:
            out[out_col] = (
                pd.to_numeric(df[in_col], errors="coerce")
                .fillna(0.0)
                .clip(0.0, None)
                .astype("float32")
            )
    optional_integer = {
        "sample_points_requested": "sample_points_requested",
        "sample_points_actual": "sample_points_actual",
        "visible_sampled_pixel_count": "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum": "visible_observer_pixel_count_sum",
        "n_observers": "n_observers",
        "target_water_pixel_count": "target_water_pixel_count",
        "target_water_sample_count": "target_water_sample_count",
        "pixel_stride": "pixel_stride",
    }
    for out_col, in_col in optional_integer.items():
        if in_col in df.columns:
            out[out_col] = (
                pd.to_numeric(df[in_col], errors="coerce").fillna(0).clip(0, None).astype("int64")
            )
    if "terrain_visible_clear_sky" in df.columns:
        out["terrain_binary"] = df["terrain_visible_clear_sky"].fillna(False).astype(bool)
    else:
        out["terrain_binary"] = out["weight_terrain"] > 0
    if "aggregation_method" in df.columns:
        out["aggregation_method"] = df["aggregation_method"].fillna("dem_raster").astype("string")
    else:
        out["aggregation_method"] = pd.Series("dem_raster", index=out.index, dtype="string")
    ordered = [
        "source_h3",
        "target_h3",
        "terrain_binary",
        "aggregation_method",
        "any_observer_support_fraction",
        "union_visible_target_fraction",
        "joint_los_fraction",
        "distance_weighted_los_fraction",
        "los_distance_weight_sum",
        "observer_sample_fraction",
        "visible_area_fraction",
        "sample_points_requested",
        "sample_points_actual",
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
        "n_observers",
        "target_water_pixel_count",
        "target_water_sample_count",
        "pixel_stride",
        "visible_area_km2",
        "target_water_area_km2",
        "weight_terrain",
    ]
    integer_columns = {
        "sample_points_requested",
        "sample_points_actual",
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
        "n_observers",
        "target_water_pixel_count",
        "target_water_sample_count",
        "pixel_stride",
    }
    for col in ordered:
        if col in out.columns:
            continue
        if col == "terrain_binary":
            out[col] = pd.Series(False, index=out.index, dtype="bool")
        elif col in integer_columns:
            out[col] = pd.Series(0, index=out.index, dtype="int64")
        else:
            out[col] = pd.Series(np.float32(0.0), index=out.index, dtype="float32")
    out = out[ordered]
    if not out.empty:
        null_keys = out[["source_h3", "target_h3"]].isna().any(axis=1)
        if bool(null_keys.any()):
            raise ValueError(
                "Terrain partition contains null source_h3/target_h3 keys: "
                f"row_count={int(null_keys.sum())}"
            )
        duplicate_keys = out.duplicated(
            subset=["source_h3", "target_h3"],
            keep=False,
        )
        if bool(duplicate_keys.any()):
            sample = (
                out.loc[duplicate_keys, ["source_h3", "target_h3"]]
                .drop_duplicates()
                .head(10)
                .to_dict("records")
            )
            raise ValueError(
                "Terrain partition contains duplicate source-target pairs before write: "
                f"duplicate_row_count={int(duplicate_keys.sum())} sample={sample}"
            )
    return out


def _source_cumulative_paths(app: AppConfig, source_cell: str) -> tuple[Path, Path, Path, Path]:
    out_dir = app.paths.output_dir / "cumulative" / f"source_h3_cell={source_cell}"
    return (
        out_dir / "cumulative_visible_count.tif",
        out_dir / "cumulative_min_view_distance_m.tif",
        out_dir / "cumulative_mean_view_distance_m.tif",
        out_dir / "cumulative_max_view_distance_m.tif",
    )


def validate_batch_raster_alignment(context: BatchContext) -> None:
    """Validate obstacle, endpoint, canopy, and water rasters share one grid."""
    with (
        rasterio.open(context.analysis_dem_path) as dem,
        rasterio.open(context.endpoint_dem_path) as endpoint,
        rasterio.open(context.water_mask_path) as water,
    ):
        if dem.crs != water.crs:
            raise ValueError(
                "Analysis DEM and water mask CRS differ. Reproject/align upstream before terrain weights. "
                f"dem_crs={dem.crs} water_crs={water.crs} dem={context.analysis_dem_path} water={context.water_mask_path}"
            )
        if dem.width != water.width or dem.height != water.height:
            raise ValueError(
                "Analysis DEM and water mask shapes differ. Reproject/align upstream before terrain weights. "
                f"dem_shape=({dem.height},{dem.width}) water_shape=({water.height},{water.width})"
            )
        if not dem.transform.almost_equals(water.transform):
            raise ValueError(
                "Analysis DEM and water mask transforms differ. Reproject/align upstream before terrain weights. "
                f"dem_transform={dem.transform} water_transform={water.transform}"
            )
        if tuple(context.water_mask_arr.shape) != (water.height, water.width):
            raise ValueError(
                "Loaded water mask array shape does not match water mask raster shape. "
                f"array_shape={context.water_mask_arr.shape} raster_shape=({water.height},{water.width})"
            )
        if endpoint.crs != dem.crs:
            raise ValueError(
                "Endpoint DEM and analysis obstacle surface CRS differ. "
                f"endpoint_crs={endpoint.crs} analysis_crs={dem.crs}"
            )
        if endpoint.width != dem.width or endpoint.height != dem.height:
            raise ValueError(
                "Endpoint DEM and analysis obstacle surface shapes differ. "
                f"endpoint_shape=({endpoint.height},{endpoint.width}) "
                f"analysis_shape=({dem.height},{dem.width})"
            )
        if not endpoint.transform.almost_equals(dem.transform):
            raise ValueError(
                "Endpoint DEM and analysis obstacle surface transforms differ. "
                f"endpoint_transform={endpoint.transform} analysis_transform={dem.transform}"
            )
    if context.aligned_canopy_height_path is not None:
        with (
            rasterio.open(context.aligned_canopy_height_path) as canopy,
            rasterio.open(context.endpoint_dem_path) as endpoint,
        ):
            if (
                canopy.crs != endpoint.crs
                or canopy.width != endpoint.width
                or canopy.height != endpoint.height
                or not canopy.transform.almost_equals(endpoint.transform)
            ):
                raise ValueError(
                    "Aligned CHM does not match the endpoint DEM grid. "
                    f"chm={context.aligned_canopy_height_path} "
                    f"endpoint={context.endpoint_dem_path}"
                )
