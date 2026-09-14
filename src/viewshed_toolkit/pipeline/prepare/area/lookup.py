"""Build the canonical source-target H3 lookup universe.

This module owns the canonical pair universe for the viewshed weighting stages.
The production lookup is deliberately narrow and strict:

    source_h3, target_h3, distance_km, source_type

``source_type`` is the modeling role of the observer source, either ``land`` or
``water``. Physical land/water composition is used internally to construct the
source and target universes, but it is not persisted in the production lookup.

Design contract
---------------
- ``prepare_area.py`` decides which source-target pairs exist.
- ``distance.py`` transforms ``distance_km`` into ``weight_distance``.
- ``terrain.py`` transforms the same pair universe into ``weight_terrain``.
- Production artifacts stay compact; QA/intermediate details stay out of the
  canonical lookup.
"""

from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import polars as pl
from shapely.geometry.base import BaseGeometry

from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ...config import write_metadata_sidecar
from ...config.distance import (
    DistanceRuntime,
    DistanceWeightConfig,
    load_distance_runtime,
    load_distance_weight_config,
)
from ...contracts.artifacts import final_artifact_paths
from ...contracts.pairs import (
    EARTH_RADIUS_KM,
    SOURCE_TARGET_LOOKUP_SCHEMA,
    SOURCE_TYPES,
)
from ...contracts.cleanup import cleanup_data_contract
from . import domains

LOGGER = logging.getLogger(__name__)

_ALLOWED_SOURCE_TYPES = SOURCE_TYPES


from .config import (
    AreaLookupResult,
    SourceTargetLookupConfig,
    _lookup_config,
    _lookup_metadata_extra,
    _lookup_partition_dir,
    _lookup_partition_path,
    _validate_existing_lookup_metadata,
)
from .geometry import h3_geometry_artifact_path, load_h3_geometry_lookup
from .universe import _build_source_universe


def _haversine_expr(
    source_lat_col: str,
    source_lon_col: str,
    target_lat_col: str,
    target_lon_col: str,
) -> pl.Expr:
    lat1 = pl.col(source_lat_col).radians()
    lon1 = pl.col(source_lon_col).radians()
    lat2 = pl.col(target_lat_col).radians()
    lon2 = pl.col(target_lon_col).radians()
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (dlat / 2.0).sin().pow(2) + lat1.cos() * lat2.cos() * (dlon / 2.0).sin().pow(2)
    return 2.0 * EARTH_RADIUS_KM * a.sqrt().arcsin()


def _empty_lookup_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "source_h3": pl.Utf8,
            "target_h3": pl.Utf8,
            "distance_km": pl.Float32,
            "source_type": pl.Utf8,
        }
    )


