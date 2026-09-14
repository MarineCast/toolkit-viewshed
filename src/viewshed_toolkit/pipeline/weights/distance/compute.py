"""Distance-decay evaluators and centroid diagnostics for viewshed pairs.

The raster viewshed uses :func:`distance_weight_values` inside observer/pixel
LOS aggregation. The lookup-driven path below separately materializes H3
centroid weights for diagnostics and backwards-compatible artifacts.

Canonical input
---------------
The source-target pair universe is created upstream by ``prepare_area.py`` and
must be stored as the new-standard lookup schema::

    source_h3: str
    target_h3: str
    distance_km: float
    source_type: str  # one of {"land", "water"}

This module does not classify land/water cells, generate H3 neighborhoods, or
recompute centroid distances. If the lookup does not contain ``distance_km`` or
``source_type``, the lookup is invalid and must be rebuilt.

Production output
-----------------
The compact distance-weight artifact contains exactly::

    source_h3: str
    target_h3: str
    distance_km: float32
    weight_distance: float32

The selected source type is encoded by the output path, not repeated in every
row of the final distance table.
"""

from __future__ import annotations

import logging
import math
import shutil
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import polars as pl

try:
    from ...config import (
        metadata_sidecar_candidates,
        write_metadata_sidecar,
    )
    from ...contracts.artifacts import final_artifact_paths
    from ...contracts.pairs import (
        DISTANCE_OUTPUT_SCHEMA,
        SOURCE_TARGET_LOOKUP_SCHEMA,
        SOURCE_TYPES,
    )
    from ...finalize.final_artifacts import (
        materialize_distance_weights,
    )
except ImportError:  # pragma: no cover - loose script execution
    from viewshed_toolkit.pipeline.config import (
        metadata_sidecar_candidates,
        write_metadata_sidecar,
    )
    from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
    from viewshed_toolkit.pipeline.contracts.pairs import (
        DISTANCE_OUTPUT_SCHEMA,
        SOURCE_TARGET_LOOKUP_SCHEMA,
        SOURCE_TYPES,
    )
    from viewshed_toolkit.pipeline.finalize.final_artifacts import (
        materialize_distance_weights,
    )


LOGGER = logging.getLogger(__name__)
VALID_SOURCE_TYPES = SOURCE_TYPES
LOOKUP_SCHEMA = SOURCE_TARGET_LOOKUP_SCHEMA


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


from ...config.distance import (
    DistanceRuntime,
    DistanceWeightConfig,
    DistanceWeightPaths,
    _validate_source_type,
    distance_weight_paths,
    resolved_max_distance_km,
)

# -----------------------------------------------------------------------------
# Weight expressions
# -----------------------------------------------------------------------------


def distance_weight_expr(
    cfg: DistanceWeightConfig,
    max_distance_km: float | None = None,
) -> pl.Expr:
    """Return a Polars expression that computes ``weight_distance``.

    When ``normalize_at_zero`` is enabled, the selected curve is divided by
    its value at zero distance. This makes ``weight_distance(0) == 1`` while
    preserving the curve shape for non-negative distances. The result is still
    clipped to the physical support interval [0, 1].
    """

    d = pl.col("distance_km").cast(pl.Float64)
    selected = cfg.selected_model.lower().strip()

    if selected == "logistic":
        raw = 1.0 / (1.0 + ((d - float(cfg.logistic_d50_km)) / float(cfg.logistic_slope_km)).exp())
        if cfg.normalize_at_zero:
            zero_weight = 1.0 / (
                1.0 + math.exp(-float(cfg.logistic_d50_km) / float(cfg.logistic_slope_km))
            )
            raw = raw / zero_weight
    elif selected == "exponential":
        raw = (-d / float(cfg.exponential_lambda_km)).exp()
    elif selected == "piecewise":
        near = float(
            cfg.piecewise_near_km
            if cfg.piecewise_near_km is not None
            else cfg.piecewise_full_weight_km
        )
        far = float(
            cfg.piecewise_far_km
            if cfg.piecewise_far_km is not None
            else cfg.piecewise_zero_weight_km
        )
        raw = (
            pl.when(d <= near)
            .then(1.0)
            .when(d >= far)
            .then(0.0)
            .otherwise(1.0 - ((d - near) / (far - near)))
        )
    else:  # guarded by config validation
        raise ValueError(f"Unsupported distance model: {cfg.selected_model}")

    clipped = raw.clip(0.0, 1.0)
    if max_distance_km is not None:
        clipped = pl.when(d <= float(max_distance_km)).then(clipped).otherwise(0.0)

    return clipped.cast(pl.Float32).alias("weight_distance")


