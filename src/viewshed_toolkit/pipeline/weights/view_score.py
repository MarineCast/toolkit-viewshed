"""Finalize pair weights into source-density opportunity indices.

Per-pair physical weights are bounded in ``[0, 1]``. Their sums across source
cells are additive exposure/opportunity indices: they are not probabilities and
can exceed one because their magnitude depends on the source-cell universe,
source H3 resolution, and source density.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import polars as pl
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config import DEFAULT_CONFIG
from ..contracts.artifacts import final_artifact_paths
from ..finalize.final_artifacts import (
    build_static_viewability_lazy,
)

AGGREGATE_METRIC_SEMANTICS = {
    "sum": "additive_source_density_opportunity_index_not_probability",
    "mean": "mean_pair_physical_support_over_candidate_source_cells",
    "max": "best_single_pair_physical_support",
    "comparability": (
        "sum metrics require the same source universe, H3 resolution, and " "source sampling policy"
    ),
}


def _write_join_report(output_path: Path, report: dict[str, Any]) -> None:
    report_path = output_path.with_suffix(output_path.suffix + ".join_report.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def finalize_land_view_score(config_path: str | Path, *, overwrite: bool = False) -> Path:
    paths = final_artifact_paths(config_path)
    joined, coverage = build_static_viewability_lazy(config_path, source_type="land")
    joined = joined.with_columns(
        pl.col("weight_static_viewability").alias("land_pair_physical_view_score")
    )
    out = joined.group_by("target_h3").agg(
        [
            pl.col("land_pair_physical_view_score").sum().alias("land_physical_view_score"),
            pl.col("land_pair_physical_view_score").mean().alias("land_mean_physical_view_score"),
            pl.col("land_pair_physical_view_score").max().alias("land_best_physical_view_score"),
            pl.when(pl.col("weight_terrain") > 0)
            .then(pl.col("source_h3"))
            .otherwise(None)
            .n_unique()
            .alias("land_visible_source_count"),
            pl.col("source_h3").n_unique().alias("land_candidate_source_count"),
            (pl.col("weight_terrain").max() > 0).alias("land_binary_visible"),
        ]
    )
    atomic_sink_parquet(out, paths.land_view_score, overwrite=overwrite)
    _write_join_report(
        paths.land_view_score,
        {"pair_universe": coverage, "metric_semantics": AGGREGATE_METRIC_SEMANTICS},
    )
    return paths.land_view_score


def finalize_water_view_score(config_path: str | Path, *, overwrite: bool = False) -> Path:
    paths = final_artifact_paths(config_path)
    physical, coverage = build_static_viewability_lazy(config_path, source_type="water")
    physical = physical.with_columns(
        [
            pl.col("weight_static_viewability").alias("water_physical_weight"),
            pl.col("weight_terrain").alias("water_los_weight"),
        ]
    )
    atomic_sink_parquet(physical, paths.ocean_physical_weights, overwrite=overwrite)

    lf = pl.scan_parquet(str(paths.ocean_physical_weights))
    out = lf.group_by("target_h3").agg(
        [
            pl.col("water_physical_weight").sum().alias("water_all_platforms_physical_view_score"),
            pl.col("water_physical_weight").mean().alias("water_mean_physical_view_score"),
            pl.col("water_physical_weight").max().alias("water_best_physical_view_score"),
            pl.when(pl.col("water_los_weight") > 0)
            .then(pl.col("source_h3"))
            .otherwise(None)
            .n_unique()
            .alias("water_visible_source_count"),
            pl.col("source_h3").n_unique().alias("water_candidate_source_count"),
            (pl.col("water_los_weight").max() > 0).alias("water_binary_visible"),
        ]
    )
    atomic_sink_parquet(out, paths.ocean_view_score, overwrite=overwrite)
    _write_join_report(
        paths.ocean_view_score,
        {"pair_universe": coverage, "metric_semantics": AGGREGATE_METRIC_SEMANTICS},
    )
    return paths.ocean_view_score


def finalize_view_score(
    config_path: str | Path, *, source_type: str, overwrite: bool = False
) -> Path:
    if source_type == "land":
        return finalize_land_view_score(config_path, overwrite=overwrite)
    if source_type == "water":
        return finalize_water_view_score(config_path, overwrite=overwrite)
    raise ValueError("source_type must be land or water")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Finalize physical view scores by source type.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    path = finalize_view_score(args.config, source_type=args.source, overwrite=args.overwrite)
    print(f"physical_view_score: {path}")


if __name__ == "__main__":
    main()
