"""Canonical, predicate-pushed vegetation ray-tracing candidate pairs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from ...config import load_metadata_sidecar, write_metadata_sidecar
from .path_config import VegetationPathConfig


@dataclass(frozen=True)
class CandidatePairArtifact:
    path: Path
    row_count: int
    filter_counts: dict[str, int]
    reused: bool


def _pair_paths(cfg: VegetationPathConfig) -> list[Path]:
    source = cfg.inputs["clear_sky_pairs"]
    paths = sorted(source.glob("source_h3_cell=*.parquet")) if source.is_dir() else [source]
    if not paths or any(not path.exists() for path in paths):
        raise FileNotFoundError(f"No clear-sky pair parquet files found at: {source}")
    return paths


def _pair_input_fingerprint(paths: list[Path]) -> str:
    payload = [
        {
            "path": str(path.resolve()),
            "size": int(path.stat().st_size),
            "mtime_ns": int(path.stat().st_mtime_ns),
        }
        for path in paths
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _candidate_scan_and_counts(
    cfg: VegetationPathConfig,
) -> tuple[pl.LazyFrame, pl.LazyFrame, str]:
    paths = _pair_paths(cfg)
    fingerprint = _pair_input_fingerprint(paths)
    scan = pl.scan_parquet([str(path) for path in paths], hive_partitioning=False)
    schema = set(scan.collect_schema().names())
    filtering = cfg.raw.get("filtering", {})
    required = {cfg.columns["source_id"], cfg.columns["target_id"], "source_type"}
    if bool(filtering.get("only_visible_pairs", True)):
        required.add("terrain_visible_clear_sky")
    distance_column = cfg.columns.get("distance_km", "distance_km")
    if filtering.get("max_distance_km") is not None:
        required.add(distance_column)
    missing_required = sorted(required - schema)
    if missing_required:
        raise ValueError(
            "Clear-sky pair artifact is missing columns required for vegetation "
            f"candidate filtering: {missing_required}"
        )

    land_filter = (
        pl.col("source_type").cast(pl.String, strict=False).eq("land").fill_null(False)
        if "source_type" in schema
        else pl.lit(True)
    )
    visibility_filter = (
        pl.col("terrain_visible_clear_sky").cast(pl.Boolean, strict=False).fill_null(False)
        if bool(filtering.get("only_visible_pairs", True)) and "terrain_visible_clear_sky" in schema
        else pl.lit(True)
    )
    after_visibility = land_filter & visibility_filter

    terrain_support_column = next(
        (
            column
            for column in ("weight_terrain", "terrain_weight", "terrain_visibility_support")
            if column in schema
        ),
        None,
    )
    minimum_terrain_support = filtering.get("min_terrain_visibility_support")
    terrain_filter = (
        pl.col(terrain_support_column)
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .ge(float(minimum_terrain_support))
        if minimum_terrain_support is not None and terrain_support_column is not None
        else pl.lit(True)
    )
    after_terrain = after_visibility & terrain_filter

    maximum_distance = filtering.get("max_distance_km")
    distance_filter = (
        pl.col(distance_column)
        .cast(pl.Float64, strict=False)
        .le(float(maximum_distance))
        .fill_null(False)
        if maximum_distance is not None and distance_column in schema
        else pl.lit(True)
    )
    after_distance = after_terrain & distance_filter

    clear_sky_column = cfg.columns.get("clear_sky_weight", "weight_terrain")
    minimum_clear_sky_weight = filtering.get("min_clear_sky_weight", 0.0)
    clear_sky_filter = (
        pl.col(clear_sky_column)
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .ge(float(minimum_clear_sky_weight))
        if clear_sky_column in schema
        else pl.lit(True)
    )
    final_filter = after_distance & clear_sky_filter

    needed = {
        cfg.columns["source_id"],
        cfg.columns["target_id"],
        distance_column,
        clear_sky_column,
        "source_type",
        "terrain_visible_clear_sky",
        "weight_terrain",
        "terrain_visibility_support",
        "visible_sampled_pixel_count",
        "visible_area_km2_approx_sum",
        "target_water_area_km2",
        "visible_from_n_points",
        "observer_sample_fraction",
        "visible_area_fraction",
        "weight_distance",
        "geometry",
        cfg.columns.get("source_lon", "source_lon"),
        cfg.columns.get("source_lat", "source_lat"),
        cfg.columns.get("target_lon", "target_lon"),
        cfg.columns.get("target_lat", "target_lat"),
    }
    if not bool(cfg.raw.get("geometry", {}).get("use_pair_geometry", False)):
        needed.discard("geometry")
    selected = sorted(column for column in needed if column in schema)
    candidate_scan = scan.filter(final_filter).select(selected)
    source_id = cfg.columns["source_id"]
    if "chunk_parent_h3" not in selected and source_id in selected:
        candidate_scan = candidate_scan.with_columns(
            pl.col(source_id).cast(pl.String).alias("chunk_parent_h3")
        )

    counts_scan = scan.select(
        pl.len().alias("raw_pair_count"),
        after_visibility.cast(pl.UInt64).sum().alias("after_visibility_filter"),
        after_terrain.cast(pl.UInt64).sum().alias("after_terrain_support_filter"),
        after_distance.cast(pl.UInt64).sum().alias("after_distance_filter"),
        final_filter.cast(pl.UInt64).sum().alias("after_min_weight_filter"),
    )
    return candidate_scan, counts_scan, fingerprint


def candidate_pair_counts(cfg: VegetationPathConfig) -> dict[str, int]:
    """Count each cumulative filter stage in one lazy source scan."""

    _candidate_scan, counts_scan, _fingerprint = _candidate_scan_and_counts(cfg)
    row = counts_scan.collect(engine="streaming").row(0, named=True)
    return {name: int(value or 0) for name, value in row.items()}


def materialize_candidate_pairs(
    cfg: VegetationPathConfig,
    *,
    overwrite: bool = False,
) -> CandidatePairArtifact:
    """Build or reuse the canonical filtered pair artifact."""

    path = cfg.outputs.get("candidate_pairs")
    if path is None:
        raise ValueError("vegetation_path_weights.outputs.candidate_pairs is required")
    candidate_scan, counts_scan, fingerprint = _candidate_scan_and_counts(cfg)
    metadata = load_metadata_sidecar(path)
    compatible = bool(
        path.exists()
        and metadata
        and metadata.get("config_hash") == cfg.config_hash
        and metadata.get("run_version") == cfg.run_version
        and metadata.get("pair_input_fingerprint") == fingerprint
    )
    if compatible and not overwrite:
        filter_counts = {
            key: int(metadata.get(key, 0))
            for key in (
                "raw_pair_count",
                "after_visibility_filter",
                "after_terrain_support_filter",
                "after_distance_filter",
                "after_min_weight_filter",
            )
        }
        return CandidatePairArtifact(
            path=path,
            row_count=int(metadata.get("output_row_count", 0)),
            filter_counts=filter_counts,
            reused=True,
        )

    counts_row = counts_scan.collect(engine="streaming").row(0, named=True)
    filter_counts = {name: int(value or 0) for name, value in counts_row.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp{path.suffix}")
    temporary.unlink(missing_ok=True)
    candidate_scan.sink_parquet(temporary, compression="zstd", mkdir=True)
    temporary.replace(path)
    write_metadata_sidecar(
        path,
        cfg.raw_config,
        {
            "step": "vegetation_candidate_pairs",
            "config_hash": cfg.config_hash,
            "run_version": cfg.run_version,
            "pair_input_fingerprint": fingerprint,
            "output_row_count": filter_counts["after_min_weight_filter"],
            **filter_counts,
        },
    )
    return CandidatePairArtifact(
        path=path,
        row_count=filter_counts["after_min_weight_filter"],
        filter_counts=filter_counts,
        reused=False,
    )