def distance_weight_values(
    distances_km: np.ndarray,
    cfg: DistanceWeightConfig,
    max_distance_km: float | None = None,
) -> np.ndarray:
    """Evaluate the configured distance curve for NumPy distance arrays.

    This is the raster-aggregation counterpart to :func:`distance_weight_expr`.
    Keeping both evaluators equivalent lets terrain aggregation apply distance
    at each visible observer/target-water-pixel pair rather than at H3
    centroids.
    """

    d = np.asarray(distances_km, dtype="float64")
    selected = cfg.selected_model.lower().strip()
    if selected == "logistic":
        exponent = (d - float(cfg.logistic_d50_km)) / float(cfg.logistic_slope_km)
        # ``exp(-logaddexp(0, x))`` is algebraically equivalent to
        # ``1 / (1 + exp(x))`` but remains stable for arbitrarily distant
        # pairs instead of emitting overflow warnings before the hard cutoff.
        raw = np.exp(-np.logaddexp(0.0, exponent))
        if cfg.normalize_at_zero:
            zero_weight = 1.0 / (
                1.0 + math.exp(-float(cfg.logistic_d50_km) / float(cfg.logistic_slope_km))
            )
            raw = raw / zero_weight
    elif selected == "exponential":
        raw = np.exp(-d / float(cfg.exponential_lambda_km))
    elif selected == "piecewise":
        near = float(
            cfg.piecewise_near_km
            if cfg.piecewise_near_km is not None
            else cfg.piecewise_full_weight_km
        )
        far = float(
            cfg.piecewise_far_km
            if cfg.piecewise_far_km is not None
            else cfg.piecewise_zero_weight_km
        )
        raw = np.where(
            d <= near,
            1.0,
            np.where(d >= far, 0.0, 1.0 - ((d - near) / (far - near))),
        )
    else:  # guarded by config validation
        raise ValueError(f"Unsupported distance model: {cfg.selected_model}")

    clipped = np.clip(raw, 0.0, 1.0)
    if max_distance_km is not None:
        clipped = np.where(d <= float(max_distance_km), clipped, 0.0)
    return clipped.astype("float32", copy=False)


# -----------------------------------------------------------------------------
# Lookup validation / partition helpers
# -----------------------------------------------------------------------------


def _partition_path(paths: DistanceWeightPaths, chunk_index: int) -> Path:
    return paths.partitioned_dir / f"chunk_{int(chunk_index):06d}.parquet"


def _filtered_lookup_scan(
    lookup_path: Path,
    *,
    source_type: str,
) -> pl.LazyFrame:
    """Load and validate the canonical source-target lookup.

    Exact schema is enforced by design. This prevents accidental reuse of old
    rich-schema or source_domain artifacts during clean rebuilds.
    """

    source_type = _validate_source_type(source_type)
    raw = pl.scan_parquet(str(lookup_path))
    schema = tuple(raw.collect_schema().names())
    expected = tuple(LOOKUP_SCHEMA)

    if schema != expected:
        raise ValueError(
            "Invalid source-target lookup schema for distance weights. "
            f"Expected exactly {list(expected)}; found {list(schema)}. "
            "Rerun prepare-source-target-lookup with --overwrite: "
            f"{lookup_path}"
        )

    return raw.filter(pl.col("source_type") == source_type)


