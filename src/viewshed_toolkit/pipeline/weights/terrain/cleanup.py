"""Radius-viewshed execution for a bare-earth or canopy obstacle surface.

Each observer sample runs one radius-based GDAL viewshed. Water pixels are
aggregated to target H3 cells with observer-to-pixel distance decay inside the
kernel:

    weight_surface = mean_observer,target_pixel(LOS * D(distance))

For the bare-earth run this is persisted as `weight_terrain`. A matched
DTM+CHM run is persisted separately and converted to conditional canopy
attenuation. These are physical support factors in [0, 1]; sums across source
cells are opportunity indices, not probabilities.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
from viewshed_toolkit._internal.data.parquet import validate_parquet_schema

from ...config import (
    AppConfig,
    BatchContext,
    metadata_sidecar_candidates,
)
from ...contracts.artifacts import (
    FINAL_SCHEMAS,
    final_artifact_paths,
)
from ...prepare.area import domains

LOGGER = logging.getLogger(__name__)
_AREA_LOOKUP_TARGET_CACHE: dict[tuple[str, str], set[str]] = {}
_LOOKUP_TARGETS_BY_SOURCE_CACHE: dict[tuple[Any, ...], dict[str, set[str]]] = {}
_TARGET_WATER_AREA_BY_H3_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_WATER_TERRAIN_DOMAIN_CACHE: dict[str, domains.DomainGeometries] = {}
_WATER_REPRESENTATIVE_POINT_CACHE: dict[tuple[str, str], Any] = {}
_PREPARED_LAND_DOMAIN_CACHE: dict[str, Any] = {}
_WATER_TERRAIN_PREFILTER_CACHE: dict[
    tuple[str, str, str, int], tuple[pd.DataFrame, set[str] | None]
] = {}

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


from .gdal import (
    _area_lookup_path_for_app,
    _partitioned_visibility_dir_for_app,
    _source_type_for_app,
    _terrain_weights_final_path,
    expected_partition_metadata,
    partition_metadata_matches,
)


def combine_partitions(app: AppConfig) -> dict[str, Any]:
    """Materialize dense compact terrain weights from source partitions.

    Terrain partition files are naturally sparse: they only contain source-target
    pairs that were visible through the DEM. The final terrain artifact uses
    SOURCE_TARGET_LOOKUP as the canonical pair universe for the selected source
    domain, so every lookup pair is represented exactly once.

    Working-stage contract under ``paths.output_dir``:

        land/weights_files/TERRAIN_WEIGHTS_H3R{res}.parquet
        ocean/weights_files/TERRAIN_WEIGHTS_H3R{res}.parquet

    with columns:

        source_h3, target_h3, weight_terrain

    where missing sparse visibility rows are filled as weight_terrain = 0.0.
    """

    logger = logging.getLogger(__name__)
    source_type = _source_type_for_app(app)
    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be land or water")

    parts = sorted(_partitioned_visibility_dir_for_app(app).glob("source_h3_cell=*.parquet"))
    expected_metadata = expected_partition_metadata(app)
    mismatched = [path for path in parts if not partition_metadata_matches(path, expected_metadata)]
    if mismatched:
        preview = "\n".join(str(path) for path in mismatched[:5])
        raise ValueError(
            "Cannot combine clear-sky partitions because partition metadata does not "
            "match the active config:\n"
            f"{preview}"
            + ("\n..." if len(mismatched) > 5 else "")
            + "\nDelete stale partitions or rerun with --overwrite."
        )

    lookup_path = _area_lookup_path_for_app(app)
    if not lookup_path.exists():
        raise FileNotFoundError(
            "Cannot materialize dense terrain weights because the source-target "
            f"lookup does not exist: {lookup_path}"
        )

    final_path = _terrain_weights_final_path(app)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_columns = list(FINAL_SCHEMAS["terrain_weights"])

    def _lazy_schema_names(lf: pl.LazyFrame) -> set[str]:
        try:
            return set(lf.collect_schema().names())
        except Exception:
            return set(lf.schema)

    def _normalized_lookup_pairs(path: Path) -> pl.LazyFrame:
        lf = pl.scan_parquet(str(path))
        cols = _lazy_schema_names(lf)
        required = {"source_h3", "target_h3", "distance_km", "source_type"}
        missing = sorted(required - cols)
        if missing:
            raise ValueError(
                f"Source-target lookup must use slim schema {sorted(required)}; "
                f"missing={missing} path={path}"
            )
        return (
            lf.filter(pl.col("source_type") == source_type)
            .select(
                pl.col("source_h3").cast(pl.Utf8),
                pl.col("target_h3").cast(pl.Utf8),
            )
            .unique(subset=["source_h3", "target_h3"])
        )

    def _normalized_visible_terrain(parts_: list[Path]) -> pl.LazyFrame:
        lf = pl.scan_parquet([str(path) for path in parts_])
        cols = _lazy_schema_names(lf)

        if {"source_h3", "target_h3", "weight_terrain"}.issubset(cols):
            visible = lf.select(
                pl.col("source_h3").cast(pl.Utf8),
                pl.col("target_h3").cast(pl.Utf8),
                pl.col("weight_terrain")
                .cast(pl.Float32, strict=False)
                .fill_null(0.0)
                .clip(0.0, 1.0)
                .alias("weight_terrain"),
            )
        else:
            raise ValueError(
                "Terrain partitions have unsupported schema. Expected "
                "source_h3/target_h3/weight_terrain partitions."
            )

        return visible.group_by(["source_h3", "target_h3"]).agg(
            pl.col("weight_terrain").max().alias("weight_terrain")
        )

    def _normalized_clear_sky(parts_: list[Path]) -> pl.LazyFrame:
        lf = pl.scan_parquet([str(path) for path in parts_])
        cols = _lazy_schema_names(lf)
        if not {"source_h3", "target_h3", "weight_terrain"}.issubset(cols):
            raise ValueError(
                "Terrain partitions lack compact terrain keys required for clear-sky output."
            )

        def f32_col(name: str, default: float = 0.0) -> pl.Expr:
            if name in cols:
                return pl.col(name).cast(pl.Float32, strict=False).fill_null(default)
            return pl.lit(default, dtype=pl.Float32)

        def i64_col(name: str, default: int = 0) -> pl.Expr:
            if name in cols:
                expr = pl.col(name).cast(pl.Int64, strict=False).fill_null(default)
            else:
                expr = pl.lit(default, dtype=pl.Int64)
            # Preserve the requested field name for synthesized defaults. Without
            # this alias, multiple absent diagnostics all project as `literal`
            # and Polars rejects the clear-sky select as a duplicate schema.
            return expr.alias(name)

        any_observer_support = (
            f32_col("any_observer_support_fraction")
            if "any_observer_support_fraction" in cols
            else f32_col("observer_sample_fraction")
        )
        union_visible_target = (
            f32_col("union_visible_target_fraction")
            if "union_visible_target_fraction" in cols
            else f32_col("visible_area_fraction")
        )
        joint_los = (
            f32_col("joint_los_fraction")
            if "joint_los_fraction" in cols
            else f32_col("weight_terrain")
        )
        distance_weighted_los = (
            f32_col("distance_weighted_los_fraction")
            if "distance_weighted_los_fraction" in cols
            else f32_col("weight_terrain")
        )

        terrain_binary = (
            pl.col("terrain_binary").cast(pl.Boolean).fill_null(False)
            if "terrain_binary" in cols
            else (pl.col("weight_terrain").cast(pl.Float32, strict=False).fill_null(0.0) > 0.0)
        )
        aggregation_method = (
            pl.col("aggregation_method").cast(pl.Utf8, strict=False).fill_null("dem_raster")
            if "aggregation_method" in cols
            else pl.lit("dem_raster", dtype=pl.Utf8)
        )
        unweighted_los_observed = "joint_los_fraction" in cols
        return (
            lf.select(
                pl.col("source_h3").cast(pl.Utf8),
                pl.col("target_h3").cast(pl.Utf8),
                terrain_binary.alias("terrain_binary"),
                aggregation_method.alias("aggregation_method"),
                pl.lit(unweighted_los_observed, dtype=pl.Boolean).alias("unweighted_los_observed"),
                any_observer_support.clip(0.0, 1.0).alias("any_observer_support_fraction"),
                union_visible_target.clip(0.0, 1.0).alias("union_visible_target_fraction"),
                joint_los.clip(0.0, 1.0).alias("joint_los_fraction"),
                distance_weighted_los.clip(0.0, 1.0).alias("distance_weighted_los_fraction"),
                f32_col("los_distance_weight_sum").clip(0.0, None).alias("los_distance_weight_sum"),
                i64_col("sample_points_requested").clip(0, None),
                i64_col("sample_points_actual").clip(0, None),
                i64_col("visible_sampled_pixel_count").clip(0, None),
                i64_col("visible_observer_pixel_count_sum").clip(0, None),
                i64_col("n_observers").clip(0, None),
                i64_col("target_water_pixel_count").clip(0, None),
                i64_col("target_water_sample_count").clip(0, None),
                i64_col("pixel_stride").clip(0, None),
                f32_col("visible_area_km2").clip(0.0, None).alias("visible_area_km2"),
                f32_col("target_water_area_km2").clip(0.0, None).alias("target_water_area_km2"),
                pl.col("weight_terrain")
                .cast(pl.Float32, strict=False)
                .fill_null(0.0)
                .clip(0.0, 1.0)
                .alias("weight_terrain"),
            )
            .group_by(["source_h3", "target_h3"])
            .agg(
                pl.col("terrain_binary").max().alias("terrain_binary"),
                pl.col("aggregation_method").first().alias("aggregation_method"),
                pl.col("unweighted_los_observed").all().alias("unweighted_los_observed"),
                pl.col("any_observer_support_fraction")
                .max()
                .alias("any_observer_support_fraction"),
                pl.col("union_visible_target_fraction")
                .max()
                .alias("union_visible_target_fraction"),
                pl.col("joint_los_fraction").max().alias("joint_los_fraction"),
                pl.col("distance_weighted_los_fraction")
                .max()
                .alias("distance_weighted_los_fraction"),
                pl.col("los_distance_weight_sum").max().alias("los_distance_weight_sum"),
                pl.col("sample_points_requested").max().alias("sample_points_requested"),
                pl.col("sample_points_actual").max().alias("sample_points_actual"),
                pl.col("visible_sampled_pixel_count").max().alias("visible_sampled_pixel_count"),
                pl.col("visible_observer_pixel_count_sum")
                .max()
                .alias("visible_observer_pixel_count_sum"),
                pl.col("n_observers").max().alias("n_observers"),
                pl.col("target_water_pixel_count").max().alias("target_water_pixel_count"),
                pl.col("target_water_sample_count").max().alias("target_water_sample_count"),
                pl.col("pixel_stride").max().alias("pixel_stride"),
                pl.col("visible_area_km2").max().alias("visible_area_km2"),
                pl.col("target_water_area_km2").max().alias("target_water_area_km2"),
                pl.col("weight_terrain").max().alias("weight_terrain"),
            )
        )

    lookup_pairs = _normalized_lookup_pairs(lookup_path)
    lookup_row_count = int(
        lookup_pairs.select(pl.col("source_h3").count().alias("n")).collect().item()
    )

    if lookup_row_count == 0:
        empty = pl.DataFrame(
            {
                "source_h3": pl.Series([], dtype=pl.Utf8),
                "target_h3": pl.Series([], dtype=pl.Utf8),
                "weight_terrain": pl.Series([], dtype=pl.Float32),
            }
        )
        tmp_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.unlink(missing_ok=True)
        try:
            empty.select(final_columns).write_parquet(tmp_path)
            tmp_path.replace(final_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        row_count = validate_parquet_schema(final_path, FINAL_SCHEMAS["terrain_weights"])
        logger.info(
            "Wrote empty compact terrain weights rows=%d output=%s",
            row_count,
            final_path,
        )
        return {
            "path": final_path,
            "rows": row_count,
            "lookup_rows": lookup_row_count,
            "partition_count": 0,
            "source_type": source_type,
        }

    if not parts:
        raise FileNotFoundError(
            "Cannot materialize dense terrain weights because no terrain partition "
            f"files exist under {_partitioned_visibility_dir_for_app(app)}. Refusing to "
            "write all-zero terrain weights because that would hide a failed terrain run."
        )

    lookup_sources = set(lookup_pairs.select("source_h3").unique().collect()["source_h3"].to_list())
    partition_sources = {
        path.stem.replace("source_h3_cell=", "", 1)
        for path in parts
        if path.stem.startswith("source_h3_cell=")
    }
    missing_sources = sorted(lookup_sources - partition_sources)
    if missing_sources:
        preview = "\n".join(missing_sources[:10])
        raise ValueError(
            "Cannot materialize dense terrain weights because terrain partitions "
            "are missing for source cells that exist in SOURCE_TARGET_LOOKUP. "
            "This would incorrectly convert unprocessed pairs to weight_terrain=0.\n"
            f"Missing source count: {len(missing_sources)}\n"
            f"First missing sources:\n{preview}" + ("\n..." if len(missing_sources) > 10 else "")
        )

    clear_sky_path = (
        final_artifact_paths(app.config_path).ocean_source_target_clear_sky
        if source_type == "water"
        else final_artifact_paths(app.config_path).source_target_clear_sky
    )
    clear_sky_path.parent.mkdir(parents=True, exist_ok=True)
    clear_tmp = clear_sky_path.with_name(f".{clear_sky_path.name}.{uuid.uuid4().hex}.tmp")
    clear_tmp.unlink(missing_ok=True)
    try:
        _normalized_clear_sky(parts).select(FINAL_SCHEMAS["source_target_clear_sky"]).sink_parquet(
            str(clear_tmp)
        )
        clear_tmp.replace(clear_sky_path)
    finally:
        clear_tmp.unlink(missing_ok=True)
    clear_sky_rows = validate_parquet_schema(
        clear_sky_path, FINAL_SCHEMAS["source_target_clear_sky"]
    )
    # Reuse the normalized persisted clear-sky table instead of scanning and
    # grouping every source partition a second time for the compact factor.
    visible_terrain = pl.scan_parquet(str(clear_sky_path)).select(
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
        pl.col("weight_terrain")
        .cast(pl.Float32, strict=False)
        .fill_null(0.0)
        .clip(0.0, 1.0)
        .alias("weight_terrain"),
    )

    dense_terrain = (
        lookup_pairs.join(
            visible_terrain,
            on=["source_h3", "target_h3"],
            how="left",
        )
        .with_columns(
            pl.col("weight_terrain")
            .cast(pl.Float32, strict=False)
            .fill_null(0.0)
            .clip(0.0, 1.0)
            .alias("weight_terrain")
        )
        .select(final_columns)
    )

    tmp_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        dense_terrain.sink_parquet(str(tmp_path))
        tmp_path.replace(final_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    row_count = validate_parquet_schema(final_path, FINAL_SCHEMAS["terrain_weights"])
    logger.info(
        "Wrote dense compact terrain weights rows=%d lookup_rows=%d "
        "visible_partition_count=%d output=%s",
        row_count,
        lookup_row_count,
        len(parts),
        final_path,
    )

    if row_count != lookup_row_count:
        raise ValueError(
            "Dense terrain weights row count does not match SOURCE_TARGET_LOOKUP row count. "
            f"terrain_rows={row_count:,} expected_rows={lookup_row_count:,}"
        )

    return {
        "path": final_path,
        "rows": row_count,
        "lookup_rows": lookup_row_count,
        "partition_count": len(parts),
        "source_type": source_type,
        "source_target_clear_sky_path": clear_sky_path,
        "source_target_clear_sky_rows": clear_sky_rows,
    }


def cleanup_batch_context(app: AppConfig, context: BatchContext) -> None:
    # Keep the most recent memory-mapped pixel/H3 index available process-wide.
    # The cache is bounded to one grid and its files live outside the disposable
    # batch directory.
    if app.run.keep_batch_intermediates:
        return
    batch_dir = context.analysis_dem_path.parent
    try:
        shutil.rmtree(batch_dir)
    except Exception:
        pass


def cleanup_batches_dir(app: AppConfig) -> None:
    if app.run.keep_batch_intermediates:
        return
    batches_dir = app.paths.output_dir / "batches"
    if not batches_dir.exists():
        return
    try:
        shutil.rmtree(batches_dir)
    except Exception:
        pass


def _remove_path_if_exists(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _prune_empty_terrain_dirs(app: AppConfig, removed: list[Path]) -> None:
    """Remove empty partition scaffolding left after source-domain cleanup."""

    for directory in [
        app.paths.partitioned_visibility_dir,
        app.paths.partitioned_visibility_dir.parent,
    ]:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            directory.rmdir()
            removed.append(directory)
        except OSError:
            continue

    for sidecar in metadata_sidecar_candidates(app.paths.partitioned_visibility_dir):
        if _remove_path_if_exists(sidecar):
            removed.append(sidecar)


def cleanup_terrain_intermediates(app: AppConfig) -> list[Path]:
    """Remove terrain scratch artifacts after TERRAIN_WEIGHTS exists."""

    final_path = _terrain_weights_final_path(app)
    if not final_path.exists():
        raise FileNotFoundError(
            f"Refusing to clean terrain intermediates because final output is missing: {final_path}"
        )
    projected_parent = app.paths.projected_dem_path.parent
    projected_cleanup = (
        projected_parent
        if projected_parent.name == "dem_cache" and app.paths.output_dir in projected_parent.parents
        else app.paths.projected_dem_path
    )
    legacy_final = app.paths.final_visibility_path
    candidates = [
        _partitioned_visibility_dir_for_app(app),
        app.paths.manifest_path,
        *metadata_sidecar_candidates(app.paths.manifest_path),
        projected_cleanup,
        app.paths.output_dir / "batches",
        app.paths.output_dir / "cumulative",
        legacy_final,
        legacy_final.with_suffix(".geojson"),
        *metadata_sidecar_candidates(legacy_final),
    ]
    removed: list[Path] = []
    for path in candidates:
        if not path.exists() or path.resolve() == final_path.resolve():
            continue
        if _remove_path_if_exists(path):
            removed.append(path)
    _prune_empty_terrain_dirs(app, removed)
    return removed
