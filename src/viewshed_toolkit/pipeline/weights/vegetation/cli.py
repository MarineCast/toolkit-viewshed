# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

import argparse
import json
import logging
from pathlib import Path
from typing import Sequence

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from ...config import (
    DEFAULT_CONFIG,
    DEFAULT_VEGETATION_PATH_INPUTS,
    DEFAULT_VEGETATION_PATH_OUTPUTS,
    metadata_sidecar_candidates,
    stage_timer,
    write_metadata_sidecar,
)
from ...contracts.cleanup import cleanup_data_contract

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


from .candidate_pairs import candidate_pair_counts, materialize_candidate_pairs
from .pair_chunks import (
    _chunk_metadata_path,
    _chunk_output_row_count,
    _chunk_pair_bounds,
    _load_cached_h3_geometries,
    _load_source_geometries,
    _load_source_weights,
    _load_target_geometries,
    _load_water_union,
    _metadata_compatible,
    _pair_input_signature,
    _vegetation_pair_chunk_for_storage,
    iter_candidate_pair_chunks,
    process_pairs,
)
from .path_config import (
    VegetationPathConfig,
    _require_raster_crs,
    load_vegetation_path_config,
    load_vegetation_rasters_full,
    load_vegetation_rasters_window,
)
from .ray_sampling import _project_geometry_lookup
from .source_h3 import run_vegetation_weights
from .summarize import (
    _materialize_final_vegetation_from_chunks,
    aggregate_chunk_outputs,
    build_water_neutral_vegetation_weights,
    cleanup_vegetation_intermediates,
)