def _select_distance_fields(lookup_scan: pl.LazyFrame) -> pl.LazyFrame:
    return lookup_scan.select(
        [
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
            pl.col("distance_km").cast(pl.Float64),
        ]
    )


def _partition_manifest_stats(path: Path) -> dict[str, Any]:
    row = (
        pl.scan_parquet(str(path))
        .select(
            [
                pl.len().alias("n_rows"),
                pl.col("source_h3").n_unique().alias("unique_source_h3"),
                pl.col("target_h3").n_unique().alias("unique_target_h3"),
                pl.col("distance_km").min().alias("distance_min_km"),
                pl.col("distance_km").max().alias("distance_max_km"),
                pl.col("distance_km").mean().alias("distance_mean_km"),
                pl.col("weight_distance").min().alias("weight_min"),
                pl.col("weight_distance").max().alias("weight_max"),
                pl.col("weight_distance").mean().alias("weight_mean"),
            ]
        )
        .collect()
        .row(0, named=True)
    )
    return dict(row)


def _partition_files_from_manifest_or_glob(paths: DistanceWeightPaths) -> list[Path]:
    if paths.manifest_path.exists():
        manifest = pd.read_csv(paths.manifest_path)
        if "distance_weight_partition" not in manifest.columns:
            raise ValueError(
                "Distance manifest missing distance_weight_partition column: "
                f"{paths.manifest_path}"
            )
        files = [Path(p) for p in manifest["distance_weight_partition"].dropna().tolist()]
    else:
        files = sorted(paths.partitioned_dir.glob("chunk_*.parquet"))

    files = [path for path in files if path.exists()]
    if not files:
        raise FileNotFoundError(
            f"No distance-weight partitions found in {paths.partitioned_dir}. "
            "Run build-distance-weights first."
        )
    return files


def _scan_distance_partitions(parts: Sequence[Path]) -> pl.LazyFrame:
    frames = [
        pl.scan_parquet(str(path)).select(
            [
                pl.col("source_h3").cast(pl.Utf8),
                pl.col("target_h3").cast(pl.Utf8),
                pl.col("distance_km").cast(pl.Float32),
                pl.col("weight_distance").cast(pl.Float32),
            ]
        )
        for path in parts
    ]
    if not frames:
        raise FileNotFoundError("No distance partitions selected.")
    return pl.concat(frames, how="vertical_relaxed")


# -----------------------------------------------------------------------------
# Build / combine / aggregate / clean
# -----------------------------------------------------------------------------


def build_distance_weight_partitions(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    *,
    limit: int | None = None,
    start: int = 0,
    progress_every: int = 1,
    source_type: str = "land",
) -> pd.DataFrame:
    """Build distance-weight partitions from the canonical lookup artifact."""

    lookup_path = final_artifact_paths(runtime.config_path).source_target_lookup
    if not lookup_path.exists():
        raise FileNotFoundError(
            "Distance weighting requires the canonical SOURCE_TARGET_LOOKUP artifact. "
            f"Run prepare-source-target-lookup first: {lookup_path}"
        )

    return build_distance_weight_partitions_from_lookup(
        runtime,
        cfg,
        lookup_path=lookup_path,
        limit=limit,
        start=start,
        progress_every=progress_every,
        source_type=source_type,
    )


