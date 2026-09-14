"""Terrain clear-sky visibility support for viewshed weights.

Scientific role
---------------
This module estimates the clear-sky bare-earth viewing opportunity from
source-domain H3 cells to water-domain H3 target cells. It integrates the
configured distance detectability curve inside line-of-sight aggregation;
vegetation, weather, observer behavior, and animal availability remain separate.

The terrain factor is intentionally kept separate from later attenuation
layers. Its persisted output is a sparse pair table:

    source_h3
        Source-domain H3 cell.

    target_h3
        Water/target H3 cell.

    weight_terrain
        Mean ``LOS * D(distance)`` across every modeled observer and target-water
        pixel in [0, 1].

Method summary
--------------
For each source H3 cell, the pipeline samples one or more observer points,
runs a DEM-backed viewshed from each point, masks the result to water, and
aggregates visible water pixels into target H3 cells. Internally it keeps richer
diagnostics including any-observer support, union-visible target support, joint
LOS support, visible pixel counts, and near/mean/far view distances. Partitions
retain the weighted numerator, unweighted numerator, and denominators so the
weight can be audited.

Core assumptions
----------------
- The DEM is the terrain surface used for line-of-sight obstruction.
- Observer eye height and target height come from the active viewshed config.
- Source and target H3 resolutions are treated as aligned for this workflow.
- Water targets are derived from the configured water/land domain, not from a
  dense all-source/all-target matrix.
- `weight_terrain` is the modeled mean of LOS times distance detectability over
  observer/pixel pairs. It is clipped to [0, 1] before being written.

Downstream contract
-------------------
The final viewability stage multiplies this distance-integrated terrain factor
only by conditional vegetation attenuation:

    physical_view_score =
        weight_terrain * weight_vegetation

The H3-centroid ``weight_distance`` remains a diagnostic, not a second physical
multiplier.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import polars as pl
import rasterio

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon
from viewshed_toolkit.pipeline.weights.distance.compute import (
    distance_weight_values,
)

try:
    from numba import njit
except ImportError:  # pragma: no cover - exercised by the NumPy fallback
    njit = None

from viewshed_toolkit._internal.geo.geometry import CRS_WGS84

from ...config import (
    AppConfig,
    load_metadata_sidecar,
    write_metadata_sidecar,
)
from ...config.distance import load_distance_runtime
from ...contracts.artifacts import FINAL_SCHEMAS
from ...prepare.area.geometry import (
    ensure_h3_geometry_artifact,
    load_h3_geometry_frame,
)

LOGGER = logging.getLogger(__name__)
_NUMBA_AVAILABLE = njit is not None
_DISTANCE_MODEL_LOGISTIC = 0
_DISTANCE_MODEL_EXPONENTIAL = 1
_DISTANCE_MODEL_PIECEWISE = 2
DISTANCE_SUM_SCALE = 1_000
DISTANCE_WEIGHT_SUM_SCALE = 1_000_000
_EMPTY_OBSERVER_MASK = np.empty((0, 0), dtype="uint64")
_TARGET_WATER_AREA_BY_H3_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_TARGET_WATER_AREA_RUNTIME_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}


from .pixel_index import WaterPixelH3Index


def _file_stat_signature(path: Path) -> dict[str, str | int]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _file_signature(path: Path) -> dict[str, str | int]:
    resolved = Path(path).resolve()
    return {
        "path": str(resolved),
        "size": int(resolved.stat().st_size),
        "sha256": checksum_path(resolved),
    }


def _normalized_domain_target_water_area(frame: pl.DataFrame) -> pl.DataFrame:
    required = set(FINAL_SCHEMAS["target_water_area"])
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Target water-area artifact is missing columns: {missing}")
    return frame.select(
        pl.col("target_h3").cast(pl.Utf8).alias("target_h3_cell"),
        pl.col("equivalent_water_pixel_count").cast(pl.Int64).alias("target_water_pixel_count"),
        pl.col("total_water_area_m2").cast(pl.Float64).alias("target_water_area_m2"),
        (pl.col("total_water_area_m2").cast(pl.Float64) / 1_000_000.0).alias(
            "target_water_area_km2"
        ),
    )


def domain_target_water_area_by_h3(app: AppConfig, h3_resolution: int) -> pl.DataFrame:
    """Return immutable, domain-wide target-water denominators.

    Areas are intersections of complete target H3 geometries with the canonical
    water polygon, measured in a stable equal-area CRS. Batch rasters are never
    used for this denominator.
    """

    # Resolve experiment artifacts from the active AppConfig rather than
    # reloading app.config_path. Notebook-driven runs intentionally replace
    # output_dir and H3 settings in memory while retaining a stable base YAML.
    lookup_path = (
        app.paths.output_dir / "lookup" / f"SOURCE_TARGET_LOOKUP_H3R{int(h3_resolution)}.parquet"
    )
    output_path = (
        app.paths.output_dir / "lookup" / f"TARGET_WATER_AREA_H3R{int(h3_resolution)}.parquet"
    )
    water_viewing = app.raw_config.get("water_viewing", {}) or {}
    equal_area_crs = str(water_viewing.get("target_area_equal_area_crs", "EPSG:6933"))
    lookup_runtime_signature = _file_stat_signature(lookup_path)
    # This table is immutable for the life of a run.  Check the cheap runtime
    # key before scanning the complete source-target lookup or validating the
    # geometry artifact.  The previous ordering performed those expensive
    # operations once per source even though the resulting frame was cached.
    runtime_cache_key = (
        "target_water_area_runtime_v1",
        lookup_runtime_signature["path"],
        lookup_runtime_signature["mtime_ns"],
        lookup_runtime_signature["size"],
        int(h3_resolution),
        equal_area_crs,
        str(app.viewshed.crs_projected),
        int(app.viewshed.dem_resolution_m),
    )
    runtime_cached = _TARGET_WATER_AREA_RUNTIME_CACHE.get(runtime_cache_key)
    if runtime_cached is not None:
        return runtime_cached.clone()
    lookup_cells = (
        pl.concat(
            [
                pl.scan_parquet(str(lookup_path)).select(
                    pl.col("source_h3").cast(pl.Utf8).alias("h3_cell")
                ),
                pl.scan_parquet(str(lookup_path)).select(
                    pl.col("target_h3").cast(pl.Utf8).alias("h3_cell")
                ),
            ]
        )
        .unique()
        .collect()["h3_cell"]
        .to_list()
    )
    runtime = replace(
        load_distance_runtime(app.config_path),
        output_dir=app.paths.output_dir,
        source_resolution=int(h3_resolution),
        target_resolution=int(h3_resolution),
    )
    geometry_path = ensure_h3_geometry_artifact(
        runtime,
        lookup_cells,
        projected_crs=app.viewshed.crs_projected,
        equal_area_crs=equal_area_crs,
    )
    lookup_signature = _file_signature(lookup_path)
    inputs = {
        "lookup": lookup_signature,
        "h3_geometry": _file_signature(geometry_path),
        "h3_resolution": int(h3_resolution),
        "equal_area_crs": equal_area_crs,
        "nominal_pixel_size_m": int(app.viewshed.dem_resolution_m),
        "algorithm": "canonical_h3_geometry_water_area_v5_content_addressed",
    }
    cache_key = (json.dumps(inputs, sort_keys=True),)
    cached = _TARGET_WATER_AREA_BY_H3_CACHE.get(cache_key)
    if cached is not None:
        _TARGET_WATER_AREA_RUNTIME_CACHE[runtime_cache_key] = cached
        return cached.clone()

    metadata = load_metadata_sidecar(output_path)
    reusable = bool(
        output_path.exists()
        and metadata is not None
        and metadata.get("step") == "target_water_area_domain_v5"
        and metadata.get("inputs") == inputs
    )
    if reusable:
        out = _normalized_domain_target_water_area(pl.read_parquet(output_path))
        _TARGET_WATER_AREA_BY_H3_CACHE[cache_key] = out
        _TARGET_WATER_AREA_RUNTIME_CACHE[runtime_cache_key] = out
        return out.clone()

    target_cells = (
        pl.scan_parquet(str(lookup_path))
        .select(pl.col("target_h3").cast(pl.Utf8))
        .unique()
        .collect()["target_h3"]
        .to_list()
    )
    if not target_cells:
        raise ValueError(f"Canonical source-target lookup contains no target cells: {lookup_path}")

    geometry_frame = load_h3_geometry_frame(geometry_path).filter(
        pl.col("h3_cell").is_in(target_cells)
    )
    area_by_cell = dict(geometry_frame.select("h3_cell", "water_area_m2").iter_rows())
    missing_cells = sorted(set(target_cells) - set(area_by_cell))
    if missing_cells:
        raise ValueError(
            f"H3 geometry artifact is missing {len(missing_cells)} target cells: {geometry_path}"
        )
    nominal_pixel_area_m2 = float(app.viewshed.dem_resolution_m) ** 2
    rows: list[dict[str, Any]] = []
    for target_h3 in sorted(str(cell) for cell in target_cells):
        water_area_m2 = max(0.0, float(area_by_cell[target_h3]))
        equivalent_pixels = (
            max(1, int(round(water_area_m2 / nominal_pixel_area_m2))) if water_area_m2 > 0.0 else 0
        )
        rows.append(
            {
                "target_h3": target_h3,
                "total_water_area_m2": water_area_m2,
                "equivalent_water_pixel_count": equivalent_pixels,
            }
        )

    artifact = pl.DataFrame(rows).select(list(FINAL_SCHEMAS["target_water_area"]))
    atomic_sink_parquet(artifact.lazy(), output_path, overwrite=True)
    write_metadata_sidecar(
        output_path,
        app.raw_config,
        {
            "step": "target_water_area_domain_v5",
            "inputs": inputs,
            "crs": equal_area_crs,
            "target_resolution": int(h3_resolution),
            "area_method": "canonical_h3_geometry_water_area_m2",
            "pixel_count_method": ("rounded_equal_area_m2_divided_by_nominal_pixel_area_m2"),
        },
    )
    out = _normalized_domain_target_water_area(artifact)
    _TARGET_WATER_AREA_BY_H3_CACHE[cache_key] = out
    _TARGET_WATER_AREA_RUNTIME_CACHE[runtime_cache_key] = out
    LOGGER.info(
        "target_water_area_domain cache_store h3_resolution=%d rows=%d output=%s",
        int(h3_resolution),
        out.height,
        output_path,
    )
    return out.clone()


def cumulative_visible_arrays_to_h3(
    visible_count: np.ndarray,
    distance_weight_sum: np.ndarray,
    min_distance: np.ndarray,
    mean_distance: np.ndarray,
    max_distance: np.ndarray,
    observer_mask: np.ndarray | None,
    reference_raster_path: Path,
    h3_resolution: int,
    target_water_area: pl.DataFrame | None = None,
    pixel_stride: int = 1,
    aggregation_mode: str = "full",
    min_visible_sampled_pixel_count: int = 1,
    min_visible_area_km2: float = 0.0,
    include_geometry: bool = True,
    n_observer_points: int | None = None,
    sample_points_requested: int | None = None,
    sample_points_actual: int | None = None,
    allowed_target_h3: set[str] | Sequence[str] | None = None,
    row_offset: int = 0,
    col_offset: int = 0,
    pixel_h3_index: WaterPixelH3Index | None = None,
    reference_shape: tuple[int, int] | None = None,
    reference_crs: Any | None = None,
    pixel_area_m2: float | None = None,
    distance_weight_sum_scale: int = 1,
    sparse_global_rows: np.ndarray | None = None,
    sparse_global_cols: np.ndarray | None = None,
) -> pd.DataFrame | gpd.GeoDataFrame:
    def empty_result() -> pd.DataFrame | gpd.GeoDataFrame:
        columns = [
            "target_h3_cell",
            "visible_sampled_pixel_count",
            "visible_observer_pixel_count_sum",
            "los_distance_weight_sum",
            "visible_from_n_points",
            "nearest_view_distance_km",
            "mean_view_distance_km",
            "farthest_view_distance_km",
            "visible_area_km2_approx_sum",
            "target_water_area_km2",
            "any_observer_support_fraction",
            "union_visible_target_fraction",
            "joint_los_fraction",
            "distance_weighted_los_fraction",
            "observer_sample_fraction",
            "visible_area_fraction",
            "terrain_visibility_support",
            "sample_points_requested",
            "sample_points_actual",
        ]
        if include_geometry:
            columns.append("geometry")
            return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=CRS_WGS84)
        return pd.DataFrame(columns=columns)

    if reference_shape is None or reference_crs is None or pixel_area_m2 is None:
        with rasterio.open(reference_raster_path) as src:
            if src.crs is None:
                raise ValueError(f"Reference raster has no CRS: {reference_raster_path}")
            reference_shape = (int(src.height), int(src.width))
            reference_crs = src.crs
            pixel_area_m2 = abs(float(src.transform.a * src.transform.e))

    if aggregation_mode not in {"sampled", "full"}:
        raise ValueError("aggregation_mode must be one of: sampled, full")
    stride = 1 if aggregation_mode == "full" else max(1, int(pixel_stride))
    sparse_mode = sparse_global_rows is not None or sparse_global_cols is not None
    if sparse_mode:
        if sparse_global_rows is None or sparse_global_cols is None:
            raise ValueError("Sparse accumulator rows and columns must be provided together.")
        if visible_count.ndim != 1:
            raise ValueError("Sparse accumulator values must be one-dimensional.")
        sparse_length = len(visible_count)
        arrays = (
            distance_weight_sum,
            min_distance,
            mean_distance,
            max_distance,
            sparse_global_rows,
            sparse_global_cols,
        )
        if any(len(array) != sparse_length for array in arrays):
            raise ValueError("Sparse accumulator columns must have identical lengths.")
        if observer_mask is not None and len(observer_mask) != sparse_length:
            raise ValueError("Sparse observer-mask values must match the accumulator length.")
        rows = np.flatnonzero(visible_count > 0)
        if stride > 1:
            rows = rows[
                (sparse_global_rows[rows] % stride == 0) & (sparse_global_cols[rows] % stride == 0)
            ]
        cols = None
    elif stride == 1:
        # Full aggregation is the final-product path; avoid allocating two
        # dense index grids just to keep every visible water pixel.
        rows, cols = np.where(visible_count > 0)
    else:
        local_row_idx, local_col_idx = np.indices(visible_count.shape, sparse=False)
        global_row_idx = local_row_idx + int(row_offset)
        global_col_idx = local_col_idx + int(col_offset)
        sample_mask = (global_row_idx % stride == 0) & (global_col_idx % stride == 0)
        rows, cols = np.where((visible_count > 0) & sample_mask)

    if len(rows) == 0:
        return empty_result()
    if int(distance_weight_sum_scale) <= 0:
        raise ValueError("distance_weight_sum_scale must be positive")
    quantized_distance_weights = int(distance_weight_sum_scale) != 1

    selected = rows if sparse_mode else (rows, cols)
    if sparse_mode:
        global_rows = sparse_global_rows[rows]
        global_cols = sparse_global_cols[rows]
    else:
        global_rows = rows + int(row_offset)
        global_cols = cols + int(col_offset)

    visible_values: dict[str, Any] = {
        "row": global_rows.astype("int32", copy=False),
        "col": global_cols.astype("int32", copy=False),
        "visible_count": visible_count[selected].astype("uint16", copy=False),
        "visible_distance_weight_sum": (
            distance_weight_sum[selected].astype("int64", copy=False)
            if quantized_distance_weights
            else distance_weight_sum[selected].astype("float32", copy=False)
        ),
        "observer_mask": (
            observer_mask[selected].astype("uint64", copy=False)
            if observer_mask is not None
            else np.zeros(len(rows), dtype="uint64")
        ),
        "min_view_distance_km": (min_distance[selected].astype("float32", copy=False) / 1_000),
        "mean_view_distance_km": (mean_distance[selected].astype("float32", copy=False) / 1_000),
        "max_view_distance_km": (max_distance[selected].astype("float32", copy=False) / 1_000),
    }

    if pixel_h3_index is None:
        raise ValueError(
            "cumulative_visible_arrays_to_h3 requires a WaterPixelH3Index; "
            "pixel-level H3 string mapping is not supported"
        )
    if pixel_h3_index.code_grid.shape != reference_shape:
        raise ValueError(
            "Water-pixel H3 index is not aligned to the reference raster: "
            f"index_shape={pixel_h3_index.code_grid.shape} reference={reference_raster_path}"
        )
    codes = pixel_h3_index.code_grid[global_rows, global_cols]
    keep = codes >= 0
    if allowed_target_h3 is not None:
        allowed_cells = frozenset(str(cell) for cell in allowed_target_h3)
        # The final element is a false sentinel. Code grids use -1 for
        # non-water pixels, so negative indexing maps -1 to the sentinel.
        allowed_code_mask = np.zeros(len(pixel_h3_index.h3_cells) + 1, dtype=bool)
        allowed_code_mask[:-1] = np.fromiter(
            (h3_cell in allowed_cells for h3_cell in pixel_h3_index.h3_cells),
            dtype=bool,
            count=len(pixel_h3_index.h3_cells),
        )
        keep = allowed_code_mask[codes]
    if not np.any(keep):
        return empty_result()
    for column in tuple(visible_values):
        visible_values[column] = visible_values[column][keep]
    visible_values["h3_code"] = codes[keep].astype("int32", copy=False)
    visible_df = pl.DataFrame(visible_values).drop("row", "col")
    h3_group_column = "h3_code"

    if visible_df.height == 0:
        return empty_result()

    LOGGER.debug(
        "h3_visible_aggregation path=%s h3_resolution=%d visible_sampled_pixels=%d allowed_filter=%s",
        reference_raster_path,
        int(h3_resolution),
        visible_df.height,
        allowed_target_h3 is not None,
    )

    aggregation_exprs = [
        pl.len().alias("visible_sampled_pixel_count"),
        pl.col("visible_count").sum().alias("visible_observer_pixel_count_sum"),
        pl.col("visible_distance_weight_sum").sum().alias("los_distance_weight_sum"),
        pl.col("visible_count").max().alias("visible_from_n_points_approx"),
        pl.col("min_view_distance_km").min().alias("nearest_view_distance_km"),
        pl.col("mean_view_distance_km").mean().alias("mean_view_distance_km"),
        pl.col("max_view_distance_km").max().alias("farthest_view_distance_km"),
    ]
    if observer_mask is not None:
        aggregation_exprs.append(pl.col("observer_mask").bitwise_or().alias("observer_mask"))
    grouped_pl = visible_df.group_by(h3_group_column).agg(*aggregation_exprs)
    if quantized_distance_weights:
        grouped_pl = grouped_pl.with_columns(
            (
                pl.col("los_distance_weight_sum").cast(pl.Float64)
                / float(distance_weight_sum_scale)
            ).alias("los_distance_weight_sum")
        )
    # Convert only grouped target codes. Pixel-level operations stay int32.
    grouped_codes = grouped_pl["h3_code"].to_numpy()
    grouped_pl = grouped_pl.with_columns(
        pl.Series(
            "h3_cell",
            [pixel_h3_index.h3_cells[int(code)] for code in grouped_codes],
            dtype=pl.Utf8,
        )
    ).drop("h3_code")

    if observer_mask is not None:
        grouped_pl = grouped_pl.with_columns(
            pl.col("observer_mask")
            .bitwise_count_ones()
            .cast(pl.Int64)
            .alias("visible_from_n_points")
        )
    else:
        grouped_pl = grouped_pl.with_columns(
            pl.col("visible_from_n_points_approx").alias("visible_from_n_points")
        )

    if target_water_area is None:
        raise ValueError(
            "cumulative_visible_arrays_to_h3 requires the immutable domain-wide "
            "target_water_area table; batch-raster denominators are not allowed."
        )
    required_area_columns = {
        "target_h3_cell",
        "target_water_pixel_count",
        "target_water_area_m2",
        "target_water_area_km2",
    }
    missing_area_columns = sorted(required_area_columns - set(target_water_area.columns))
    if missing_area_columns:
        raise ValueError(
            "Domain-wide target water-area table is missing columns: " f"{missing_area_columns}"
        )
    water_area = target_water_area.select(sorted(required_area_columns))
    if allowed_target_h3 is not None:
        allowed_values = [str(cell) for cell in allowed_target_h3]
        water_area = water_area.filter(pl.col("target_h3_cell").is_in(allowed_values))

    visible_targets = grouped_pl.select(pl.col("h3_cell").cast(pl.Utf8)).unique()
    missing_denominators = visible_targets.join(
        water_area.select(pl.col("target_h3_cell").alias("h3_cell")),
        on="h3_cell",
        how="anti",
    )
    if missing_denominators.height:
        preview = missing_denominators["h3_cell"].head(10).to_list()
        raise ValueError(
            "Visible target H3 cells are missing immutable water-area denominators: "
            f"count={missing_denominators.height} first={preview}"
        )
    nonpositive_denominators = visible_targets.join(
        water_area.select(
            pl.col("target_h3_cell").alias("h3_cell"),
            "target_water_area_m2",
        ),
        on="h3_cell",
        how="inner",
    ).filter(pl.col("target_water_area_m2") <= 0.0)
    if nonpositive_denominators.height:
        preview = nonpositive_denominators["h3_cell"].head(10).to_list()
        raise ValueError(
            "Visible target H3 cells have non-positive immutable water areas: "
            f"count={nonpositive_denominators.height} first={preview}"
        )

    grouped_pl = (
        grouped_pl.with_columns(
            (
                pl.col("visible_sampled_pixel_count").cast(pl.Float64)
                * float(pixel_area_m2)
                * int(stride)
                * int(stride)
                / 1_000_000.0
            ).alias("visible_area_km2_approx_sum")
        )
        .join(
            water_area,
            left_on="h3_cell",
            right_on="target_h3_cell",
            how="left",
        )
        .with_columns(
            pl.col("target_water_pixel_count").cast(pl.Int64),
            pl.col("target_water_area_m2").cast(pl.Float64),
            pl.col("target_water_area_km2").cast(pl.Float64),
            pl.lit(int(h3_resolution)).alias("target_h3_resolution"),
            pl.lit(int(stride)).alias("pixel_stride"),
            pl.lit(str(aggregation_mode)).alias("aggregation_mode"),
            pl.lit(sample_points_requested, dtype=pl.Int64).alias("sample_points_requested"),
            pl.lit(sample_points_actual, dtype=pl.Int64).alias("sample_points_actual"),
        )
        .rename({"h3_cell": "target_h3_cell"})
        .filter(
            (pl.col("visible_sampled_pixel_count") >= min_visible_sampled_pixel_count)
            & (pl.col("visible_area_km2_approx_sum") >= min_visible_area_km2)
        )
    )

    if grouped_pl.collect_schema().names() and grouped_pl.height == 0:
        grouped = grouped_pl.to_pandas()
        if include_geometry:
            return gpd.GeoDataFrame(grouped, geometry=[], crs=CRS_WGS84)
        return grouped

    grouped_pl = add_terrain_visibility_support(
        grouped_pl,
        n_observer_points=n_observer_points,
    )
    grouped = grouped_pl.sort(
        ["visible_from_n_points", "nearest_view_distance_km"],
        descending=[True, False],
    ).to_pandas()
    if not include_geometry:
        return grouped
    return gpd.GeoDataFrame(
        grouped,
        geometry=grouped["target_h3_cell"].apply(cell_to_polygon),
        crs=CRS_WGS84,
    )


def add_terrain_visibility_support(
    df: pl.DataFrame,
    *,
    n_observer_points: int | None = None,
) -> pl.DataFrame:
    """Add terrain-support diagnostics while retaining the grouped table in Polars."""

    out = df
    schema = set(out.columns)

    def numeric_expr(column: str, default: float = 0.0) -> pl.Expr:
        if column not in schema:
            return pl.lit(default, dtype=pl.Float64)
        return pl.col(column).cast(pl.Float64, strict=False).fill_nan(default).fill_null(default)

    if "terrain_visible_clear_sky" not in schema:
        out = out.with_columns(pl.lit(True).alias("terrain_visible_clear_sky"))
        schema.add("terrain_visible_clear_sky")

    observer_candidates: list[pl.Expr] = []
    for column in (
        "sample_points_actual",
        "sample_points_requested",
        "sample_points_per_source_cell",
    ):
        if column in schema:
            observer_candidates.append(pl.col(column).cast(pl.Float64, strict=False).fill_nan(None))
    if n_observer_points is not None:
        observer_candidates.append(pl.lit(float(n_observer_points)))
    observer_candidates.append(pl.lit(1.0))
    out = out.with_columns(pl.coalesce(observer_candidates).alias("_observer_denominator"))
    invalid_observers = out.filter(
        pl.col("_observer_denominator").is_null() | (pl.col("_observer_denominator") <= 0.0)
    )
    if invalid_observers.height:
        raise ValueError(
            "Joint terrain LOS requires a positive observer count for every target row."
        )

    out = out.with_columns(
        pl.col("_observer_denominator").cast(pl.Int64).alias("n_observers"),
        numeric_expr("visible_from_n_points").alias("_visible_from_n_points"),
        numeric_expr("visible_area_km2_approx_sum").alias("_visible_area"),
    )
    impossible = out.filter(pl.col("_visible_from_n_points") > pl.col("_observer_denominator"))
    if impossible.height:
        preview_columns = [
            column
            for column in (
                "source_h3_cell",
                "target_h3_cell",
                "visible_from_n_points",
                "sample_points_actual",
                "sample_points_requested",
            )
            if column in impossible.columns
        ]
        preview = impossible.select(preview_columns).head(10).to_dicts()
        raise ValueError(
            "Visible observer count exceeds the selected source-sampling denominator: " f"{preview}"
        )

    out = out.with_columns(
        (pl.col("_visible_from_n_points") / pl.col("_observer_denominator"))
        .fill_nan(0.0)
        .fill_null(0.0)
        .clip(0.0, 1.0)
        .alias("any_observer_support_fraction")
    )
    if "target_water_area_km2" in schema:
        out = out.with_columns(
            pl.when(numeric_expr("target_water_area_km2") > 0.0)
            .then(pl.col("_visible_area") / numeric_expr("target_water_area_km2"))
            .otherwise(0.0)
            .fill_nan(0.0)
            .fill_null(0.0)
            .clip(0.0, 1.0)
            .alias("union_visible_target_fraction")
        )
    else:
        max_area = float(out["_visible_area"].max() or 0.0)
        denominator = max(max_area, 1e-12)
        out = out.with_columns(
            (pl.col("_visible_area") / denominator)
            .fill_nan(0.0)
            .fill_null(0.0)
            .clip(0.0, 1.0)
            .alias("union_visible_target_fraction")
        )

    required_joint_columns = {
        "visible_observer_pixel_count_sum",
        "los_distance_weight_sum",
        "target_water_pixel_count",
        "pixel_stride",
    }
    missing_joint_columns = sorted(required_joint_columns - schema)
    if missing_joint_columns:
        raise ValueError(
            "Joint terrain LOS is missing numerator/denominator columns: "
            f"{missing_joint_columns}"
        )
    out = out.with_columns(
        numeric_expr("visible_observer_pixel_count_sum").alias("_visible_pair_count"),
        numeric_expr("los_distance_weight_sum").alias("_distance_weight_sum"),
        numeric_expr("target_water_pixel_count").alias("_target_water_pixels"),
        numeric_expr("pixel_stride").alias("_pixel_stride"),
    )
    invalid_denominators = out.filter(
        (pl.col("_target_water_pixels") <= 0.0) | (pl.col("_pixel_stride") <= 0.0)
    )
    if invalid_denominators.height:
        raise ValueError(
            "Joint terrain LOS requires positive target-water pixel counts and pixel strides."
        )

    visible = pl.col("terrain_visible_clear_sky").cast(pl.Boolean, strict=False).fill_null(False)
    denominator = pl.col("_observer_denominator") * pl.col("_target_water_pixels")
    stride_area = pl.col("_pixel_stride").pow(2)
    out = out.with_columns(
        pl.when(visible)
        .then(pl.col("_visible_pair_count") * stride_area / denominator)
        .otherwise(0.0)
        .fill_nan(0.0)
        .fill_null(0.0)
        .clip(0.0, 1.0)
        .alias("joint_los_fraction"),
        pl.when(visible)
        .then(pl.col("_distance_weight_sum") * stride_area / denominator)
        .otherwise(0.0)
        .fill_nan(0.0)
        .fill_null(0.0)
        .clip(0.0, 1.0)
        .alias("distance_weighted_los_fraction"),
    ).with_columns(
        pl.col("distance_weighted_los_fraction").alias("terrain_visibility_support"),
        pl.col("any_observer_support_fraction").alias("observer_sample_fraction"),
        pl.col("union_visible_target_fraction").alias("visible_area_fraction"),
    )
    return out.drop(
        "_observer_denominator",
        "_visible_from_n_points",
        "_visible_area",
        "_visible_pair_count",
        "_distance_weight_sum",
        "_target_water_pixels",
        "_pixel_stride",
    )


# -----------------------------------------------------------------------------
# Source-cell processing within shared batch DEM
# -----------------------------------------------------------------------------


def _init_cumulative_arrays(shape_: tuple[int, int], n_observers: int) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray | None,
]:
    visible_count = np.zeros(shape_, dtype="uint16")
    min_distance = np.full(shape_, np.inf, dtype="float32")
    max_distance = np.zeros(shape_, dtype="float32")
    distance_sum = np.zeros(shape_, dtype="int64")
    distance_weight_sum = np.zeros(shape_, dtype="int64")
    observer_mask = np.zeros(shape_, dtype="uint64") if n_observers <= 63 else None
    return (
        visible_count,
        min_distance,
        max_distance,
        distance_sum,
        distance_weight_sum,
        observer_mask,
    )


def _distance_kernel_parameters(
    distance_weight_config: Any,
) -> tuple[int, float, float, float, float, float, float]:
    """Return primitive distance-model parameters accepted by the JIT kernel."""

    selected = str(distance_weight_config.selected_model).lower().strip()
    if selected == "logistic":
        model_code = _DISTANCE_MODEL_LOGISTIC
    elif selected == "exponential":
        model_code = _DISTANCE_MODEL_EXPONENTIAL
    elif selected == "piecewise":
        model_code = _DISTANCE_MODEL_PIECEWISE
    else:
        raise ValueError(f"Unsupported distance model: {distance_weight_config.selected_model}")

    logistic_d50_km = float(distance_weight_config.logistic_d50_km)
    logistic_slope_km = float(distance_weight_config.logistic_slope_km)
    logistic_zero_weight = 1.0
    if selected == "logistic" and bool(getattr(distance_weight_config, "normalize_at_zero", False)):
        logistic_zero_weight = 1.0 / (1.0 + math.exp(-logistic_d50_km / logistic_slope_km))
    piecewise_near_km = float(
        distance_weight_config.piecewise_near_km
        if distance_weight_config.piecewise_near_km is not None
        else distance_weight_config.piecewise_full_weight_km
    )
    piecewise_far_km = float(
        distance_weight_config.piecewise_far_km
        if distance_weight_config.piecewise_far_km is not None
        else distance_weight_config.piecewise_zero_weight_km
    )
    return (
        model_code,
        logistic_d50_km,
        logistic_slope_km,
        logistic_zero_weight,
        float(distance_weight_config.exponential_lambda_km),
        piecewise_near_km,
        piecewise_far_km,
    )


def _accumulate_visible_numpy(
    visible: np.ndarray,
    transform,
    observer_x: float,
    observer_y: float,
    sample_index: int,
    visible_count: np.ndarray,
    min_distance: np.ndarray,
    max_distance: np.ndarray,
    distance_sum: np.ndarray,
    distance_weight_sum: np.ndarray,
    distance_weight_config: Any,
    distance_weight_max_km: float,
    observer_mask: np.ndarray | None,
    row_offset: int = 0,
    col_offset: int = 0,
    storage_row_offset: int | None = None,
    storage_col_offset: int | None = None,
) -> None:
    """Reference NumPy implementation retained for parity and fallback."""

    local_rows, local_cols = np.where(visible)
    if len(local_rows) == 0:
        return

    # Global rows/cols are used to compute distances against the parent raster
    # transform. Storage rows/cols may be local to a cropped union window.
    rows = local_rows + int(row_offset)
    cols = local_cols + int(col_offset)
    if storage_row_offset is None:
        store_rows = rows
    else:
        store_rows = local_rows + int(storage_row_offset)
    if storage_col_offset is None:
        store_cols = cols
    else:
        store_cols = local_cols + int(storage_col_offset)

    xs_arr = (
        transform.c
        + (cols.astype("float32") + np.float32(0.5)) * np.float32(transform.a)
        + (rows.astype("float32") + np.float32(0.5)) * np.float32(transform.b)
    )
    ys_arr = (
        transform.f
        + (cols.astype("float32") + np.float32(0.5)) * np.float32(transform.d)
        + (rows.astype("float32") + np.float32(0.5)) * np.float32(transform.e)
    )
    distances = np.sqrt((xs_arr - observer_x) ** 2 + (ys_arr - observer_y) ** 2).astype("float32")
    distance_weights = distance_weight_values(
        distances.astype("float64") / 1_000.0,
        distance_weight_config,
        max_distance_km=distance_weight_max_km,
    )

    visible_count[store_rows, store_cols] += 1
    distance_sum[store_rows, store_cols] += np.floor(
        distances.astype("float64") * DISTANCE_SUM_SCALE + 0.5
    ).astype("int64")
    distance_weight_sum[store_rows, store_cols] += np.floor(
        distance_weights.astype("float64") * DISTANCE_WEIGHT_SUM_SCALE + 0.5
    ).astype("int64")
    min_distance[store_rows, store_cols] = np.minimum(
        min_distance[store_rows, store_cols], distances
    )
    max_distance[store_rows, store_cols] = np.maximum(
        max_distance[store_rows, store_cols], distances
    )

    if observer_mask is not None:
        observer_mask[store_rows, store_cols] = np.bitwise_or(
            observer_mask[store_rows, store_cols],
            np.uint64(1 << (sample_index - 1)),
        )


if njit is not None:

    @njit(cache=True, nogil=True)
    def _accumulate_visible_compiled_kernel(
        visible,
        transform_c,
        transform_a,
        transform_b,
        transform_f,
        transform_d,
        transform_e,
        observer_x,
        observer_y,
        sample_bit,
        visible_count,
        min_distance,
        max_distance,
        distance_sum,
        distance_weight_sum,
        observer_mask,
        update_observer_mask,
        row_offset,
        col_offset,
        storage_row_offset,
        storage_col_offset,
        model_code,
        logistic_d50_km,
        logistic_slope_km,
        logistic_zero_weight,
        exponential_lambda_km,
        piecewise_near_km,
        piecewise_far_km,
        distance_weight_max_km,
    ):
        """Fuse visible-pixel distance, weighting, and accumulator updates."""

        transform_c32 = np.float32(transform_c)
        transform_a32 = np.float32(transform_a)
        transform_b32 = np.float32(transform_b)
        transform_f32 = np.float32(transform_f)
        transform_d32 = np.float32(transform_d)
        transform_e32 = np.float32(transform_e)
        observer_x32 = np.float32(observer_x)
        observer_y32 = np.float32(observer_y)
        half = np.float32(0.5)

        for local_row in range(visible.shape[0]):
            for local_col in range(visible.shape[1]):
                if not visible[local_row, local_col]:
                    continue

                global_row = local_row + row_offset
                global_col = local_col + col_offset
                if storage_row_offset < 0:
                    store_row = global_row
                else:
                    store_row = local_row + storage_row_offset
                if storage_col_offset < 0:
                    store_col = global_col
                else:
                    store_col = local_col + storage_col_offset

                global_row32 = np.float32(global_row)
                global_col32 = np.float32(global_col)
                pixel_x = np.float32(
                    transform_c32
                    + (global_col32 + half) * transform_a32
                    + (global_row32 + half) * transform_b32
                )
                pixel_y = np.float32(
                    transform_f32
                    + (global_col32 + half) * transform_d32
                    + (global_row32 + half) * transform_e32
                )
                delta_x = np.float32(pixel_x - observer_x32)
                delta_y = np.float32(pixel_y - observer_y32)
                distance_m = np.float32(np.sqrt(np.float32(delta_x * delta_x + delta_y * delta_y)))
                distance_km = float(distance_m) / 1_000.0

                if model_code == _DISTANCE_MODEL_LOGISTIC:
                    weight = 1.0 / (
                        1.0 + math.exp((distance_km - logistic_d50_km) / logistic_slope_km)
                    )
                    weight = weight / logistic_zero_weight
                elif model_code == _DISTANCE_MODEL_EXPONENTIAL:
                    weight = math.exp(-distance_km / exponential_lambda_km)
                else:
                    if distance_km <= piecewise_near_km:
                        weight = 1.0
                    elif distance_km >= piecewise_far_km:
                        weight = 0.0
                    else:
                        weight = 1.0 - (
                            (distance_km - piecewise_near_km)
                            / (piecewise_far_km - piecewise_near_km)
                        )

                if weight < 0.0:
                    weight = 0.0
                elif weight > 1.0:
                    weight = 1.0
                if distance_km > distance_weight_max_km:
                    weight = 0.0
                quantized_distance = np.int64(
                    math.floor(float(distance_m) * DISTANCE_SUM_SCALE + 0.5)
                )
                quantized_weight = np.int64(math.floor(weight * DISTANCE_WEIGHT_SUM_SCALE + 0.5))

                visible_count[store_row, store_col] += np.uint16(1)
                distance_sum[store_row, store_col] += quantized_distance
                distance_weight_sum[store_row, store_col] += quantized_weight
                if distance_m < min_distance[store_row, store_col]:
                    min_distance[store_row, store_col] = distance_m
                if distance_m > max_distance[store_row, store_col]:
                    max_distance[store_row, store_col] = distance_m
                if update_observer_mask:
                    observer_mask[store_row, store_col] = np.bitwise_or(
                        observer_mask[store_row, store_col],
                        sample_bit,
                    )

else:  # pragma: no cover - depends on the optional runtime dependency
    _accumulate_visible_compiled_kernel = None


def _accumulate_visible(
    visible: np.ndarray,
    transform,
    observer_x: float,
    observer_y: float,
    sample_index: int,
    visible_count: np.ndarray,
    min_distance: np.ndarray,
    max_distance: np.ndarray,
    distance_sum: np.ndarray,
    distance_weight_sum: np.ndarray,
    distance_weight_config: Any,
    distance_weight_max_km: float,
    observer_mask: np.ndarray | None,
    row_offset: int = 0,
    col_offset: int = 0,
    storage_row_offset: int | None = None,
    storage_col_offset: int | None = None,
    *,
    use_compiled: bool | None = None,
) -> None:
    """Accumulate one observer result using the fused kernel when available."""

    compiled = _NUMBA_AVAILABLE if use_compiled is None else bool(use_compiled)
    if compiled and _accumulate_visible_compiled_kernel is None:
        raise RuntimeError("Compiled terrain accumulation requires numba.")
    if not compiled:
        _accumulate_visible_numpy(
            visible,
            transform,
            observer_x,
            observer_y,
            sample_index,
            visible_count,
            min_distance,
            max_distance,
            distance_sum,
            distance_weight_sum,
            distance_weight_config,
            distance_weight_max_km,
            observer_mask,
            row_offset=row_offset,
            col_offset=col_offset,
            storage_row_offset=storage_row_offset,
            storage_col_offset=storage_col_offset,
        )
        return

    (
        model_code,
        logistic_d50_km,
        logistic_slope_km,
        logistic_zero_weight,
        exponential_lambda_km,
        piecewise_near_km,
        piecewise_far_km,
    ) = _distance_kernel_parameters(distance_weight_config)
    _accumulate_visible_compiled_kernel(
        np.asarray(visible, dtype=bool),
        float(transform.c),
        float(transform.a),
        float(transform.b),
        float(transform.f),
        float(transform.d),
        float(transform.e),
        float(observer_x),
        float(observer_y),
        np.uint64(0 if observer_mask is None else 1 << (sample_index - 1)),
        visible_count,
        min_distance,
        max_distance,
        distance_sum,
        distance_weight_sum,
        observer_mask if observer_mask is not None else _EMPTY_OBSERVER_MASK,
        observer_mask is not None,
        int(row_offset),
        int(col_offset),
        -1 if storage_row_offset is None else int(storage_row_offset),
        -1 if storage_col_offset is None else int(storage_col_offset),
        int(model_code),
        float(logistic_d50_km),
        float(logistic_slope_km),
        float(logistic_zero_weight),
        float(exponential_lambda_km),
        float(piecewise_near_km),
        float(piecewise_far_km),
        float(distance_weight_max_km),
    )