def _build_pair_chunk_polars(
    source_cells: Sequence[str],
    *,
    target_cells: set[str],
    centroid_df: pl.DataFrame,
    source_type: str,
    max_distance_km: float,
    grid_disk_k: int,
    allow_self_pairs: bool,
    allow_active_role_self_pairs: bool = True,
    projected_cell_geometries: Mapping[str, BaseGeometry] | None = None,
    max_candidate_pairs_per_chunk: int | None = None,
) -> tuple[pl.DataFrame, int]:
    source_type = str(source_type).lower().strip()
    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(_ALLOWED_SOURCE_TYPES)}")

    rows: list[tuple[str, str]] = []
    candidate_count = 0
    for source in source_cells:
        candidates = domains.h3_grid_disk(str(source), int(grid_disk_k)).intersection(target_cells)
        if not allow_self_pairs and not allow_active_role_self_pairs:
            candidates.discard(str(source))
        candidate_count += len(candidates)
        if candidates:
            source_id = str(source)
            rows.extend((source_id, str(target)) for target in candidates)
        if max_candidate_pairs_per_chunk is not None and len(rows) > int(
            max_candidate_pairs_per_chunk
        ):
            raise MemoryError(
                "Candidate pair count exceeded source_target_lookup.max_candidate_pairs_per_chunk "
                f"({max_candidate_pairs_per_chunk}) for source_type={source_type}. "
                "Lower source_chunk_size, lower max_grid_disk_k/max_distance_km, or raise the cap."
            )

    if not rows:
        return _empty_lookup_frame(), candidate_count

    pairs = pl.DataFrame(rows, schema=["source_h3", "target_h3"], orient="row")

    centroid_lf = centroid_df.lazy()
    source_centroids = centroid_lf.select(
        pl.col("h3").alias("source_h3"),
        pl.col("lat").alias("source_lat"),
        pl.col("lon").alias("source_lon"),
    )
    target_centroids = centroid_lf.select(
        pl.col("h3").alias("target_h3"),
        pl.col("lat").alias("target_lat"),
        pl.col("lon").alias("target_lon"),
    )

    out = (
        pairs.lazy()
        .join(source_centroids, on="source_h3", how="left")
        .join(target_centroids, on="target_h3", how="left")
        .with_columns(
            _haversine_expr("source_lat", "source_lon", "target_lat", "target_lon").alias(
                "distance_km"
            )
        )
        .filter(pl.col("distance_km").is_not_null())
        .with_columns(pl.lit(source_type).alias("source_type"))
        .select(
            [
                pl.col("source_h3").cast(pl.Utf8),
                pl.col("target_h3").cast(pl.Utf8),
                pl.col("distance_km").cast(pl.Float32),
                pl.col("source_type").cast(pl.Utf8),
            ]
        )
        .collect()
    )

    # Centroid distance is retained only as a diagnostic. The candidate
    # universe is based on the minimum distance between complete H3 polygons so
    # a near source/target edge is not discarded merely because the two
    # centroids lie outside the physical cutoff. The terrain kernel applies the
    # cutoff to actual observer/target-water-pixel distances.
    within = out["distance_km"].to_numpy() <= float(max_distance_km)
    if not np.all(within):
        if projected_cell_geometries is None:
            raise ValueError(
                "Geometry-aware source-target cutoff requires projected H3 cell geometries."
            )
        source_values = out["source_h3"].to_list()
        target_values = out["target_h3"].to_list()
        for index in np.flatnonzero(~within):
            source_geometry = projected_cell_geometries.get(str(source_values[index]))
            target_geometry = projected_cell_geometries.get(str(target_values[index]))
            if source_geometry is None or target_geometry is None:
                raise KeyError(
                    "Missing projected H3 geometry for source-target cutoff: "
                    f"source={source_values[index]} target={target_values[index]}"
                )
            within[index] = (
                float(source_geometry.distance(target_geometry))
                <= float(max_distance_km) * 1_000.0 + 1e-6
            )
    out = out.filter(pl.Series("within_physical_cutoff", within))
    return out, candidate_count


def _build_and_write_pair_chunk(
    spec: tuple[int, str, list[str], float, int],
    *,
    partition_dir: Path,
    target_cells: set[str],
    centroid_df: pl.DataFrame,
    allow_self_pairs: bool,
    allow_active_role_self_pairs: bool,
    projected_cell_geometries: Mapping[str, BaseGeometry],
    max_candidate_pairs_per_chunk: int | None,
) -> tuple[int, Path | None, int, int, str, int]:
    chunk_index, source_type, source_chunk, max_distance_km, grid_disk_k = spec
    lookup, candidate_count = _build_pair_chunk_polars(
        source_chunk,
        target_cells=target_cells,
        centroid_df=centroid_df,
        source_type=source_type,
        max_distance_km=max_distance_km,
        grid_disk_k=grid_disk_k,
        allow_self_pairs=allow_self_pairs,
        allow_active_role_self_pairs=allow_active_role_self_pairs,
        projected_cell_geometries=projected_cell_geometries,
        max_candidate_pairs_per_chunk=max_candidate_pairs_per_chunk,
    )
    kept_count = int(lookup.height)
    if kept_count <= 0:
        return (
            chunk_index,
            None,
            kept_count,
            candidate_count,
            source_type,
            len(source_chunk),
        )

    out_path = _lookup_partition_path(partition_dir, chunk_index)
    lookup.write_parquet(out_path)
    return (
        chunk_index,
        out_path,
        kept_count,
        candidate_count,
        source_type,
        len(source_chunk),
    )


# -----------------------------------------------------------------------------
# Validation / reuse
# -----------------------------------------------------------------------------


def _scan_lookup_required(path: Path) -> pl.LazyFrame:
    if not path.exists():
        raise FileNotFoundError(f"Source-target lookup does not exist: {path}")
    lf = pl.scan_parquet(str(path))
    cols = lf.collect_schema().names()
    if tuple(cols) != SOURCE_TARGET_LOOKUP_SCHEMA:
        raise ValueError(
            "Source-target lookup must use exact canonical schema/order: "
            f"{SOURCE_TARGET_LOOKUP_SCHEMA}. Found {tuple(cols)} at {path}"
        )
    return lf