def build_distance_weight_partitions_from_lookup(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    *,
    lookup_path: Path,
    limit: int | None = None,
    start: int = 0,
    progress_every: int = 1,
    source_type: str = "land",
) -> pd.DataFrame:
    """Transform canonical lookup distances into distance-weight partitions."""

    source_type = _validate_source_type(source_type)
    if start < 0:
        raise ValueError("start must be >= 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0 when provided")

    paths = distance_weight_paths(runtime, cfg, source_type)
    max_distance_km = resolved_max_distance_km(runtime, cfg)

    lookup_scan = _filtered_lookup_scan(lookup_path, source_type=source_type)
    lookup_scan = _select_distance_fields(lookup_scan).filter(
        pl.col("distance_km").is_not_null()
        & pl.col("distance_km").is_finite()
        & (pl.col("distance_km") >= 0)
    )

    source_cells = (
        lookup_scan.select("source_h3").unique().sort("source_h3").collect()["source_h3"].to_list()
    )
    selected_sources = [str(cell) for cell in source_cells[int(start) :]]
    if limit is not None:
        selected_sources = selected_sources[: int(limit)]
    if not selected_sources:
        raise ValueError(
            f"No source cells found in lookup for source_type={source_type!r}. "
            "Check that prepare-source-target-lookup emitted this source type: "
            f"{lookup_path}"
        )

    if cfg.overwrite and paths.partitioned_dir.exists():
        shutil.rmtree(paths.partitioned_dir)
    paths.partitioned_dir.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info(
        "Building %s-source distance weights lookup=%s selected_sources=%d "
        "start=%d limit=%s model=%s max_distance_km=%.3f",
        source_type,
        lookup_path,
        len(selected_sources),
        start,
        limit,
        cfg.selected_model,
        max_distance_km,
    )

    manifest_rows: list[dict[str, Any]] = []
    for chunk_number, source_start in enumerate(
        range(0, len(selected_sources), int(cfg.source_chunk_size))
    ):
        source_chunk = selected_sources[source_start : source_start + int(cfg.source_chunk_size)]
        global_source_start = int(start) + int(source_start)
        global_source_stop = global_source_start + len(source_chunk)
        global_chunk_number = global_source_start // int(cfg.source_chunk_size)
        out_path = _partition_path(paths, global_chunk_number)

        if out_path.exists() and not cfg.overwrite:
            existing_schema = tuple(pl.scan_parquet(str(out_path)).collect_schema().names())
            if existing_schema != DISTANCE_OUTPUT_SCHEMA:
                raise ValueError(
                    "Existing distance partition has stale schema. "
                    f"Expected {list(DISTANCE_OUTPUT_SCHEMA)}; found {list(existing_schema)}. "
                    f"Rerun with --overwrite: {out_path}"
                )
            stats = _partition_manifest_stats(out_path)
            status = "skipped_existing"
        else:
            out_lf = (
                lookup_scan.filter(pl.col("source_h3").is_in(source_chunk))
                .with_columns(distance_weight_expr(cfg, max_distance_km))
                .select(
                    [
                        pl.col("source_h3").cast(pl.Utf8),
                        pl.col("target_h3").cast(pl.Utf8),
                        pl.col("distance_km").cast(pl.Float32),
                        pl.col("weight_distance").cast(pl.Float32),
                    ]
                )
            )
            out_lf.sink_parquet(str(out_path))
            stats = _partition_manifest_stats(out_path)
            status = "ok"

        manifest_rows.append(
            {
                "distance_weight_partition": str(out_path),
                "status": status,
                "source_start": global_source_start,
                "source_stop": global_source_stop,
                "n_source_cells": len(source_chunk),
                "source_type": source_type,
                **stats,
            }
        )

        if progress_every > 0 and (
            (chunk_number + 1) % progress_every == 0
            or global_source_stop >= int(start) + len(selected_sources)
        ):
            LOGGER.info(
                "Distance chunk %d complete source_range=[%d,%d) rows=%s output=%s",
                global_chunk_number,
                global_source_start,
                global_source_stop,
                manifest_rows[-1].get("n_rows"),
                out_path,
            )

        pd.DataFrame(manifest_rows).to_csv(paths.manifest_path, index=False)

    manifest = pd.DataFrame(manifest_rows)
    if manifest.empty or int(manifest.get("n_rows", pd.Series(dtype=int)).sum()) == 0:
        raise ValueError(
            f"Distance build produced zero rows for source_type={source_type!r}. "
            f"Check lookup and max distance settings: {lookup_path}"
        )

    manifest.to_csv(paths.manifest_path, index=False)
    write_metadata_sidecar(
        paths.partitioned_dir,
        runtime.raw_config,
        {
            "step": "viewshed_distance_weights",
            "distance_weight_version": cfg.version,
            "distance_metric": "h3_centroid_great_circle_km_from_prepare_lookup",
            "input_lookup_path": str(lookup_path),
            "input_lookup_schema": list(LOOKUP_SCHEMA),
            "source_type": source_type,
            "selected_model": cfg.selected_model,
            "max_distance_km": max_distance_km,
            "source_chunk_size": cfg.source_chunk_size,
            "output_columns": list(DISTANCE_OUTPUT_SCHEMA),
        },
    )
    return manifest