def run_vegetation_path_weights(
    config_path: str | Path | VegetationPathConfig,
    *,
    overwrite: bool | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    clean_intermediates: bool = True,
    source_type: str = "land",
) -> dict[str, Path | None]:
    if source_type == "water":
        if isinstance(config_path, VegetationPathConfig):
            raise TypeError(
                "In-memory VegetationPathConfig is currently supported for land sources only."
            )
        return build_water_neutral_vegetation_weights(
            config_path,
            overwrite=bool(overwrite),
            dry_run=dry_run,
        )
    if source_type != "land":
        raise ValueError("source_type must be land or water.")
    cfg = (
        config_path
        if isinstance(config_path, VegetationPathConfig)
        else load_vegetation_path_config(config_path)
    )
    perf = cfg.raw.get("performance", {})
    overwrite = bool(perf.get("overwrite", False) if overwrite is None else overwrite)
    pair_out = cfg.outputs.get("pair_chunks") or cfg.outputs.get("pair_weights")
    if pair_out is None:
        raise ValueError("vegetation_path_weights.outputs.pair_chunks is required.")
    LOGGER.info("Vegetation pair chunks directory: %s", pair_out)
    LOGGER.info("Vegetation target summary path: %s", cfg.outputs.get("target_summary"))
    LOGGER.info("Vegetation source summary path: %s", cfg.outputs.get("source_summary"))
    count_before = bool(perf.get("count_candidates_before_run", False))
    if dry_run:
        filter_counts = candidate_pair_counts(cfg)
        candidate_count = filter_counts["after_min_weight_filter"]
        if limit is not None:
            candidate_count = min(candidate_count, int(limit))
        print(f"Dry run only; candidate pairs: {candidate_count:,}")
        print(f"Filter counts: {filter_counts}")
        print(f"Inputs: {cfg.inputs}")
        print(f"Outputs: {cfg.outputs}")
        return {}
    candidate_artifact = materialize_candidate_pairs(cfg, overwrite=overwrite)
    LOGGER.info(
        "%s vegetation candidate pair artifact: %s (%d rows)",
        "Reused" if candidate_artifact.reused else "Wrote",
        candidate_artifact.path,
        candidate_artifact.row_count,
    )
    candidate_count = (
        min(candidate_artifact.row_count, int(limit))
        if count_before and limit is not None
        else (candidate_artifact.row_count if count_before else None)
    )
    if candidate_count is not None:
        LOGGER.info("Loaded %d vegetation path candidate pairs.", candidate_count)
    else:
        LOGGER.info("Skipping pre-count of vegetation path candidate pairs.")
    existing_chunks = sorted(pair_out.glob("chunk_*.parquet")) if pair_out.exists() else []
    stale_chunks_rebuilt = False
    existing_chunks_compatible = bool(existing_chunks) and all(
        _metadata_compatible(_chunk_metadata_path(path), cfg) for path in existing_chunks
    )
    target_out = cfg.outputs.get("target_summary")
    source_out = cfg.outputs.get("source_summary")
    summaries_exist = (target_out is None or target_out.exists()) and (
        source_out is None or source_out.exists()
    )
    if existing_chunks and not overwrite and existing_chunks_compatible:
        if not summaries_exist:
            LOGGER.info(
                "Reusing %d vegetation path chunk(s) and rebuilding missing summaries.",
                len(existing_chunks),
            )
            target_df, source_df = aggregate_chunk_outputs(cfg)
            if target_out is not None and not target_out.exists():
                target_df.to_parquet(target_out, index=False)
                write_metadata_sidecar(
                    target_out,
                    cfg.raw_config,
                    {"step": "vegetation_path_target_summary"},
                )
            if source_out is not None and not source_out.exists():
                source_df.to_parquet(source_out, index=False)
                write_metadata_sidecar(
                    source_out,
                    cfg.raw_config,
                    {"step": "vegetation_path_source_summary"},
                )
        else:
            LOGGER.info(
                "Reusing existing vegetation path weights: %s (%d chunk files)",
                pair_out,
                len(existing_chunks),
            )
        final_path, _rows = _materialize_final_vegetation_from_chunks(cfg, overwrite=overwrite)
        outputs = {
            "vegetation_weights": final_path,
            "candidate_pairs": candidate_artifact.path,
            "pair_chunks": pair_out,
            "pair_weights": pair_out,
            "target_summary": target_out,
            "source_summary": source_out,
            "target_aggregation": target_out,
            "source_aggregation": source_out,
            "manifest": cfg.outputs.get("manifest"),
        }
        if clean_intermediates:
            cleanup_vegetation_intermediates(cfg)
            if not isinstance(config_path, VegetationPathConfig):
                cleanup_data_contract(
                    config_path,
                    require=("vegetation_weights",),
                    remove_stage_scratch=False,
                )
        return outputs
    if existing_chunks and not overwrite and not existing_chunks_compatible:
        LOGGER.warning(
            "Existing vegetation path chunks are stale and will be rebuilt: %s",
            pair_out,
        )
        for chunk_path in existing_chunks:
            chunk_path.unlink(missing_ok=True)
            for sidecar in metadata_sidecar_candidates(chunk_path):
                sidecar.unlink(missing_ok=True)
        existing_chunks = []
        stale_chunks_rebuilt = True
    if pair_out.exists() and not existing_chunks and not overwrite:
        LOGGER.warning(
            "Vegetation pair chunks directory exists but contains no chunk_*.parquet files; processing will run: %s",
            pair_out,
        )
        if not all(
            name in cfg.inputs
            for name in (
                "surface_transmission",
                "surface_obstruction",
                "chm_obstruction",
                "landcover",
                "clear_sky_pairs",
                "source_cells",
            )
        ):
            return {
                "pair_chunks": pair_out,
                "pair_weights": pair_out,
                "target_summary": target_out,
                "source_summary": source_out,
                "target_aggregation": target_out,
                "source_aggregation": source_out,
                "manifest": cfg.outputs.get("manifest"),
            }
    chunk_size = max(1, int(perf.get("chunk_size_pairs", 5000)))
    chunk_dir = pair_out
    chunk_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for old in chunk_dir.glob("chunk_*.parquet"):
            old.unlink()
    debug_chunks = []
    processed = 0
    derived_source_geoms: dict[str, BaseGeometry] = {}
    derived_target_geoms: dict[str, tuple[BaseGeometry, str]] = {}
    missing_source_ids: set[str] = set()
    raster_access_mode = str(cfg.raw.get("raster_access_mode", "windowed"))
    raster_data = load_vegetation_rasters_full(cfg) if raster_access_mode == "full_read" else None
    raster_crs = (
        raster_data.raster_crs
        if raster_data is not None
        else _require_raster_crs(cfg.inputs["surface_transmission"])
    )
    source_geoms = _project_geometry_lookup(_load_source_geometries(cfg), raster_crs)
    cached_full_geoms = _load_cached_h3_geometries(cfg, "geometry_projected", raster_crs)
    if cached_full_geoms is not None:
        derived_source_geoms.update(
            {
                cell: geometry
                for cell, geometry in cached_full_geoms.items()
                if cell not in source_geoms
            }
        )
    target_loaded = _load_target_geometries(cfg)
    target_geoms = _load_cached_h3_geometries(cfg, "water_geometry_projected", raster_crs)
    if target_geoms is None and target_loaded is not None:
        target_geoms = _project_geometry_lookup(target_loaded, raster_crs)
    water_union = None if target_geoms is not None else _load_water_union(cfg)
    source_weights = _load_source_weights(cfg)
    point_cache: dict[tuple[str, str], list[Point]] = {}
    progress = None
    try:
        from tqdm.auto import tqdm

        progress = (
            tqdm(total=candidate_count, desc="Vegetation path pairs")
            if candidate_count is not None
            else None
        )
    except Exception:
        progress = None
    try:
        manifest_rows = []
        filter_totals: dict[str, int] = {}
        debug_rows_remaining = int(cfg.raw.get("debug", {}).get("max_pairs", 100))
        pair_input_path, pair_input_mtime = _pair_input_signature(cfg)
        for chunk_id, pairs_chunk in enumerate(
            iter_candidate_pair_chunks(
                cfg,
                chunk_size=chunk_size,
                limit=limit,
                count_totals=filter_totals,
            )
        ):
            start = processed
            stop = processed + len(pairs_chunk)
            LOGGER.debug("Processing vegetation path pair chunk %d:%d", start, stop)
            chunk_path = chunk_dir / f"chunk_{chunk_id:06d}.parquet"
            parent_h3 = (
                str(pairs_chunk["chunk_parent_h3"].iloc[0])
                if "chunk_parent_h3" in pairs_chunk.columns and not pairs_chunk.empty
                else None
            )
            if (
                chunk_path.exists()
                and not overwrite
                and _metadata_compatible(_chunk_metadata_path(chunk_path), cfg)
            ):
                existing_rows = _chunk_output_row_count(chunk_path)
                manifest_rows.append(
                    {
                        "chunk_id": chunk_id,
                        "parent_h3": parent_h3,
                        "vegetation_path_chunk": str(chunk_path),
                        "status": "skipped_existing",
                        "input_row_count": int(len(pairs_chunk)),
                        "output_row_count": existing_rows,
                        "runtime_seconds": 0.0,
                        "run_version": cfg.run_version,
                        "config_hash": cfg.config_hash,
                    }
                )
                processed = stop
                continue
            chunk_raster_data = raster_data
            if chunk_raster_data is None:
                bounds = _chunk_pair_bounds(
                    pairs_chunk,
                    cfg,
                    source_geoms=source_geoms,
                    target_geoms=target_geoms,
                    raster_crs=raster_crs,
                    pad_pixels=int(cfg.raw.get("performance", {}).get("window_pad_pixels", 2)),
                )
                chunk_raster_data = load_vegetation_rasters_window(cfg, bounds)
            chunk_df, debug_df = process_pairs(
                cfg,
                pairs_chunk,
                progress=progress,
                derived_source_geoms=derived_source_geoms,
                missing_source_ids=missing_source_ids,
                source_geoms=source_geoms,
                target_geoms=target_geoms,
                source_weights=source_weights,
                raster_crs=raster_crs,
                raster_data=chunk_raster_data,
                water_union=water_union,
                point_cache=point_cache,
                derived_target_geoms=derived_target_geoms,
                chunk_id=chunk_id,
                max_debug_rows=max(0, debug_rows_remaining),
            )
            chunk_df = _vegetation_pair_chunk_for_storage(chunk_df)
            with stage_timer("vegetation_path_chunk", output_path=chunk_path) as t:
                chunk_df.to_parquet(chunk_path, index=False)
                t["input_rows"] = len(pairs_chunk)
                t["output_rows"] = len(chunk_df)
                t["unique_source_h3_cell"] = (
                    int(chunk_df["source_h3"].nunique())
                    if "source_h3" in chunk_df.columns
                    else None
                )
                t["unique_target_h3_cell"] = (
                    int(chunk_df["target_h3"].nunique())
                    if "target_h3" in chunk_df.columns
                    else None
                )
            manifest_rows.append(
                {
                    "chunk_id": chunk_id,
                    "parent_h3": parent_h3,
                    "vegetation_path_chunk": str(chunk_path),
                    "status": "ok",
                    "input_row_count": int(len(pairs_chunk)),
                    "output_row_count": int(len(chunk_df)),
                    "runtime_seconds": float(t.get("elapsed_seconds", 0.0)),
                    "run_version": cfg.run_version,
                    "config_hash": cfg.config_hash,
                }
            )
            _chunk_metadata_path(chunk_path).write_text(
                json.dumps(
                    {
                        "chunk_id": int(chunk_id),
                        "parent_h3": parent_h3,
                        "input_row_count": int(len(pairs_chunk)),
                        "output_row_count": int(len(chunk_df)),
                        "config_hash": cfg.config_hash,
                        "run_version": cfg.run_version,
                        "pair_input_path": pair_input_path,
                        "pair_input_mtime_ns": pair_input_mtime,
                        "runtime_seconds": float(t.get("elapsed_seconds", 0.0)),
                        "output_path": str(chunk_path),
                    },
                    indent=2,
                )
            )
            LOGGER.info("Wrote vegetation path chunk: %s", chunk_path)
            if debug_df is not None:
                debug_chunks.append(debug_df)
                debug_rows_remaining = max(0, debug_rows_remaining - len(debug_df))
            processed = stop
        LOGGER.info(
            "vegetation_pair_filter_counts raw_pair_count=%d after_visibility_filter=%d "
            "after_terrain_support_filter=%d after_distance_filter=%d after_min_weight_filter=%d",
            filter_totals.get("raw_pair_count", 0),
            filter_totals.get("after_visibility_filter", 0),
            filter_totals.get("after_terrain_support_filter", 0),
            filter_totals.get("after_distance_filter", 0),
            filter_totals.get("after_min_weight_filter", 0),
        )
    finally:
        if progress is not None:
            progress.close()
    if missing_source_ids:
        LOGGER.warning(
            "Derived full H3 source polygons for %d source cell(s) missing from source_cells.",
            len(missing_source_ids),
        )
    manifest = pd.DataFrame(manifest_rows)
    manifest_path = cfg.outputs.get("manifest")
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest.to_csv(manifest_path, index=False)
    write_metadata_sidecar(
        chunk_dir,
        cfg.raw_config,
        {"step": "vegetation_path_weights_chunks"},
    )
    debug_df = pd.concat(debug_chunks, ignore_index=True) if debug_chunks else None
    LOGGER.info("Wrote vegetation path pair chunks under: %s", chunk_dir)
    target_df, source_df = aggregate_chunk_outputs(cfg)
    target_out = cfg.outputs.get("target_summary")
    if target_out is not None:
        target_df.to_parquet(target_out, index=False)
        write_metadata_sidecar(
            target_out,
            cfg.raw_config,
            {"step": "vegetation_path_target_summary"},
        )
        LOGGER.info("Wrote vegetation-adjusted target aggregates: %s", target_out)
    source_out = cfg.outputs.get("source_summary")
    if source_out is not None:
        source_df.to_parquet(source_out, index=False)
        write_metadata_sidecar(
            source_out,
            cfg.raw_config,
            {"step": "vegetation_path_source_summary"},
        )
        LOGGER.info("Wrote vegetation-adjusted source aggregates: %s", source_out)
    debug_out = cfg.outputs.get("debug_rays")
    if debug_out is not None and debug_df is not None:
        if isinstance(debug_df, gpd.GeoDataFrame):
            debug_df.to_parquet(debug_out, index=False)
        else:
            debug_df.to_parquet(debug_out, index=False)
        LOGGER.info("Wrote debug vegetation rays: %s", debug_out)
    final_path, _rows = _materialize_final_vegetation_from_chunks(
        cfg, overwrite=overwrite or stale_chunks_rebuilt
    )
    outputs = {
        "vegetation_weights": final_path,
        "candidate_pairs": candidate_artifact.path,
        "pair_chunks": chunk_dir,
        "pair_weights": chunk_dir,
        "target_summary": target_out,
        "source_summary": source_out,
        "target_aggregation": target_out,
        "source_aggregation": source_out,
        "manifest": cfg.outputs.get("manifest"),
        "debug_rays": debug_out,
    }
    if clean_intermediates:
        cleanup_vegetation_intermediates(cfg)
        if not isinstance(config_path, VegetationPathConfig):
            cleanup_data_contract(
                config_path,
                require=("vegetation_weights",),
                remove_stage_scratch=False,
            )
    return outputs


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build vegetation source weights or path attenuation weights."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    source = sub.add_parser(
        "source", help="Build vegetation rasters and source-cell vegetation weights."
    )
    source.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    source.add_argument("--overwrite", action="store_true")
    source.add_argument("--dry-run", action="store_true")
    source.add_argument("--resolution-m", type=int, default=None)
    source.add_argument("--h3-resolution", type=int, default=None)
    source.add_argument("--skip-rasters", action="store_true")
    source.add_argument("--skip-h3", action="store_true")

    paths = sub.add_parser(
        "paths",
        help="Build vegetation path attenuation weights for clear-sky source-target pairs.",
    )
    paths.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    paths.add_argument("--overwrite", action="store_true")
    paths.add_argument("--dry-run", action="store_true")
    paths.add_argument("--limit", type=int, default=None)
    paths.add_argument("--keep-intermediates", action="store_true")
    paths.add_argument("--source", choices=["land", "water"], default="land")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    if args.command == "source":
        outputs = run_vegetation_weights(
            args.config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            resolution_m=args.resolution_m,
            h3_resolution=args.h3_resolution,
            skip_rasters=args.skip_rasters,
            skip_h3=args.skip_h3,
        )
    elif args.command == "paths":
        outputs = run_vegetation_path_weights(
            args.config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            limit=args.limit,
            clean_intermediates=not args.keep_intermediates,
            source_type=args.source,
        )
    else:  # pragma: no cover
        parser.error(f"Unknown command: {args.command}")
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