def _validate_lookup_artifact(
    lookup_path: Path,
    lookup_cfg: SourceTargetLookupConfig,
) -> tuple[int, int, int]:
    lf = _scan_lookup_required(lookup_path)
    summary = lf.select(
        pl.len().alias("n_rows"),
        pl.col("source_h3").n_unique().alias("n_source_cells"),
        pl.col("target_h3").n_unique().alias("n_target_cells"),
        pl.col("distance_km").min().alias("min_distance_km"),
        pl.col("distance_km").max().alias("max_distance_km"),
    ).collect()
    row = summary.row(0, named=True)
    n_rows = int(row["n_rows"])
    if n_rows <= 0:
        raise ValueError(f"Source-target lookup is empty: {lookup_path}")

    source_types = set(lf.select(pl.col("source_type").unique()).collect()["source_type"].to_list())
    bad_source_types = source_types - _ALLOWED_SOURCE_TYPES
    if bad_source_types:
        raise ValueError(
            f"Source-target lookup has invalid source_type values: {sorted(bad_source_types)}"
        )

    if row["min_distance_km"] is None or float(row["min_distance_km"]) < 0:
        raise ValueError("Source-target lookup contains negative or null distances.")

    max_allowed = max(
        float(lookup_cfg.max_distance_km_land or 0.0),
        float(lookup_cfg.max_distance_km_water or 0.0),
    )
    edge_km = float(domains.AVG_H3_EDGE_LENGTH_KM[int(lookup_cfg.h3_resolution or 0)])
    maximum_centroid_halo_km = 4.0 * edge_km
    if float(row["max_distance_km"]) > max_allowed + maximum_centroid_halo_km + 1e-4:
        raise ValueError(
            "Source-target lookup contains centroid distances beyond the geometry-aware "
            "candidate halo: "
            f"max={row['max_distance_km']} cutoff={max_allowed} "
            f"halo={maximum_centroid_halo_km}"
        )

    if not lookup_cfg.allow_self_pairs and not lookup_cfg.allow_active_role_self_pairs:
        n_self = lf.filter(pl.col("source_h3") == pl.col("target_h3")).limit(1).collect().height
        if n_self:
            raise ValueError(
                "Source-target lookup contains self-pair row(s), but allow_self_pairs=False."
            )

    n_dupes = (
        lf.group_by(["source_h3", "target_h3", "source_type"])
        .agg(pl.len().alias("n"))
        .filter(pl.col("n") > 1)
        .limit(1)
        .collect()
        .height
    )
    if n_dupes:
        raise ValueError(
            "Source-target lookup contains duplicate source_h3/target_h3/source_type keys."
        )

    return n_rows, int(row["n_source_cells"]), int(row["n_target_cells"])


def _reuse_existing_lookup_if_valid(
    lookup_path: Path,
    partition_dir: Path,
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
) -> AreaLookupResult | None:
    if not lookup_path.exists():
        return None
    _validate_existing_lookup_metadata(lookup_path, runtime, cfg, lookup_cfg)
    n_pairs, n_source_cells, n_target_cells = _validate_lookup_artifact(lookup_path, lookup_cfg)
    LOGGER.info("Reusing validated source-target lookup: %s rows=%d", lookup_path, n_pairs)
    return AreaLookupResult(
        lookup_path=lookup_path,
        partition_dir=partition_dir,
        n_pairs=n_pairs,
        n_source_cells=n_source_cells,
        n_target_cells=n_target_cells,
        h3_resolution=runtime.source_resolution,
    )


# -----------------------------------------------------------------------------
# Public builder
# -----------------------------------------------------------------------------