def combine_distance_weight_partitions(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    partition_paths: Sequence[Path] | None = None,
    source_type: str = "land",
) -> dict[str, Any]:
    source_type = _validate_source_type(source_type)
    paths = distance_weight_paths(runtime, cfg, source_type)
    parts = (
        [Path(path) for path in partition_paths]
        if partition_paths is not None
        else _partition_files_from_manifest_or_glob(paths)
    )
    if not parts:
        raise FileNotFoundError("No distance partitions selected for combine.")

    LOGGER.info(
        "Materializing compact %s-source distance weights from %d partition(s)",
        source_type,
        len(parts),
    )
    output_path, row_count = materialize_distance_weights(
        parts,
        runtime.config_path,
        overwrite=cfg.overwrite,
        source_type=source_type,
    )
    LOGGER.info(
        "Wrote compact %s-source distance weights rows=%d output=%s",
        source_type,
        row_count,
        output_path,
    )
    return {"path": output_path, "row_count": row_count, "source_type": source_type}


def aggregate_distance_weight_partitions_to_target(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    *,
    source_type: str = "land",
    return_dataframe: bool = False,
) -> dict[str, Any] | pd.DataFrame:
    source_type = _validate_source_type(source_type)
    paths = distance_weight_paths(runtime, cfg, source_type)
    lf = _scan_distance_partitions(_partition_files_from_manifest_or_glob(paths))

    aggregations: list[pl.Expr] = [
        pl.col("source_h3").n_unique().alias("n_distance_weighted_sources"),
        pl.col("weight_distance").fill_null(0.0).sum().alias("weight_distance_sum"),
        pl.col("weight_distance").fill_null(0.0).mean().alias("weight_distance_mean"),
        pl.col("weight_distance").max().alias("weight_distance_max"),
        pl.col("distance_km").min().alias("nearest_source_distance_km"),
        pl.col("distance_km").mean().alias("mean_source_distance_km"),
        pl.lit(source_type).alias("source_type"),
        pl.lit(runtime.run_version).alias("run_version"),
        pl.lit(runtime.config_hash).alias("config_hash"),
    ]
    if cfg.p90_method == "exact" or cfg.compute_exact_p90:
        aggregations.insert(
            4,
            pl.col("weight_distance")
            .quantile(0.9, interpolation="linear")
            .alias("weight_distance_p90"),
        )
    else:
        aggregations.insert(4, pl.lit(None, dtype=pl.Float64).alias("weight_distance_p90"))

    out_lf = (
        lf.group_by("target_h3")
        .agg(aggregations)
        .sort(["weight_distance_sum", "weight_distance_max"], descending=[True, True])
    )

    if return_dataframe:
        return out_lf.collect().to_pandas()

    paths.target_aggregation_path.parent.mkdir(parents=True, exist_ok=True)
    if paths.target_aggregation_path.exists() and not cfg.overwrite:
        raise FileExistsError(f"Output exists and overwrite=False: {paths.target_aggregation_path}")
    out_lf.sink_parquet(str(paths.target_aggregation_path))
    row_count = int(
        pl.scan_parquet(str(paths.target_aggregation_path)).select(pl.len()).collect().item()
    )
    write_metadata_sidecar(
        paths.target_aggregation_path,
        runtime.raw_config,
        {
            "step": "viewshed_distance_weighted_target_summary",
            "distance_weight_version": cfg.version,
            "source_type": source_type,
            "selected_model": cfg.selected_model,
            "row_count": row_count,
        },
    )
    LOGGER.info(
        "Wrote target distance summary rows=%d output=%s",
        row_count,
        paths.target_aggregation_path,
    )
    return {
        "path": paths.target_aggregation_path,
        "row_count": row_count,
        "source_type": source_type,
    }


