# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

import logging
from pathlib import Path

import pandas as pd
import polars as pl
from viewshed_toolkit._internal.data.parquet import (
    atomic_sink_parquet,
    validate_parquet_schema,
)

from ...config import (
    DEFAULT_VEGETATION_PATH_INPUTS,
    DEFAULT_VEGETATION_PATH_OUTPUTS,
    metadata_sidecar_candidates,
    resolve_path,
    scan_parquet_lazy,
    viewshed_domain_relative,
)
from ...contracts.artifacts import (
    FINAL_SCHEMAS,
    VEGETATION_STATUS_NOT_APPLICABLE,
    final_artifact_paths,
)
from ...contracts.cleanup import remove_paths
from ...finalize.final_artifacts import (
    materialize_vegetation_weights_from_mapping,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_INPUTS = dict(DEFAULT_VEGETATION_PATH_INPUTS)
DEFAULT_COLUMNS = {
    "source_id": "source_h3",
    "target_id": "target_h3",
    "target_id_output": "target_h3",
    "clear_sky_weight": "weight_terrain",
    "distance_km": "distance_km",
    "source_weight_column": "land_source_vegetation_weight",
    "source_lon": "source_lon",
    "source_lat": "source_lat",
    "target_lon": "target_lon",
    "target_lat": "target_lat",
}
DEFAULT_OUTPUTS = dict(DEFAULT_VEGETATION_PATH_OUTPUTS)


from .path_config import VegetationPathConfig


def _iter_chunk_paths(cfg: VegetationPathConfig) -> list[Path]:
    chunk_root = cfg.outputs.get("pair_chunks") or cfg.outputs["pair_weights"]
    if chunk_root is None:
        return []
    if chunk_root.suffix:
        chunk_root = chunk_root.parent / "chunks"
    if chunk_root.is_dir():
        return sorted(chunk_root.glob("chunk_*.parquet"))
    return [chunk_root] if chunk_root.exists() else []


def aggregate_chunk_outputs(
    cfg: VegetationPathConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    target_col = "target_h3"
    source_col = "source_h3"
    chunk_paths = _iter_chunk_paths(cfg)
    if not chunk_paths:
        return pd.DataFrame(), pd.DataFrame()
    lf_all = scan_parquet_lazy(chunk_paths)
    schema_names = set(lf_all.collect_schema().names())
    select_cols = [source_col, target_col]
    optional_cols = [
        "weight_landcover",
        "weight_chm",
    ]
    select_cols.extend(
        col for col in optional_cols if col in schema_names and col not in select_cols
    )
    lf = lf_all.select(select_cols)
    lf = lf.with_columns(
        [
            pl.col("weight_landcover").cast(pl.Float64, strict=False).fill_null(1.0),
            pl.col("weight_chm").cast(pl.Float64, strict=False).fill_null(1.0),
        ]
    ).with_columns(
        [
            (pl.col("weight_landcover") * pl.col("weight_chm")).alias("vegetation_weight"),
        ]
    )
    target_df = (
        lf.group_by(target_col)
        .agg(
            [
                pl.col(source_col).n_unique().alias("n_visible_source_cells"),
                pl.col("vegetation_weight").sum().alias("sum_vegetation_weight"),
                pl.col("vegetation_weight").max().alias("max_vegetation_weight"),
                pl.col("vegetation_weight").mean().alias("mean_vegetation_weight"),
                pl.col("weight_landcover").mean().alias("mean_weight_landcover"),
                pl.col("weight_chm").mean().alias("mean_weight_chm"),
                pl.lit(cfg.run_version).alias("run_version"),
                pl.lit(cfg.config_hash).alias("config_hash"),
            ]
        )
        .sort(target_col)
        .collect()
        .to_pandas()
    )
    source_df = (
        lf.group_by(source_col)
        .agg(
            [
                pl.col(target_col).n_unique().alias("n_visible_target_cells"),
                pl.col("vegetation_weight").sum().alias("sum_vegetation_weight"),
                pl.col("vegetation_weight").max().alias("max_vegetation_weight"),
                pl.col("vegetation_weight").mean().alias("mean_vegetation_weight"),
                pl.col("weight_landcover").mean().alias("mean_weight_landcover"),
                pl.col("weight_chm").mean().alias("mean_weight_chm"),
                pl.lit(cfg.run_version).alias("run_version"),
                pl.lit(cfg.config_hash).alias("config_hash"),
            ]
        )
        .sort(source_col)
        .collect()
        .to_pandas()
    )
    LOGGER.info("aggregate_chunk_outputs chunks=%d", len(chunk_paths))
    return target_df, source_df


def cleanup_vegetation_intermediates(cfg: VegetationPathConfig) -> list[Path]:
    """Remove vegetation scratch artifacts after VEGETATION_WEIGHTS exists."""

    pair_dir = cfg.outputs.get("pair_chunks") or cfg.outputs.get("pair_weights")
    resolution_m = int(
        cfg.raw.get(
            "resolution_m",
            cfg.raw_config.get("vegetation_weights", {}).get(
                "resolution_m",
                cfg.raw_config.get("viewshed", {}).get("dem_resolution_m", 30),
            ),
        )
    )
    source_res = int(
        (cfg.raw_config.get("h3", {}) or {}).get(
            "source_resolution", cfg.raw_config.get("h3_resolution", 6)
        )
    )
    data_dir = resolve_path("data", cfg.config_dir)
    weights_cfg = cfg.raw_config.get("vegetation_weights", {}) or {}
    land_vegetation_dir = resolve_path(
        weights_cfg.get("output_dir", viewshed_domain_relative("land", "vegetation")),
        cfg.config_dir,
    )
    land_support_dir = resolve_path(
        weights_cfg.get("support_dir", land_vegetation_dir / "support"),
        cfg.config_dir,
    )
    candidates: list[Path] = []
    if pair_dir is not None:
        # Pair chunks are scratch after final composition. The sibling
        # VEGETATION_CANDIDATE_PAIRS parquet is a canonical reusable artifact
        # and must survive cleanup.
        candidates.append(pair_dir)
    for key in ("manifest", "target_summary", "source_summary", "debug_rays"):
        value = cfg.outputs.get(key)
        if value is not None:
            candidates.append(value)
    for key in ("source_cell_weights", "surface_transmission", "surface_obstruction"):
        value = cfg.inputs.get(key)
        if value is not None:
            candidates.append(value)
    candidates.extend(
        [
            land_support_dir / "LAND_COVER_CLASS_WEIGHTS.csv",
            land_support_dir / f"CHM_OBSTRUCTION_{resolution_m}M.tif",
            land_support_dir / f"LAND_COVER_OBSTRUCTION_MULTIPLIER_{resolution_m}M.tif",
            land_support_dir / f"LAND_COVER_OBSTRUCTION_FLOOR_{resolution_m}M.tif",
            land_support_dir / f"LAND_COVER_SOURCE_ACCESS_WEIGHT_{resolution_m}M.tif",
            land_support_dir / f"SURFACE_OBSTRUCTION_{resolution_m}M.tif",
            land_support_dir / f"SURFACE_VISIBILITY_TRANSMISSION_{resolution_m}M.tif",
            land_vegetation_dir
            / f"LAND_SOURCE_CELL_VEGETATION_WEIGHTS_{resolution_m}M_R{source_res}.parquet",
            # Legacy locations kept here so cleanup removes artifacts from older runs.
            data_dir / "LAND_COVER_CLASS_LOOKUP.csv",
            data_dir / "LAND_COVER_CLASS_WEIGHTS.csv",
            data_dir / f"CHM_OBSTRUCTION_{resolution_m}M.tif",
            data_dir / f"LAND_COVER_OBSTRUCTION_MULTIPLIER_{resolution_m}M.tif",
            data_dir / f"LAND_COVER_OBSTRUCTION_FLOOR_{resolution_m}M.tif",
            data_dir / f"LAND_COVER_SOURCE_ACCESS_WEIGHT_{resolution_m}M.tif",
            data_dir / f"SURFACE_OBSTRUCTION_{resolution_m}M.tif",
            data_dir / f"SURFACE_VISIBILITY_TRANSMISSION_{resolution_m}M.tif",
            data_dir / f"LAND_SOURCE_CELL_VEGETATION_WEIGHTS_{resolution_m}M_R{source_res}.parquet",
        ]
    )
    # De-duplicate while preserving order and avoid deleting raw CHM/LAND_COVER inputs.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in candidates:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
        for sidecar in metadata_sidecar_candidates(path):
            if sidecar not in seen:
                seen.add(sidecar)
                deduped.append(sidecar)

    removed = remove_paths(deduped)
    stop_dirs = {data_dir.resolve(), cfg.config_dir.resolve()}
    for path in list(removed):
        parent = Path(path).parent
        while parent.exists():
            try:
                resolved = parent.resolve()
            except OSError:
                break
            if resolved in stop_dirs:
                break
            try:
                parent.rmdir()
            except OSError:
                break
            removed.append(parent)
            parent = parent.parent
    return removed


def _materialize_final_vegetation_from_chunks(
    cfg: VegetationPathConfig,
    *,
    overwrite: bool,
) -> tuple[Path, int]:
    chunk_paths = _iter_chunk_paths(cfg)
    if not chunk_paths:
        raise FileNotFoundError(
            "No vegetation chunk files found to materialize final vegetation weights."
        )
    return materialize_vegetation_weights_from_mapping(
        chunk_paths,
        cfg.raw_config,
        config_dir=cfg.config_dir,
        overwrite=overwrite,
    )


def build_water_neutral_vegetation_weights(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Path | None]:
    """Persist neutral water-source vegetation weights aligned to water terrain rows."""

    paths = final_artifact_paths(config_path)
    if not paths.ocean_terrain_weights.exists():
        raise FileNotFoundError(
            "Missing water-source terrain weights: "
            f"{paths.ocean_terrain_weights}. Run terrain-weight --source water first."
        )
    if dry_run:
        print(f"water_vegetation_weights: {paths.ocean_vegetation_weights}")
        return {"water_vegetation_weights": paths.ocean_vegetation_weights}

    lf = pl.scan_parquet(str(paths.ocean_terrain_weights)).select(
        [
            "source_h3",
            "target_h3",
            pl.lit("water").alias("source_type"),
            pl.lit(1.0).cast(pl.Float32).alias("weight_vegetation"),
            pl.lit(VEGETATION_STATUS_NOT_APPLICABLE).alias("vegetation_status"),
        ]
    )
    atomic_sink_parquet(lf, paths.ocean_vegetation_weights, overwrite=overwrite)
    validate_parquet_schema(paths.ocean_vegetation_weights, FINAL_SCHEMAS["vegetation_weights"])
    return {"water_vegetation_weights": paths.ocean_vegetation_weights}