def build_source_target_lookup(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    combine: bool = True,
    limit_per_source_type: int | None = None,
    limit: int | None = None,
    clean_intermediates: bool = False,
) -> AreaLookupResult:
    """Build the canonical slim source-target lookup.

    ``limit`` is kept as a deprecated alias for ``limit_per_source_type`` so old
    smoke-test commands do not fail abruptly.
    """

    if limit_per_source_type is None and limit is not None:
        LOGGER.warning(
            "--limit is deprecated for source-target lookup; use --limit-per-source-type."
        )
        limit_per_source_type = limit

    runtime = load_distance_runtime(config_path)
    cfg = load_distance_weight_config(runtime.raw_config)
    if overwrite:
        cfg = DistanceWeightConfig(**{**cfg.__dict__, "overwrite": True})
    lookup_cfg = _lookup_config(runtime.raw_config, runtime, cfg)

    lookup_path = final_artifact_paths(config_path).source_target_lookup
    partition_dir = _lookup_partition_dir(lookup_path, config_path)

    if not overwrite:
        try:
            existing = _reuse_existing_lookup_if_valid(
                lookup_path, partition_dir, runtime, cfg, lookup_cfg
            )
        except ValueError as exc:
            raise ValueError(f"Cannot reuse existing source-target lookup: {exc}") from exc
        if existing is not None:
            return existing

    if overwrite:
        lookup_path.unlink(missing_ok=True)
        for meta_path in [
            lookup_path.with_name(f"{lookup_path.stem}_metadata.json"),
            Path(f"{lookup_path}.metadata.json"),
        ]:
            meta_path.unlink(missing_ok=True)

    # Scratch chunks are intentionally not reused. This stage is the canonical
    # universe-builder; stale chunks are more dangerous than a clean rebuild.
    if partition_dir.exists():
        shutil.rmtree(partition_dir)
    partition_dir.mkdir(parents=True, exist_ok=True)

    universe = _build_source_universe(
        config_path,
        runtime=runtime,
        cfg=cfg,
        lookup_cfg=lookup_cfg,
        overwrite=overwrite,
    )

    land_source_cells = universe.land_source_cells
    water_source_cells = universe.water_source_cells
    target_cells = universe.target_cells

    if limit_per_source_type is not None:
        if int(limit_per_source_type) <= 0:
            raise ValueError("limit_per_source_type must be a positive integer when provided.")
        land_source_cells = land_source_cells[: int(limit_per_source_type)]
        water_source_cells = water_source_cells[: int(limit_per_source_type)]

    all_pair_cells = sorted(set(land_source_cells).union(water_source_cells).union(target_cells))
    if not all_pair_cells:
        raise ValueError("No source/target cells available for lookup construction.")

    centroid_pd = domains._build_centroid_lookup(all_pair_cells)
    centroid_df = pl.DataFrame(
        {
            "h3": centroid_pd.index.astype(str).tolist(),
            "lat": centroid_pd["lat"].astype(float).tolist(),
            "lon": centroid_pd["lon"].astype(float).tolist(),
        }
    )
    geometry_path = h3_geometry_artifact_path(runtime.output_dir, runtime.source_resolution)
    all_projected_geometries = load_h3_geometry_lookup(geometry_path, "geometry_projected")
    projected_cell_geometries = {cell: all_projected_geometries[cell] for cell in all_pair_cells}

    land_grid_disk_k = domains.estimate_grid_disk_k(
        runtime.source_resolution,
        float(lookup_cfg.max_distance_km_land or 0.0),
        override_k=lookup_cfg.max_grid_disk_k,
    )
    water_grid_disk_k = domains.estimate_grid_disk_k(
        runtime.source_resolution,
        float(lookup_cfg.max_distance_km_water or 0.0),
        override_k=lookup_cfg.max_grid_disk_k,
    )
    LOGGER.info(
        "Source-target lookup universe land_sources=%d water_sources=%d targets=%d "
        "grid_disk_k_land=%d grid_disk_k_water=%d",
        len(land_source_cells),
        len(water_source_cells),
        len(target_cells),
        land_grid_disk_k,
        water_grid_disk_k,
    )

    worker_count = int(
        lookup_cfg.parallel_workers
        or (runtime.raw_config.get("batch", {}) or {}).get("max_workers", 1)
        or 1
    )
    worker_count = max(1, worker_count)

    chunk_specs: list[tuple[int, str, list[str], float, int]] = []
    chunk_index = 0
    for source_type, source_cells, max_distance_km, grid_disk_k in [
        (
            "land",
            land_source_cells,
            float(lookup_cfg.max_distance_km_land or 0.0),
            land_grid_disk_k,
        ),
        (
            "water",
            water_source_cells,
            float(lookup_cfg.max_distance_km_water or 0.0),
            water_grid_disk_k,
        ),
    ]:
        if not source_cells:
            LOGGER.info("No %s source cells selected; skipping.", source_type)
            continue
        for source_start in range(0, len(source_cells), int(cfg.source_chunk_size)):
            source_chunk = source_cells[source_start : source_start + int(cfg.source_chunk_size)]
            chunk_specs.append(
                (
                    chunk_index,
                    source_type,
                    list(source_chunk),
                    max_distance_km,
                    grid_disk_k,
                )
            )
            chunk_index += 1

    partition_paths: list[Path] = []
    total_pairs = 0
    total_candidate_pairs = 0
    if worker_count > 1 and len(chunk_specs) > 1:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            chunk_results = list(
                pool.map(
                    lambda spec: _build_and_write_pair_chunk(
                        spec,
                        partition_dir=partition_dir,
                        target_cells=target_cells,
                        centroid_df=centroid_df,
                        allow_self_pairs=lookup_cfg.allow_self_pairs,
                        allow_active_role_self_pairs=lookup_cfg.allow_active_role_self_pairs,
                        projected_cell_geometries=projected_cell_geometries,
                        max_candidate_pairs_per_chunk=lookup_cfg.max_candidate_pairs_per_chunk,
                    ),
                    chunk_specs,
                )
            )
    else:
        chunk_results = [
            _build_and_write_pair_chunk(
                spec,
                partition_dir=partition_dir,
                target_cells=target_cells,
                centroid_df=centroid_df,
                allow_self_pairs=lookup_cfg.allow_self_pairs,
                allow_active_role_self_pairs=lookup_cfg.allow_active_role_self_pairs,
                projected_cell_geometries=projected_cell_geometries,
                max_candidate_pairs_per_chunk=lookup_cfg.max_candidate_pairs_per_chunk,
            )
            for spec in chunk_specs
        ]

    for chunk_index, out_path, kept_count, candidate_count, source_type, source_count in sorted(
        chunk_results, key=lambda row: row[0]
    ):
        total_candidate_pairs += candidate_count
        if out_path is None:
            LOGGER.info(
                "Skipped empty source-target lookup chunk source_type=%s source_count=%d "
                "candidate_pairs=%d kept_pairs=0",
                source_type,
                source_count,
                candidate_count,
            )
            continue
        total_pairs += kept_count
        partition_paths.append(out_path)
        LOGGER.info(
            "Wrote source-target lookup chunk %d source_type=%s source_count=%d "
            "candidate_pairs=%d kept_pairs=%d path=%s",
            chunk_index,
            source_type,
            source_count,
            candidate_count,
            kept_count,
            out_path,
        )

    if not partition_paths:
        raise ValueError(
            "Source-target lookup produced zero pairs. Likely causes: no source cells, "
            "no target cells, max distance too small, or bbox/filter settings too restrictive. "
            f"land_sources={len(land_source_cells)} water_sources={len(water_source_cells)} "
            f"targets={len(target_cells)} candidate_pairs={total_candidate_pairs}"
        )

    n_source_cells = len(set(land_source_cells).union(water_source_cells))
    n_target_cells = len(target_cells)

    if combine:
        lookup_path.parent.mkdir(parents=True, exist_ok=True)
        lf = pl.scan_parquet([str(path) for path in partition_paths]).select(
            list(SOURCE_TARGET_LOOKUP_SCHEMA)
        )
        total_pairs = atomic_sink_parquet(lf, lookup_path, overwrite=True)

        n_pairs, n_source_cells_from_file, n_target_cells_from_file = _validate_lookup_artifact(
            lookup_path, lookup_cfg
        )
        total_pairs = n_pairs
        n_source_cells = n_source_cells_from_file
        n_target_cells = n_target_cells_from_file

        write_metadata_sidecar(
            lookup_path,
            runtime.raw_config,
            _lookup_metadata_extra(
                runtime,
                cfg,
                lookup_cfg,
                n_pairs=total_pairs,
                n_source_cells=n_source_cells,
                n_land_source_cells=len(set(land_source_cells)),
                n_water_source_cells=len(set(water_source_cells)),
                n_target_cells=n_target_cells,
                grid_disk_k=max(land_grid_disk_k, water_grid_disk_k),
            ),
        )
        LOGGER.info(
            "Wrote source-target lookup total_pairs=%d path=%s",
            total_pairs,
            lookup_path,
        )

        if clean_intermediates:
            shutil.rmtree(partition_dir, ignore_errors=True)
            # This stage owns the canonical lookup. Preserve it while optionally
            # cleaning stage scratch.
            cleanup_data_contract(
                config_path,
                require=(),
                remove_stage_scratch=False,
                preserve_paths=(lookup_path,),
            )

    return AreaLookupResult(
        lookup_path=lookup_path,
        partition_dir=partition_dir,
        n_pairs=total_pairs,
        n_source_cells=n_source_cells,
        n_target_cells=n_target_cells,
        h3_resolution=runtime.source_resolution,
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