def aggregate_distance_weight_partitions_to_source(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    *,
    source_type: str = "land",
    return_dataframe: bool = False,
) -> dict[str, Any] | pd.DataFrame:
    source_type = _validate_source_type(source_type)
    paths = distance_weight_paths(runtime, cfg, source_type)
    lf = _scan_distance_partitions(_partition_files_from_manifest_or_glob(paths))

    aggregations: list[pl.Expr] = [
        pl.col("target_h3").n_unique().alias("n_distance_weighted_targets"),
        pl.col("weight_distance").fill_null(0.0).sum().alias("weight_distance_sum"),
        pl.col("weight_distance").fill_null(0.0).mean().alias("weight_distance_mean"),
        pl.col("weight_distance").max().alias("weight_distance_max"),
        pl.col("distance_km").mean().alias("mean_target_distance_km"),
        pl.col("distance_km").max().alias("max_target_distance_km"),
        pl.lit(source_type).alias("source_type"),
        pl.lit(runtime.run_version).alias("run_version"),
        pl.lit(runtime.config_hash).alias("config_hash"),
    ]
    if cfg.p90_method == "exact" or cfg.compute_exact_p90:
        aggregations.insert(
            4,
            pl.col("weight_distance")
            .quantile(0.9, interpolation="linear")
            .alias("weight_distance_p90"),
        )
    else:
        aggregations.insert(4, pl.lit(None, dtype=pl.Float64).alias("weight_distance_p90"))

    out_lf = lf.group_by("source_h3").agg(aggregations).sort("weight_distance_sum", descending=True)

    if return_dataframe:
        return out_lf.collect().to_pandas()

    paths.source_aggregation_path.parent.mkdir(parents=True, exist_ok=True)
    if paths.source_aggregation_path.exists() and not cfg.overwrite:
        raise FileExistsError(f"Output exists and overwrite=False: {paths.source_aggregation_path}")
    out_lf.sink_parquet(str(paths.source_aggregation_path))
    row_count = int(
        pl.scan_parquet(str(paths.source_aggregation_path)).select(pl.len()).collect().item()
    )
    write_metadata_sidecar(
        paths.source_aggregation_path,
        runtime.raw_config,
        {
            "step": "viewshed_distance_weighted_source_summary",
            "distance_weight_version": cfg.version,
            "source_type": source_type,
            "selected_model": cfg.selected_model,
            "row_count": row_count,
        },
    )
    LOGGER.info(
        "Wrote source distance summary rows=%d output=%s",
        row_count,
        paths.source_aggregation_path,
    )
    return {
        "path": paths.source_aggregation_path,
        "row_count": row_count,
        "source_type": source_type,
    }


def clean_distance_weight_outputs(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    source_type: str = "land",
) -> None:
    """Remove only this source-type distance scratch directory."""

    source_type = _validate_source_type(source_type)
    paths = distance_weight_paths(runtime, cfg, source_type)
    if paths.base_dir.exists():
        shutil.rmtree(paths.base_dir)
        LOGGER.info("Removed distance-weight directory: %s", paths.base_dir)
    for sidecar in metadata_sidecar_candidates(paths.base_dir):
        if sidecar.exists():
            sidecar.unlink()
            LOGGER.info("Removed distance-weight metadata sidecar: %s", sidecar)
    distance_root = paths.base_dir.parent
    if distance_root.exists() and distance_root.is_dir():
        try:
            distance_root.rmdir()
            LOGGER.info("Removed empty distance-weight root directory: %s", distance_root)
        except OSError:
            pass


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
