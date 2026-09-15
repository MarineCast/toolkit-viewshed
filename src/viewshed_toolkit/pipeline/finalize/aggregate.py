"""Descriptive source/target summaries of validated physical pair weights."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config import AppConfig
from ..contracts.components import component_root, provenance, record_product
from .composition import validate_composed

FACTORS = ("weight_terrain", "weight_vegetation", "weight_distance", "weight_static_viewability")


def aggregate_frame(frame: pl.DataFrame, key: str) -> pl.DataFrame:
    if key not in {"source_h3", "target_h3"}:
        raise ValueError("Aggregate key must be source_h3 or target_h3")
    other = "target_h3" if key == "source_h3" else "source_h3"
    expressions = [
        pl.len().alias("candidate_pairs"),
        pl.col(other).n_unique().alias("candidate_cells"),
        (pl.col("weight_static_viewability") > 0).sum().alias("positive_static_pairs"),
    ]
    for factor in FACTORS:
        expressions.extend(
            [
                pl.col(factor).mean().alias(f"mean_{factor}"),
                pl.col(factor).max().alias(f"max_{factor}"),
                pl.col(factor).sum().alias(f"sum_{factor}"),
            ]
        )
    expressions.extend(
        [
            pl.col("weight_vegetation")
            .filter(pl.col("weight_terrain") > 0)
            .mean()
            .alias("mean_canopy_given_terrain_support"),
            (pl.col("weight_terrain") > 0).sum().alias("terrain_supported_pairs"),
        ]
    )
    return (
        frame.group_by(key)
        .agg(expressions)
        .with_columns(
            (pl.col("positive_static_pairs") / pl.col("candidate_pairs")).alias(
                "positive_static_fraction"
            )
        )
        .sort(key)
    )


def aggregate_components(app: AppConfig) -> dict[str, Path]:
    root = component_root(app) / "aggregates"
    outputs = {}
    for role in ("land", "water"):
        source = validate_composed(app, role)
        frame = pl.read_parquet(source)
        for key in ("source_h3", "target_h3"):
            result = aggregate_frame(frame, key)
            if role == "water":
                result = result.with_columns(
                    pl.lit(None, dtype=pl.Float64).alias("mean_canopy_given_terrain_support"),
                    *[
                        pl.lit(None, dtype=pl.Float64).alias(f"{stat}_weight_vegetation")
                        for stat in ("mean", "max", "sum")
                    ],
                    pl.lit("not_applicable").alias("canopy_support"),
                )
            output = root / f"{role}_{key.removesuffix('_h3')}_weights.parquet"
            output.parent.mkdir(parents=True, exist_ok=True)
            atomic_sink_parquet(result.lazy(), output)
            record_product(
                output,
                provenance(app, "pair_aggregate_v1", {"static": source}),
                denominator="candidate pairs within configured distance; no effort weighting",
                source_type=role,
                grain=key,
                rows=result.height,
            )
            outputs[f"{role}_{key}"] = output
    return outputs
