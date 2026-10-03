"""Canonical dual-surface canopy visibility composition.

The land-source model runs the same radius-based observer viewshed twice on an
identical source-target universe:

``K_bare``
    Mean bare-earth LOS multiplied by observer-to-water-pixel distance support.

``K_canopy``
    The same kernel on the DTM + CHM obstacle surface, with observers grounded
    to the DTM and water targets left on the endpoint DEM.

The durable factors are::

    weight_terrain = K_bare
    weight_vegetation = min(K_canopy, K_bare) / K_bare  when K_bare > 0
    static physical weight = weight_terrain * weight_vegetation

For pairs without baseline integrated support, ``weight_vegetation`` is neutral
1 bookkeeping; this does not establish a terrain obstruction. Land-cover attenuation is deliberately
outside this pipeline.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import polars as pl
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet, validate_parquet_schema

from ..config import (
    AppConfig,
    initialize_app_config,
    load_app_config,
    stable_config_hash,
    write_metadata_sidecar,
)
from ..contracts.artifacts import (
    FINAL_SCHEMAS,
    VEGETATION_STATUS_COMPUTED,
    FinalArtifactPaths,
    final_artifact_paths_from_raw,
)
from ..contracts.pairs import validate_pair_kernel, validate_los_diagnostics
from .terrain.runner import run_paired_surface_source_cells
from .terrain.gdal import expected_partition_metadata, partition_metadata_matches
from .terrain.cleanup import combine_partitions

CANOPY_VISIBILITY_CONTRACT_VERSION = "conditional_canopy_los_ratio_no_landcover_v1"
TERRAIN_FACTOR_DEFINITION = "mean_bare_los_times_observer_pixel_distance_weight"
CANOPY_FACTOR_DEFINITION = "mean_canopy_los_times_observer_pixel_distance_weight"


@dataclass(frozen=True)
class DualSurfaceCanopyResult:
    """Artifacts and diagnostics produced by one canonical dual-surface run."""

    terrain_weights: Path
    canopy_los_weights: Path
    vegetation_weights: Path
    dual_surface_factors: Path
    bare_earth_partition_dir: Path
    canopy_partition_dir: Path
    diagnostics: dict[str, float | int | str]


def canopy_scenario_metadata(app: AppConfig) -> dict[str, Any]:
    """Return a reproducible identifier for the CHM obstruction assumptions."""

    canopy_path = Path(app.paths.canopy_height_path).expanduser().resolve()
    canopy_identity: dict[str, Any] = {"path": str(canopy_path)}
    if canopy_path.exists():
        stat = canopy_path.stat()
        canopy_identity.update({"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
    scenario = {
        "canopy_input": canopy_identity,
        "canopy_resampling": app.viewshed.canopy_resampling,
        "canopy_nodata_policy": app.viewshed.canopy_nodata_policy,
        "minimum_canopy_height_m": float(app.viewshed.minimum_canopy_height_m),
        "observer_canopy_clearance_radius_m": float(
            app.viewshed.observer_canopy_clearance_radius_m
        ),
        "observer_eye_height_m": float(app.viewshed.observer_eye_height_m),
        "target_height_m": float(app.viewshed.target_height_m),
        "dem_resolution_m": int(app.viewshed.dem_resolution_m),
        "surface_contract": CANOPY_VISIBILITY_CONTRACT_VERSION,
    }
    return {
        "canopy_scenario_id": f"canopy-{stable_config_hash(scenario, length=16)}",
        "canopy_scenario": scenario,
    }


def make_terrain_variant_app(
    app: AppConfig,
    *,
    surface_model: str,
    terrain_dir: Path,
    config_hash_function: Callable[[dict[str, Any]], str] = stable_config_hash,
) -> AppConfig:
    """Return an isolated AppConfig for one of the two terrain surfaces."""

    surface_model = str(surface_model).strip().lower()
    if surface_model not in {"bare_earth", "canopy"}:
        raise ValueError("surface_model must be 'bare_earth' or 'canopy'.")

    terrain_dir = Path(terrain_dir)
    run_name = f"{app.run.name}_{surface_model}"
    raw = copy.deepcopy(app.raw_config)
    raw.setdefault("viewshed", {})["surface_model"] = surface_model
    raw.setdefault("run", {}).update({"name": run_name, "version": run_name})
    raw.setdefault("paths", {}).update(
        {
            "partitioned_visibility_dir": str(terrain_dir / "partitions"),
            "manifest_path": str(terrain_dir / "terrain_manifest.csv"),
            "final_visibility_path": str(terrain_dir / "terrain_edges.parquet"),
        }
    )
    return replace(
        app,
        raw_config=raw,
        config_hash=config_hash_function(raw),
        run=replace(app.run, name=run_name, version=run_name, combine_final_parquet=False),
        paths=replace(
            app.paths,
            partitioned_visibility_dir=terrain_dir / "partitions",
            manifest_path=terrain_dir / "terrain_manifest.csv",
            final_visibility_path=terrain_dir / "terrain_edges.parquet",
        ),
        viewshed=replace(app.viewshed, surface_model=surface_model),
        source_type="land",
    )


def terrain_partition_paths(
    partitioned_visibility_dir: Path,
    *,
    selected_sources: set[str] | None = None,
) -> list[Path]:
    """Return only the requested land-source partition files when specified."""

    partition_dir = Path(partitioned_visibility_dir) / "source=land"
    if selected_sources is None:
        return sorted(partition_dir.glob("source_h3_cell=*.parquet"))

    normalized_sources = sorted({str(value) for value in selected_sources})
    paths: list[Path] = []
    for source_h3 in normalized_sources:
        filename = f"source_h3_cell={source_h3}.parquet"
        if Path(filename).name != filename:
            raise ValueError(f"Invalid source H3 partition identifier: {source_h3!r}")
        paths.append(partition_dir / filename)
    return paths


def scan_terrain_partitions(
    partitioned_visibility_dir: Path,
    *,
    selected_sources: set[str] | None = None,
) -> pl.LazyFrame:
    """Lazily scan typed sparse terrain partitions after source pruning."""

    if selected_sources is not None and not selected_sources:
        raise ValueError("selected_sources cannot be empty.")
    paths = terrain_partition_paths(
        partitioned_visibility_dir,
        selected_sources=selected_sources,
    )
    if not paths:
        raise FileNotFoundError(
            "No terrain partitions found at " f"{Path(partitioned_visibility_dir) / 'source=land'}"
        )
    missing_paths = [path for path in paths if not path.is_file()]
    if missing_paths:
        raise FileNotFoundError(
            f"Missing {len(missing_paths)} requested terrain partition(s): "
            f"{[str(path) for path in missing_paths[:10]]}"
        )
    scans = [
        pl.scan_parquet(str(path)).with_columns(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
        )
        for path in paths
    ]
    combined = pl.concat(scans, how="vertical")
    if selected_sources is not None:
        normalized_sources = sorted({str(value) for value in selected_sources})
        combined = combined.filter(pl.col("source_h3").is_in(normalized_sources))
    return combined.unique(["source_h3", "target_h3"], keep="first")


def _has_unweighted_los(paths: Sequence[Path]) -> bool:
    frame = pl.scan_parquet([str(path) for path in paths])
    columns = frame.collect_schema().names()
    return "joint_los_fraction" in columns and (
        "unweighted_los_observed" not in columns
        or bool(frame.select(pl.col("unweighted_los_observed").all()).collect().item())
    )


def _terrain_kernel_scan(paths: Sequence[Path], output_name: str) -> pl.LazyFrame:
    """Validate observed keys and weights before composing sparse kernels.

    Validation uses bounded lazy results, then composition retains a multi-file
    scan so regional partitions do not require one independent plan per file.
    """

    if not paths:
        raise FileNotFoundError(f"No {output_name} terrain partitions were supplied.")
    # One LazyFrame per partition causes Polars to schedule thousands of
    # independent blocking scans at regional scale.  Besides unnecessary plan
    # overhead, that can exhaust Polars' blocking-thread/file-handle ceiling
    # before the streaming sink begins.  A single multi-file scan lets the
    # Parquet reader bound file access internally.
    frame = pl.scan_parquet([str(path) for path in paths])
    validate_pair_kernel(frame, weight="weight_terrain")
    validate_los_diagnostics(frame)
    columns = frame.collect_schema().names()
    observed = (
        pl.col("unweighted_los_observed")
        if "unweighted_los_observed" in columns
        else pl.lit("joint_los_fraction" in columns)
    )
    los = (
        pl.when(observed).then(pl.col("joint_los_fraction")).otherwise(None)
        if "joint_los_fraction" in columns
        else pl.lit(None, dtype=pl.Float32)
    )
    return frame.select(
        los.cast(pl.Float32).alias(
            "bare_los_fraction" if output_name == "weight_terrain" else "canopy_los_fraction"
        ),
        pl.lit(True).alias(
            "__bare_observed" if output_name == "weight_terrain" else "__canopy_observed"
        ),
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
        pl.col("weight_terrain").cast(pl.Float32).alias(output_name),
    )


def compose_dual_surface_artifacts(
    *,
    lookup_path: Path,
    bare_earth_partition_paths: Sequence[Path],
    canopy_partition_paths: Sequence[Path],
    paths: FinalArtifactPaths,
    raw_config: dict[str, Any],
    config_dir: Path,
    bare_config_hash: str,
    canopy_config_hash: str,
    bare_completed_sources: Sequence[str],
    canopy_completed_sources: Sequence[str],
    canopy_scenario_id: str | None = None,
    canopy_scenario: dict[str, Any] | None = None,
) -> DualSurfaceCanopyResult:
    """Compose and atomically persist the scalable production factor tables."""

    lookup = (
        pl.scan_parquet(str(lookup_path))
        .filter(pl.col("source_type") == "land")
        .select(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
        )
    )
    validate_pair_kernel(lookup)
    expected_sources = set(lookup.select("source_h3").unique().collect()["source_h3"])
    for completed in (bare_completed_sources, canopy_completed_sources):
        if len(completed) != len(set(completed)) or set(completed) != expected_sources:
            raise ValueError("Incomplete paired LOS source execution; cannot infer observed zero")
    bare = _terrain_kernel_scan(bare_earth_partition_paths, "weight_terrain")
    canopy = _terrain_kernel_scan(canopy_partition_paths, "weight_canopy_los_raw")
    composed = (
        lookup.join(
            bare,
            on=["source_h3", "target_h3"],
            how="left",
            validate="1:1",
        )
        .join(
            canopy,
            on=["source_h3", "target_h3"],
            how="left",
            validate="1:1",
        )
        .with_columns(
            pl.when(pl.col("__bare_observed").is_null())
            .then(
                pl.when(pl.lit(_has_unweighted_los(bare_earth_partition_paths)))
                .then(0.0)
                .otherwise(None)
            )
            .otherwise(pl.col("bare_los_fraction"))
            .alias("bare_los_fraction"),
            pl.when(pl.col("__canopy_observed").is_null())
            .then(
                pl.when(pl.lit(_has_unweighted_los(canopy_partition_paths)))
                .then(0.0)
                .otherwise(None)
            )
            .otherwise(pl.col("canopy_los_fraction"))
            .alias("canopy_los_fraction"),
            pl.col("weight_terrain").fill_null(0.0).clip(0.0, 1.0),
            pl.col("weight_canopy_los_raw").fill_null(0.0).clip(0.0, 1.0),
        )
        .with_columns(
            pl.min_horizontal("weight_terrain", "weight_canopy_los_raw")
            .cast(pl.Float32)
            .alias("weight_canopy_los")
        )
        .with_columns(
            pl.when(pl.col("weight_terrain") > 0.0)
            .then(pl.col("weight_canopy_los") / pl.col("weight_terrain"))
            .otherwise(1.0)
            .clip(0.0, 1.0)
            .cast(pl.Float32)
            .alias("weight_vegetation"),
            pl.lit("land").alias("source_type"),
            pl.lit(VEGETATION_STATUS_COMPUTED).alias("vegetation_status"),
            pl.lit(CANOPY_VISIBILITY_CONTRACT_VERSION).alias("vegetation_provenance"),
        )
        .select(list(FINAL_SCHEMAS["dual_surface_factors"]))
    )

    try:
        atomic_sink_parquet(composed, paths.dual_surface_factors, overwrite=True)
    except pl.exceptions.ComputeError as exc:
        if "validation" not in str(exc).lower():
            raise
        raise ValueError(
            "Duplicate source-target pairs found while composing the land lookup, "
            "bare-earth partitions, or canopy partitions."
        ) from exc
    validate_parquet_schema(paths.dual_surface_factors, FINAL_SCHEMAS["dual_surface_factors"])
    persisted = pl.scan_parquet(str(paths.dual_surface_factors))
    atomic_sink_parquet(
        persisted.select(list(FINAL_SCHEMAS["terrain_weights"])),
        paths.terrain_weights,
        overwrite=True,
    )
    atomic_sink_parquet(
        persisted.select(list(FINAL_SCHEMAS["canopy_los_weights"])),
        paths.canopy_los_weights,
        overwrite=True,
    )
    atomic_sink_parquet(
        persisted.select(list(FINAL_SCHEMAS["vegetation_weights"])),
        paths.vegetation_weights,
        overwrite=True,
    )
    for artifact_name, artifact_path in (
        ("terrain_weights", paths.terrain_weights),
        ("canopy_los_weights", paths.canopy_los_weights),
        ("vegetation_weights", paths.vegetation_weights),
    ):
        validate_parquet_schema(artifact_path, FINAL_SCHEMAS[artifact_name])

    metrics = (
        persisted.select(
            pl.len().alias("pair_count"),
            (pl.col("canopy_los_fraction") > pl.col("bare_los_fraction") + 1e-6)
            .sum()
            .alias("unweighted_canopy_exceeds_bare_pair_count"),
            ((pl.col("weight_canopy_los_raw") - pl.col("weight_terrain")) > 1e-6)
            .sum()
            .alias("substantial_canopy_excess_pair_count"),
            (pl.col("weight_terrain") > 0.0).sum().alias("bare_earth_visible_pair_count"),
            (pl.col("weight_canopy_los") > 0.0).sum().alias("canopy_visible_pair_count"),
            (pl.col("weight_canopy_los_raw") > pl.col("weight_terrain"))
            .sum()
            .alias("canopy_exceeds_bare_pair_count_before_cap"),
            (pl.col("weight_canopy_los_raw") - pl.col("weight_terrain"))
            .clip(lower_bound=0.0)
            .max()
            .fill_null(0.0)
            .alias("maximum_canopy_excess_before_cap"),
        )
        .collect()
        .row(0, named=True)
    )
    diagnostics: dict[str, float | int | str] = {
        **{key: value for key, value in metrics.items()},
        "contract_version": CANOPY_VISIBILITY_CONTRACT_VERSION,
        "terrain_factor_definition": TERRAIN_FACTOR_DEFINITION,
        "canopy_los_factor_definition": CANOPY_FACTOR_DEFINITION,
        "vegetation_factor_definition": "canopy_support_divided_by_bare_earth_support",
        "landcover_used": "false",
        "bare_earth_config_hash": bare_config_hash,
        "canopy_config_hash": canopy_config_hash,
        "canopy_scenario_id": str(canopy_scenario_id or f"canopy-config-{canopy_config_hash}"),
    }
    metadata = {
        "step": "dual_surface_canopy_visibility",
        **diagnostics,
        "lookup_path": str(Path(lookup_path).resolve()),
        "distance_role": "integrated_inside_each_terrain_kernel",
        "physical_static_formula": "weight_terrain * weight_vegetation",
        "canopy_scenario": canopy_scenario or {},
    }
    for artifact_path in (
        paths.dual_surface_factors,
        paths.terrain_weights,
        paths.canopy_los_weights,
        paths.vegetation_weights,
    ):
        write_metadata_sidecar(artifact_path, raw_config, metadata)

    return DualSurfaceCanopyResult(
        terrain_weights=paths.terrain_weights,
        canopy_los_weights=paths.canopy_los_weights,
        vegetation_weights=paths.vegetation_weights,
        dual_surface_factors=paths.dual_surface_factors,
        bare_earth_partition_dir=Path(bare_earth_partition_paths[0]).parent,
        canopy_partition_dir=Path(canopy_partition_paths[0]).parent,
        diagnostics=diagnostics,
    )


def run_dual_surface_canopy_weights(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    max_workers: int | None = None,
    batch_size: int | None = None,
) -> DualSurfaceCanopyResult:
    """Run both land surfaces and persist the canonical conditional canopy factor."""

    app = load_app_config(config_path)
    canopy_contract = app.raw_config.get("canopy_visibility", {}) or {}
    configured_contract = str(canopy_contract.get("contract", CANOPY_VISIBILITY_CONTRACT_VERSION))
    if configured_contract != CANOPY_VISIBILITY_CONTRACT_VERSION:
        raise ValueError(
            "Unsupported canopy_visibility.contract. Expected "
            f"{CANOPY_VISIBILITY_CONTRACT_VERSION!r}; got {configured_contract!r}."
        )
    if bool(canopy_contract.get("landcover_used", False)):
        raise ValueError(
            "The canonical dual-surface canopy visibility stage does not use land cover. "
            "Run land-cover attenuation as a separate pipeline."
        )
    app = replace(
        app,
        source_type="land",
        run=replace(app.run, overwrite=bool(overwrite), combine_final_parquet=False),
        batch=replace(
            app.batch,
            max_workers=(int(max_workers) if max_workers is not None else app.batch.max_workers),
            batch_size_cells=(
                int(batch_size) if batch_size is not None else app.batch.batch_size_cells
            ),
        ),
    )
    terrain_root = app.paths.output_dir / "land" / "terrain_surfaces"
    bare_app = initialize_app_config(
        make_terrain_variant_app(
            app,
            surface_model="bare_earth",
            terrain_dir=terrain_root / "bare_earth",
        )
    )
    canopy_app = initialize_app_config(
        make_terrain_variant_app(
            app,
            surface_model="canopy",
            terrain_dir=terrain_root / "canopy",
        )
    )

    bare_completed, canopy_completed = run_paired_surface_source_cells(bare_app, canopy_app)
    completed_sources = []
    selected_partitions = []
    for surface_app, completed in ((bare_app, bare_completed), (canopy_app, canopy_completed)):
        if not {"source_h3_cell", "status"}.issubset(completed.columns):
            raise ValueError("Missing paired LOS completion records")
        if not completed["status"].isin(["ok", "skipped_existing"]).all():
            raise ValueError("Failed paired LOS source execution")
        sources = completed["source_h3_cell"].astype(str).tolist()
        partitions = terrain_partition_paths(
            surface_app.paths.partitioned_visibility_dir, selected_sources=set(sources)
        )
        expected = expected_partition_metadata(surface_app)
        if any(not partition_metadata_matches(path, expected) for path in partitions):
            raise ValueError("Missing or stale paired terrain partition")
        completed_sources.append(sources)
        selected_partitions.append(partitions)
    bare_partitions, canopy_partitions = selected_partitions
    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    scenario_metadata = canopy_scenario_metadata(app)
    result = compose_dual_surface_artifacts(
        lookup_path=paths.source_target_lookup,
        bare_earth_partition_paths=bare_partitions,
        canopy_partition_paths=canopy_partitions,
        paths=paths,
        raw_config=app.raw_config,
        config_dir=app.config_path.parent,
        bare_config_hash=bare_app.config_hash,
        canopy_config_hash=canopy_app.config_hash,
        bare_completed_sources=completed_sources[0],
        canopy_completed_sources=completed_sources[1],
        canopy_scenario_id=str(scenario_metadata["canopy_scenario_id"]),
        canopy_scenario=dict(scenario_metadata["canopy_scenario"]),
    )

    # Only bare-earth diagnostics own the land clear-sky product. Canopy
    # combination must not overwrite it with the obstruction-surface kernel.
    combine_partitions(bare_app, partition_paths=bare_partitions)
    return result
