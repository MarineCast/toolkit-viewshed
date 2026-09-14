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
import math
import threading
from concurrent.futures import Executor, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

import numpy as np
import pandas as pd

from ...config import (
    AppConfig,
    BatchContext,
    current_process_memory_mb,
    metadata_sidecar_candidates,
    timer,
)
from ...prepare.area.batching import (
    iter_source_batches,
    make_batch_id,
)
from ...prepare.area.context import prepare_batch_context
from ...prepare.area.inputs import validate_viewshed_inputs
from ...prepare.area.sampling import (
    h3_cell_for_latlon,
    prepare_source_samples,
    source_cell_polygon,
    write_source_sampling_diagnostics,
)
from .gdal import load_batch_lookup
from .pixel_index import canonical_pixel_h3_window
from .telemetry import append_terrain_performance, write_manifest, write_source_window_plan

LOGGER = logging.getLogger(__name__)
_SourceTaskT = TypeVar("_SourceTaskT")
_SourceResultT = TypeVar("_SourceResultT")


from .batches import PreparedSourceExecution, prepare_source_execution, run_source_cell_in_batch
from .cleanup import cleanup_batch_context, cleanup_batches_dir, combine_partitions
from .gdal import (
    _area_lookup_path_for_app,
    _load_terrain_source_cells,
    _lookup_source_cells,
    _partition_path_for_source,
    _partition_row_count,
    _source_type_for_app,
    _water_terrain_prefilter_for_source,
    _write_water_open_shortcut_partition,
    expected_partition_metadata,
    partition_metadata_matches,
    validate_batch_raster_alignment,
)
from .los import (
    SourceViewshedResult,
    load_xrspatial_analysis_dem_dataarray,
    xrspatial_source_worker_count,
)
from .summarize import domain_target_water_area_by_h3


class _FallbackProgress:
    def __init__(self, total: int, desc: str, unit: str) -> None:
        self.total = max(0, int(total))
        self.desc = desc
        self.unit = unit
        self.n = 0
        self.postfix = ""
        self._closed = False
        self._render()

    def set_postfix_str(self, value: str, refresh: bool = False) -> None:
        self.postfix = value
        if refresh:
            self._render()

    def update(self, n: int = 1) -> None:
        self.n = min(self.total, self.n + int(n))
        self._render()

    def close(self) -> None:
        if not self._closed:
            print()
            self._closed = True

    def _render(self) -> None:
        width = 40
        if self.total:
            filled = int(width * self.n / self.total)
            pct = int(100 * self.n / self.total)
        else:
            filled = width
            pct = 100
        bar = "█" * filled + " " * (width - filled)
        suffix = f", {self.postfix}" if self.postfix else ""
        print(
            f"\r{self.desc}: {pct:3d}%|{bar}| {self.n}/{self.total} {self.unit}{suffix}",
            end="",
            flush=True,
        )


class _ObserverInFlightController:
    """Dynamically bound retained observer results while keeping the pool fed."""

    def __init__(
        self,
        *,
        total_workers: int,
        source_workers: int,
        remaining_sources: int,
    ) -> None:
        self.total_workers = max(1, int(total_workers))
        self.source_workers = max(1, int(source_workers))
        self.remaining_sources = max(0, int(remaining_sources))
        self._lock = threading.Lock()

    def limit(self) -> int:
        with self._lock:
            active = max(
                1,
                min(self.source_workers, self.remaining_sources),
            )
            return max(1, int(math.ceil(self.total_workers / active)) + 1)

    def source_completed(self) -> None:
        with self._lock:
            self.remaining_sources = max(0, self.remaining_sources - 1)


def _estimated_runtime_peak_memory_mb(
    app: AppConfig,
    context: BatchContext,
    *,
    source_workers: int,
    observer_workers: int,
    source_window_pixel_counts: Sequence[int] | None = None,
) -> float:
    """Conservative resident-memory estimate for one active terrain batch."""

    total_pixels = int(context.water_mask_arr.size)
    if source_window_pixel_counts:
        window_pixels = sorted(
            (max(1, int(value)) for value in source_window_pixel_counts),
            reverse=True,
        )
    else:
        radius_pixels = int(math.ceil(app.viewshed.max_distance_m / app.viewshed.dem_resolution_m))
        window_pixels = [min(total_pixels, int((2 * radius_pixels + 5) ** 2))]
    source_window_pixels = window_pixels[0]
    # Base batch rasters/indexes, six source accumulators (34 bytes/pixel),
    # and the globally bounded observer-result queue (bool arrays).
    base_bytes = total_pixels * 36
    active_window_pixels = sum(
        window_pixels[index % len(window_pixels)] for index in range(max(1, int(source_workers)))
    )
    accumulator_bytes = active_window_pixels * 34
    observer_count = max(1, int(observer_workers))
    coordinator_count = max(1, int(source_workers))
    maximum_inflight_results = max(
        active * (int(math.ceil(observer_count / active)) + 1)
        for active in range(1, coordinator_count + 1)
    )
    result_queue_bytes = source_window_pixels * maximum_inflight_results
    return float(base_bytes + accumulator_bytes + result_queue_bytes) / (1024.0 * 1024.0)


def _memory_bounded_source_worker_count(
    app: AppConfig,
    context: BatchContext,
    *,
    requested_workers: int,
    pending_source_count: int,
    observer_workers: int,
    source_window_pixel_counts: Sequence[int] | None = None,
) -> tuple[int, float]:
    """Choose the largest source coordinator count inside the memory guardrail."""

    requested = max(1, min(int(requested_workers), int(pending_source_count)))
    maximum_mb = app.batch.max_estimated_batch_memory_mb
    if maximum_mb is None:
        estimate = _estimated_runtime_peak_memory_mb(
            app,
            context,
            source_workers=requested,
            observer_workers=observer_workers,
            source_window_pixel_counts=source_window_pixel_counts,
        )
        return requested, estimate
    for workers in range(requested, 0, -1):
        estimate = _estimated_runtime_peak_memory_mb(
            app,
            context,
            source_workers=workers,
            observer_workers=observer_workers,
            source_window_pixel_counts=source_window_pixel_counts,
        )
        if estimate <= float(maximum_mb):
            return workers, estimate
    minimum_estimate = _estimated_runtime_peak_memory_mb(
        app,
        context,
        source_workers=1,
        observer_workers=observer_workers,
        source_window_pixel_counts=source_window_pixel_counts,
    )
    raise ValueError(
        "Terrain runtime memory estimate exceeds the configured guardrail even "
        "with one active source coordinator: "
        f"estimated_memory_mb={minimum_estimate:.1f} "
        f"max_estimated_batch_memory_mb={float(maximum_mb):.1f} "
        f"batch_id={context.batch_id}. Reduce batch radius/resolution or raise the "
        "guardrail after validating available memory."
    )


def _schedule_source_tasks_by_window_cost(
    pending_cells: Sequence[tuple[int, str]],
    prepared_executions: dict[str, PreparedSourceExecution],
) -> list[tuple[int, str]]:
    """Start costly sources first so the coordinator pool drains its long tail."""

    return sorted(
        pending_cells,
        key=lambda item: (
            -prepared_executions[str(item[1])].accumulator_pixel_count,
            int(item[0]),
            str(item[1]),
        ),
    )


def _source_window_plan_rows(
    context: BatchContext,
    prepared_executions: dict[str, PreparedSourceExecution],
) -> list[dict[str, Any]]:
    """Serialize deterministic source observer windows for planning and audit."""

    return [
        {
            "batch_id": str(context.batch_id),
            "source_h3_cell": source_cell,
            "row_start": prepared.array_row_offset,
            "row_end": prepared.array_row_end,
            "col_start": prepared.array_col_offset,
            "col_end": prepared.array_col_end,
            "height": prepared.accumulator_shape[0],
            "width": prepared.accumulator_shape[1],
            "pixel_count": prepared.accumulator_pixel_count,
            "window_fingerprint": prepared.window_fingerprint,
            "observer_count": prepared.sample_points_actual,
            "maximum_distance_m": max(
                float(observer["max_distance_m"]) for observer, _projected in prepared.observer_jobs
            ),
            "uses_full_grid": prepared.use_full_grid,
        }
        for source_cell, prepared in sorted(prepared_executions.items())
    ]


_CANOPY_ONLY_SURFACE_METADATA_FIELDS = frozenset(
    {
        "intervening_obstacle_surface",
        "observer_grounded_pixel_count",
        "canopy_land_pixel_count",
        "canopy_masked_land_pixel_count",
        "canopy_masked_land_fraction",
        "positive_canopy_pixel_count",
        "maximum_canopy_height_m",
    }
)


def _bare_earth_context_from_canopy(context: BatchContext) -> BatchContext:
    """Return the endpoint-DTM view of one already-prepared canopy batch."""

    metadata = {
        key: value
        for key, value in dict(context.surface_metadata or {}).items()
        if key not in _CANOPY_ONLY_SURFACE_METADATA_FIELDS
    }
    metadata["terrain_surface_model"] = "bare_earth"
    metadata["dem_nodata_los_pixel_count"] = metadata.get("endpoint_dem_nodata_los_pixel_count", 0)
    return replace(
        context,
        analysis_dem_path=context.endpoint_dem_path,
        aligned_canopy_height_path=None,
        surface_metadata=metadata,
        xrspatial_dem_da=None,
    )


def _paired_surface_shared_contract(app: AppConfig) -> dict[str, Any]:
    viewshed = {key: value for key, value in vars(app.viewshed).items() if key != "surface_model"}
    return {
        "source_type": str(app.source_type),
        "output_dir": str(Path(app.paths.output_dir).resolve()),
        "water_polygon_path": str(Path(app.paths.water_polygon_path).resolve()),
        "regional_dem_path": str(Path(app.paths.regional_dem_path).resolve()),
        "canopy_height_path": str(Path(app.paths.canopy_height_path).resolve()),
        "projected_dem_path": str(Path(app.paths.projected_dem_path).resolve()),
        "source_cells_path": str(Path(app.paths.source_cells_path).resolve()),
        "land_h3_path": str(Path(app.paths.land_h3_path).resolve()),
        "viewshed": viewshed,
        "h3": dict(vars(app.h3)),
        "batch": dict(vars(app.batch)),
    }


def _validate_paired_surface_apps(
    bare_earth_app: AppConfig,
    canopy_app: AppConfig,
) -> None:
    if str(bare_earth_app.viewshed.surface_model).strip().lower() != "bare_earth":
        raise ValueError("bare_earth_app must use viewshed.surface_model='bare_earth'.")
    if str(canopy_app.viewshed.surface_model).strip().lower() != "canopy":
        raise ValueError("canopy_app must use viewshed.surface_model='canopy'.")
    if _source_type_for_app(bare_earth_app) != "land" or _source_type_for_app(canopy_app) != "land":
        raise ValueError("Paired bare-earth/canopy execution requires land sources.")
    if bare_earth_app.viewshed.backend != "gdal" or canopy_app.viewshed.backend != "gdal":
        raise ValueError("Paired bare-earth/canopy execution currently requires GDAL.")

    bare_contract = _paired_surface_shared_contract(bare_earth_app)
    canopy_contract = _paired_surface_shared_contract(canopy_app)
    mismatches = [key for key in bare_contract if bare_contract[key] != canopy_contract[key]]
    if mismatches:
        raise ValueError(
            "Paired terrain apps must share all source, raster, H3, and batch settings. "
            f"Mismatched contract field(s): {mismatches}"
        )


def _coordinate_sources_with_observer_executor(
    pending_cells: Sequence[_SourceTaskT],
    *,
    total_workers: int,
    observer_executor: Executor,
    run_one_source: Callable[[_SourceTaskT, Executor], _SourceResultT],
    source_thread_prefix: str = "viewshed-source",
) -> list[_SourceResultT]:
    """Coordinate sources while one executor bounds all observer work."""

    source_workers = max(1, min(len(pending_cells), int(total_workers)))
    if source_workers == 1:
        return [run_one_source(item, observer_executor) for item in pending_cells]

    completed_rows: list[_SourceResultT] = []
    with ThreadPoolExecutor(
        max_workers=source_workers,
        thread_name_prefix=source_thread_prefix,
    ) as source_executor:
        futures = [
            source_executor.submit(run_one_source, item, observer_executor)
            for item in pending_cells
        ]
        for future in as_completed(futures):
            completed_rows.append(future.result())
    return completed_rows


def _interleave_paired_surface_tasks(
    pending_by_surface: dict[str, list[tuple[int, str]]],
    source_order: Sequence[str],
) -> list[tuple[str, tuple[int, str]]]:
    """Return deterministic source-surface tasks interleaved by source cell.

    Interleaving prevents the source coordinator pool from filling entirely with
    one surface when its worker count is smaller than the number of pending bare
    sources. Each source still owns its observer futures and consumes them in
    sample order, so cross-surface scheduling cannot change accumulation order.
    """

    tasks_by_surface: dict[str, dict[str, tuple[int, str]]] = {}
    for surface in ("bare_earth", "canopy"):
        surface_tasks: dict[str, tuple[int, str]] = {}
        for item in pending_by_surface.get(surface, []):
            source_cell = str(item[1])
            if source_cell in surface_tasks:
                raise ValueError(
                    "Duplicate paired terrain source task: "
                    f"surface={surface} source_h3_cell={source_cell}"
                )
            surface_tasks[source_cell] = item
        tasks_by_surface[surface] = surface_tasks

    mixed: list[tuple[str, tuple[int, str]]] = []
    for source_cell in (str(value) for value in source_order):
        for surface in ("bare_earth", "canopy"):
            item = tasks_by_surface[surface].pop(source_cell, None)
            if item is not None:
                mixed.append((surface, item))

    leftovers = {surface: sorted(tasks) for surface, tasks in tasks_by_surface.items() if tasks}
    if leftovers:
        raise ValueError(
            "Paired terrain tasks contain source cells outside the batch order: " f"{leftovers}"
        )
    return mixed


def _attach_batch_runtime_indexes(
    app: AppConfig,
    context: BatchContext,
    source_cells: Sequence[str],
    *,
    lookup_targets: dict[str, frozenset[str]] | None = None,
    lookup_distances: dict[str, dict[str, float]] | None = None,
) -> BatchContext:
    """Attach bounded indexes shared by every source worker in one batch."""

    runtime_index_started = __import__("time").perf_counter()
    if lookup_targets is None:
        targets, distances = load_batch_lookup(
            app,
            source_cells,
            include_distances=_source_type_for_app(app) == "water",
        )
    else:
        requested = {str(cell) for cell in source_cells}
        targets = {
            source: values for source, values in lookup_targets.items() if source in requested
        }
        distances = (
            {source: values for source, values in lookup_distances.items() if source in requested}
            if lookup_distances is not None
            else None
        )
    if context.canonical_water_mask_path is None:
        raise ValueError("Batch context is missing its canonical water-mask path.")
    pixel_h3_index = canonical_pixel_h3_window(
        app,
        context.canonical_water_mask_path,
        batch_transform=context.water_transform,
        batch_crs=context.water_crs,
        batch_water_mask=context.water_mask_arr,
    )
    runtime_index_elapsed = __import__("time").perf_counter() - runtime_index_started
    surface_metadata = dict(context.surface_metadata or {})
    surface_metadata["batch_runtime_index_elapsed_seconds"] = float(runtime_index_elapsed)
    surface_metadata["batch_preparation_elapsed_seconds"] = float(
        surface_metadata.get("batch_preparation_elapsed_seconds", 0.0)
    ) + float(runtime_index_elapsed)
    return replace(
        context,
        water_pixel_h3_index=pixel_h3_index,
        lookup_targets_by_source=targets,
        lookup_distances_by_source=distances,
        surface_metadata=surface_metadata,
    )


def run_source_cells(
    app: AppConfig,
    limit: int | None = None,
    start: int = 0,
) -> pd.DataFrame:
    validate_viewshed_inputs(app)
    domain_target_water_area_by_h3(app, app.h3.output_resolution)
    source_cells_gdf = _load_terrain_source_cells(app)
    logger = __import__("logging").getLogger(__name__)
    lookup_sources = _lookup_source_cells(app)
    if lookup_sources is not None:
        before_count = len(source_cells_gdf)
        source_cells_gdf = source_cells_gdf[
            source_cells_gdf["h3_cell"].astype(str).isin(lookup_sources)
        ].copy()
        logger.info(
            "Filtered terrain source cells by area lookup before=%d after=%d lookup=%s",
            before_count,
            len(source_cells_gdf),
            _area_lookup_path_for_app(app),
        )
    logger.info(
        "Batch strategy: %s; Batch size cells: %s",
        app.batch.strategy,
        app.batch.batch_size_cells,
    )
    if app.batch.strategy == "sequential":
        logger.warning(
            "Sequential batching can create very large AOIs if source cells are not spatially sorted. "
            "For regional runs, prefer batch.strategy=h3_parent."
        )

    if start:
        source_cells_gdf = source_cells_gdf.iloc[start:].copy()
    if limit is not None:
        source_cells_gdf = source_cells_gdf.head(limit).copy()

    source_cells = [str(cell) for cell in source_cells_gdf["h3_cell"].tolist()]
    source_sample_points, source_sampling_diagnostics = prepare_source_samples(
        app,
        source_cells_gdf,
        source_cells,
    )
    sampling_diagnostics_path, sampling_summary = write_source_sampling_diagnostics(
        app,
        source_sampling_diagnostics,
    )
    sampling_by_source = {
        str(row["source_h3"]): row for row in source_sampling_diagnostics.to_dict("records")
    }
    source_points_by_cell: dict[str, list[Any]] = {}
    for row in source_sample_points.itertuples(index=False):
        source_points_by_cell.setdefault(str(row.source_h3_cell), []).append(row.geometry)

    def sampling_manifest_fields(source_cell: str) -> dict[str, Any]:
        row = sampling_by_source[str(source_cell)]
        return {
            "source_type": str(row["source_type"]),
            "active_source_fraction": float(row["active_source_fraction"]),
            "source_sampling_mode": str(row["source_sampling_mode"]),
            "sample_points_max": int(row["sample_points_max"]),
            "sample_points_min": int(row["sample_points_min"]),
            "sample_points_requested": int(row["sample_points_requested"]),
            "sample_points_actual": int(row["sample_points_actual"]),
        }

    logger.info(
        "source_sampling_run sampling_mode=%s max_samples_per_cell=%d "
        "min_samples_per_cell=%d diagnostics=%s summary=%s",
        app.h3.source_sampling_mode,
        int(app.h3.sample_points_per_source_cell),
        int(app.h3.min_sample_points_per_source_cell),
        sampling_diagnostics_path,
        sampling_summary,
    )
    batches = iter_source_batches(
        source_cells_gdf,
        app,
        source_sample_points_gdf=source_sample_points,
    )
    manifest_rows: list[dict[str, Any]] = []
    total_source_cells = len(source_cells)
    completed_source_cells = 0
    stale_partition_count = 0
    stale_notice_logged = False

    try:
        from tqdm.auto import tqdm
    except ImportError:
        tqdm = None

    progress = (
        tqdm(total=total_source_cells, desc="Viewshed source cells", unit="cell")
        if tqdm is not None
        else _FallbackProgress(
            total=total_source_cells,
            desc="Viewshed source cells",
            unit="cell",
        )
    )

    def advance_progress(source_cell: str, status: str) -> None:
        nonlocal completed_source_cells
        completed_source_cells += 1
        progress.set_postfix_str(f"{status}: {source_cell}", refresh=False)
        progress.update(1)

    try:
        for batch_index, batch_cells in enumerate(batches, start=1):
            batch_id = make_batch_id(app, batch_index, batch_cells)
            water_land_mask_start = __import__("time").perf_counter()
            pending_cells: list[tuple[int, str]] = []
            batch_lookup_targets: dict[str, frozenset[str]] | None = None
            batch_lookup_distances: dict[str, dict[str, float]] | None = None
            if _source_type_for_app(app) == "water":
                batch_lookup_targets, batch_lookup_distances = load_batch_lookup(
                    app,
                    batch_cells,
                    include_distances=True,
                )

                water_cells: list[str] = []
                for source_cell in batch_cells:
                    partition_path = _partition_path_for_source(app, source_cell)
                    if (
                        app.run.skip_existing_partitions
                        and not app.run.overwrite
                        and partition_path.exists()
                    ):
                        if partition_metadata_matches(
                            partition_path,
                            expected_partition_metadata(app),
                        ):
                            manifest_rows.append(
                                {
                                    "source_h3_cell": source_cell,
                                    **sampling_manifest_fields(source_cell),
                                    "batch_id": batch_id,
                                    "status": "skipped_existing",
                                    "n_rows": _partition_row_count(partition_path),
                                    "partition_path": str(partition_path),
                                }
                            )
                            advance_progress(source_cell, "skipped_existing")
                            continue
                        stale_partition_count += 1
                        if not stale_notice_logged:
                            logger.info(
                                "Stale terrain partitions were found and will be rebuilt; "
                                "individual paths are available at DEBUG level."
                            )
                            stale_notice_logged = True
                        partition_path.unlink(missing_ok=True)
                        for sidecar in metadata_sidecar_candidates(partition_path):
                            sidecar.unlink(missing_ok=True)
                    water_cells.append(source_cell)

                def build_water_land_mask_partition(source_cell: str) -> dict[str, Any]:
                    rows, dem_targets = _water_terrain_prefilter_for_source(
                        app,
                        source_cell,
                        source_points_wgs84=source_points_by_cell[source_cell],
                        target_distances_override=(batch_lookup_distances or {}).get(
                            source_cell,
                            {},
                        ),
                    )
                    if dem_targets:
                        raise AssertionError(
                            "Opaque-land water model must never request DEM fallback: "
                            f"source={source_cell} targets={len(dem_targets)}"
                        )
                    sampling_fields = sampling_manifest_fields(source_cell)
                    rows = rows.assign(
                        sample_points_requested=sampling_fields["sample_points_requested"],
                        sample_points_actual=sampling_fields["sample_points_actual"],
                        n_observers=sampling_fields["sample_points_actual"],
                    )
                    partition_path = _write_water_open_shortcut_partition(
                        app,
                        source_cell,
                        rows,
                    )
                    return {
                        "source_h3_cell": source_cell,
                        **sampling_fields,
                        "batch_id": batch_id,
                        "status": "water_land_mask",
                        "n_rows": len(rows),
                        "water_land_mask_pairs": len(rows),
                        "partition_path": str(partition_path),
                    }

                completed_water_rows: list[dict[str, Any]] = []
                if water_cells:
                    # Neighboring sources reuse the same target sample and H3
                    # bound caches. Serial evaluation is faster than threading
                    # here because it avoids duplicate GEOS/cache construction.
                    completed_water_rows.extend(
                        build_water_land_mask_partition(source_cell) for source_cell in water_cells
                    )
                for row in completed_water_rows:
                    manifest_rows.append(row)
                    advance_progress(row["source_h3_cell"], row["status"])

                elapsed = __import__("time").perf_counter() - water_land_mask_start
                append_terrain_performance(
                    app,
                    "water_land_mask_batch_complete",
                    batch_id=batch_id,
                    completed_source_count=len(completed_water_rows),
                    skipped_source_count=len(batch_cells) - len(completed_water_rows),
                    source_workers=1 if water_cells else 0,
                    elapsed_seconds=elapsed,
                    cells_per_second=(
                        len(completed_water_rows) / elapsed
                        if elapsed > 0 and completed_water_rows
                        else 0.0
                    ),
                    batch_lookup_pair_count=sum(
                        len(values) for values in (batch_lookup_targets or {}).values()
                    ),
                )
                if manifest_rows:
                    write_manifest(manifest_rows, app.paths.manifest_path)
                continue

            for idx, source_cell in enumerate(batch_cells, start=1):
                partition_path = _partition_path_for_source(app, source_cell)
                if (
                    app.run.skip_existing_partitions
                    and not app.run.overwrite
                    and partition_path.exists()
                ):
                    if not partition_metadata_matches(
                        partition_path, expected_partition_metadata(app)
                    ):
                        stale_partition_count += 1
                        logger.debug(
                            "Existing terrain partition is stale and will be rebuilt: %s",
                            partition_path,
                        )
                        if not stale_notice_logged:
                            logger.info(
                                "Stale terrain partitions were found and will be rebuilt; "
                                "individual paths are available at DEBUG level."
                            )
                            stale_notice_logged = True
                        partition_path.unlink(missing_ok=True)
                        for sidecar in metadata_sidecar_candidates(partition_path):
                            sidecar.unlink(missing_ok=True)
                    else:
                        n_rows = _partition_row_count(partition_path)
                        manifest_rows.append(
                            {
                                "source_h3_cell": source_cell,
                                **sampling_manifest_fields(source_cell),
                                "batch_id": batch_id,
                                "status": "skipped_existing",
                                "n_rows": n_rows,
                                "partition_path": str(partition_path),
                            }
                        )
                        advance_progress(source_cell, "skipped_existing")
                        continue

                pending_cells.append((idx, source_cell))

            if not pending_cells:
                if manifest_rows:
                    write_manifest(manifest_rows, app.paths.manifest_path)
                continue

            pending_source_cells = [source_cell for _, source_cell in pending_cells]
            context = prepare_batch_context(
                app,
                pending_source_cells,
                batch_index=batch_index,
                source_cells_gdf=source_cells_gdf,
                source_sample_points_gdf=source_sample_points,
            )
            context = _attach_batch_runtime_indexes(
                app,
                context,
                pending_source_cells,
                lookup_targets=batch_lookup_targets,
                lookup_distances=batch_lookup_distances,
            )
            if app.viewshed.backend == "xrspatial":
                context = replace(
                    context,
                    xrspatial_dem_da=load_xrspatial_analysis_dem_dataarray(
                        context.analysis_dem_path
                    ),
                )
            try:
                validate_batch_raster_alignment(context)
                prepared_executions = {
                    source_cell: prepare_source_execution(app, context, source_cell)
                    for source_cell in pending_source_cells
                }
                pending_cells = _schedule_source_tasks_by_window_cost(
                    pending_cells,
                    prepared_executions,
                )
                source_window_pixel_counts = [
                    prepared_executions[source_cell].accumulator_pixel_count
                    for _index, source_cell in pending_cells
                ]
                source_window_plan_path = write_source_window_plan(
                    app,
                    context.batch_id,
                    _source_window_plan_rows(context, prepared_executions),
                )
                total_workers = max(1, int(app.batch.max_workers))
                if app.viewshed.backend == "xrspatial":
                    source_workers = xrspatial_source_worker_count(
                        requested_workers=total_workers,
                        pending_cell_count=len(pending_cells),
                        max_distance_m=app.viewshed.max_distance_m,
                        dem_resolution_m=app.viewshed.dem_resolution_m,
                    )
                    observer_workers = 1
                    estimated_peak_memory_mb = _estimated_runtime_peak_memory_mb(
                        app,
                        context,
                        source_workers=source_workers,
                        observer_workers=observer_workers,
                        source_window_pixel_counts=source_window_pixel_counts,
                    )
                    inflight_controller = None
                else:
                    # Source workers only coordinate source-level aggregation. All
                    # GDAL observer jobs share one bounded batch-wide executor so
                    # an eight-worker run can immediately start observers from the
                    # next source instead of idling after an 8+2 split.
                    observer_workers = total_workers
                    source_workers, estimated_peak_memory_mb = _memory_bounded_source_worker_count(
                        app,
                        context,
                        requested_workers=total_workers,
                        pending_source_count=len(pending_cells),
                        observer_workers=observer_workers,
                        source_window_pixel_counts=source_window_pixel_counts,
                    )
                    inflight_controller = _ObserverInFlightController(
                        total_workers=observer_workers,
                        source_workers=source_workers,
                        remaining_sources=len(pending_cells),
                    )
                logger.info(
                    "clear_sky_batch_runtime_start batch_id=%s n_batch_cells=%d "
                    "n_pending_cells=%d n_skipped_cells=%d source_workers=%d "
                    "observer_workers=%d max_workers=%d estimated_peak_memory_mb=%.1f "
                    "scheduler=%s",
                    context.batch_id,
                    len(batch_cells),
                    len(pending_cells),
                    len(batch_cells) - len(pending_cells),
                    source_workers,
                    observer_workers,
                    total_workers,
                    estimated_peak_memory_mb,
                    (
                        "xrspatial_source_pool"
                        if app.viewshed.backend == "xrspatial"
                        else "batch_shared_observer_queue"
                    ),
                )
                logger.info(
                    "source_window_plan batch_id=%s sources=%d maximum_pixels=%d path=%s",
                    context.batch_id,
                    len(source_window_pixel_counts),
                    max(source_window_pixel_counts),
                    source_window_plan_path,
                )
                batch_run_start = __import__("time").perf_counter()

                def run_one_source(
                    item: tuple[int, str],
                    observer_executor: Executor | None = None,
                ) -> dict[str, Any]:
                    _, source_cell = item
                    try:
                        with timer(f"source:{source_cell}") as source_timer:
                            result = run_source_cell_in_batch(
                                app,
                                context,
                                source_cell,
                                max_workers=observer_workers,
                                observer_executor=observer_executor,
                                observer_inflight_limit=(
                                    inflight_controller.limit
                                    if inflight_controller is not None
                                    else None
                                ),
                                prepared_execution=prepared_executions[source_cell],
                                validate_rasters=False,
                            )
                    finally:
                        if inflight_controller is not None:
                            inflight_controller.source_completed()
                    n_visible = int(result.n_visible_target_cells)
                    open_water_shortcut_pairs = int(result.open_water_shortcut_pairs)
                    dem_viewshed_pairs = int(result.dem_viewshed_pairs)
                    __import__("logging").getLogger(__name__).info(
                        "source_profile batch_id=%s source_h3_cell=%s "
                        "elapsed_seconds=%.2f n_visible_target_cells=%d "
                        "open_water_shortcut_pairs=%d dem_viewshed_pairs=%d "
                        "process_memory_mb=%s",
                        context.batch_id,
                        source_cell,
                        float(source_timer["elapsed_seconds"] or 0.0),
                        n_visible,
                        open_water_shortcut_pairs,
                        dem_viewshed_pairs,
                        current_process_memory_mb(),
                    )
                    return {
                        "source_h3_cell": source_cell,
                        **sampling_manifest_fields(source_cell),
                        "batch_id": context.batch_id,
                        "status": "ok",
                        "n_rows": int(result.n_rows),
                        "elapsed_seconds": float(source_timer["elapsed_seconds"] or 0.0),
                        "open_water_shortcut_pairs": open_water_shortcut_pairs,
                        "dem_viewshed_pairs": dem_viewshed_pairs,
                        "partition_path": (
                            str(result.partition_path) if result.partition_path else ""
                        ),
                    }

                if app.viewshed.backend == "xrspatial":
                    if source_workers == 1:
                        for item in pending_cells:
                            row = run_one_source(item)
                            manifest_rows.append(row)
                            advance_progress(row["source_h3_cell"], row["status"])
                    else:
                        with ThreadPoolExecutor(max_workers=source_workers) as executor:
                            futures = [
                                executor.submit(run_one_source, item) for item in pending_cells
                            ]
                            for future in as_completed(futures):
                                row = future.result()
                                manifest_rows.append(row)
                                advance_progress(row["source_h3_cell"], row["status"])
                else:
                    with ThreadPoolExecutor(
                        max_workers=observer_workers,
                        thread_name_prefix="viewshed-observer",
                    ) as observer_executor:
                        completed_rows = _coordinate_sources_with_observer_executor(
                            pending_cells,
                            total_workers=source_workers,
                            observer_executor=observer_executor,
                            run_one_source=run_one_source,
                        )
                    for row in completed_rows:
                        manifest_rows.append(row)
                        advance_progress(row["source_h3_cell"], row["status"])
                batch_elapsed = __import__("time").perf_counter() - batch_run_start
                logger.info(
                    "clear_sky_batch_runtime_done batch_id=%s n_completed_cells=%d "
                    "elapsed_seconds=%.3f cells_per_second=%.4f",
                    context.batch_id,
                    len(pending_cells),
                    batch_elapsed,
                    (len(pending_cells) / batch_elapsed) if batch_elapsed > 0 else 0.0,
                )
                append_terrain_performance(
                    app,
                    "batch_complete",
                    batch_id=context.batch_id,
                    completed_source_count=len(pending_cells),
                    source_workers=source_workers,
                    observer_workers=observer_workers,
                    estimated_peak_memory_mb=estimated_peak_memory_mb,
                    batch_preparation_elapsed_seconds=float(
                        (context.surface_metadata or {}).get(
                            "batch_preparation_elapsed_seconds", 0.0
                        )
                    ),
                    elapsed_seconds=batch_elapsed,
                    cells_per_second=(
                        len(pending_cells) / batch_elapsed if batch_elapsed > 0 else 0.0
                    ),
                    raster_height=int(context.water_mask_arr.shape[0]),
                    raster_width=int(context.water_mask_arr.shape[1]),
                    water_pixel_count=int(np.count_nonzero(context.water_mask_arr)),
                    pixel_h3_index_memory_mb=(
                        float(context.water_pixel_h3_index.memory_bytes) / (1024.0 * 1024.0)
                        if context.water_pixel_h3_index is not None
                        else 0.0
                    ),
                    batch_lookup_pair_count=sum(
                        len(values) for values in (context.lookup_targets_by_source or {}).values()
                    ),
                )
                write_manifest(manifest_rows, app.paths.manifest_path)
            finally:
                cleanup_batch_context(app, context)

        if manifest_rows:
            write_manifest(manifest_rows, app.paths.manifest_path)

        if stale_partition_count:
            logger.info(
                "Rebuilt stale terrain partitions count=%d source_type=%s",
                stale_partition_count,
                _source_type_for_app(app),
            )

        if app.run.combine_final_parquet:
            combine_partitions(app)

        return pd.DataFrame(manifest_rows)
    finally:
        progress.close()
        cleanup_batches_dir(app)


def run_paired_surface_source_cells(
    bare_earth_app: AppConfig,
    canopy_app: AppConfig,
    *,
    limit: int | None = None,
    start: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run matched bare-earth and canopy surfaces from one prepared batch.

    The canopy batch preparation already creates the endpoint DTM, water mask,
    aligned CHM, and DTM+CHM obstacle surface. This runner reuses its endpoint
    DTM for bare-earth work and interleaves bare/canopy source coordinators on
    one bounded GDAL observer executor. Independent partition and manifest
    contracts are unchanged.
    """

    _validate_paired_surface_apps(bare_earth_app, canopy_app)
    validate_viewshed_inputs(bare_earth_app)
    validate_viewshed_inputs(canopy_app)
    domain_target_water_area_by_h3(canopy_app, canopy_app.h3.output_resolution)

    logger = logging.getLogger(__name__)
    source_cells_gdf = _load_terrain_source_cells(canopy_app)
    bare_lookup_sources = _lookup_source_cells(bare_earth_app)
    canopy_lookup_sources = _lookup_source_cells(canopy_app)
    if bare_lookup_sources != canopy_lookup_sources:
        raise ValueError(
            "Paired terrain apps resolved different source universes from their lookup artifacts."
        )
    if canopy_lookup_sources is not None:
        before_count = len(source_cells_gdf)
        source_cells_gdf = source_cells_gdf[
            source_cells_gdf["h3_cell"].astype(str).isin(canopy_lookup_sources)
        ].copy()
        logger.info(
            "Filtered paired terrain source cells by area lookup before=%d after=%d lookup=%s",
            before_count,
            len(source_cells_gdf),
            _area_lookup_path_for_app(canopy_app),
        )

    if start:
        source_cells_gdf = source_cells_gdf.iloc[start:].copy()
    if limit is not None:
        source_cells_gdf = source_cells_gdf.head(limit).copy()

    source_cells = [str(cell) for cell in source_cells_gdf["h3_cell"].tolist()]
    source_sample_points, source_sampling_diagnostics = prepare_source_samples(
        canopy_app,
        source_cells_gdf,
        source_cells,
    )
    for surface_app in (bare_earth_app, canopy_app):
        write_source_sampling_diagnostics(surface_app, source_sampling_diagnostics)
    sampling_by_source = {
        str(row["source_h3"]): row for row in source_sampling_diagnostics.to_dict("records")
    }

    def sampling_manifest_fields(source_cell: str) -> dict[str, Any]:
        row = sampling_by_source[str(source_cell)]
        return {
            "source_type": str(row["source_type"]),
            "active_source_fraction": float(row["active_source_fraction"]),
            "source_sampling_mode": str(row["source_sampling_mode"]),
            "sample_points_max": int(row["sample_points_max"]),
            "sample_points_min": int(row["sample_points_min"]),
            "sample_points_requested": int(row["sample_points_requested"]),
            "sample_points_actual": int(row["sample_points_actual"]),
        }

    batches = iter_source_batches(
        source_cells_gdf,
        canopy_app,
        source_sample_points_gdf=source_sample_points,
    )
    surface_apps = {
        "bare_earth": bare_earth_app,
        "canopy": canopy_app,
    }
    manifest_rows: dict[str, list[dict[str, Any]]] = {
        "bare_earth": [],
        "canopy": [],
    }
    stale_partition_counts = {"bare_earth": 0, "canopy": 0}
    stale_notice_logged = False
    keep_batch_intermediates = bool(
        bare_earth_app.run.keep_batch_intermediates or canopy_app.run.keep_batch_intermediates
    )
    cleanup_app = replace(
        canopy_app,
        run=replace(
            canopy_app.run,
            keep_batch_intermediates=keep_batch_intermediates,
        ),
    )

    try:
        from tqdm.auto import tqdm
    except ImportError:
        tqdm = None
    total_units = len(source_cells) * len(surface_apps)
    progress = (
        tqdm(total=total_units, desc="Paired terrain source cells", unit="cell-surface")
        if tqdm is not None
        else _FallbackProgress(
            total=total_units,
            desc="Paired terrain source cells",
            unit="cell-surface",
        )
    )

    def advance_progress(surface: str, source_cell: str, status: str) -> None:
        progress.set_postfix_str(f"{status}: {surface}/{source_cell}", refresh=False)
        progress.update(1)

    try:
        for batch_index, batch_cells in enumerate(batches, start=1):
            pending_by_surface: dict[str, list[tuple[int, str]]] = {
                "bare_earth": [],
                "canopy": [],
            }
            pending_source_set: set[str] = set()

            for surface, surface_app in surface_apps.items():
                display_batch_id = make_batch_id(surface_app, batch_index, batch_cells)
                for idx, source_cell in enumerate(batch_cells, start=1):
                    partition_path = _partition_path_for_source(surface_app, source_cell)
                    if (
                        surface_app.run.skip_existing_partitions
                        and not surface_app.run.overwrite
                        and partition_path.exists()
                    ):
                        if partition_metadata_matches(
                            partition_path,
                            expected_partition_metadata(surface_app),
                        ):
                            manifest_rows[surface].append(
                                {
                                    "source_h3_cell": source_cell,
                                    **sampling_manifest_fields(source_cell),
                                    "batch_id": display_batch_id,
                                    "status": "skipped_existing",
                                    "n_rows": _partition_row_count(partition_path),
                                    "partition_path": str(partition_path),
                                }
                            )
                            advance_progress(surface, source_cell, "skipped_existing")
                            continue
                        stale_partition_counts[surface] += 1
                        logger.debug(
                            "Existing paired terrain partition is stale and will be rebuilt: %s",
                            partition_path,
                        )
                        if not stale_notice_logged:
                            logger.info(
                                "Stale paired terrain partitions were found and will be "
                                "rebuilt; individual paths are available at DEBUG level."
                            )
                            stale_notice_logged = True
                        partition_path.unlink(missing_ok=True)
                        for sidecar in metadata_sidecar_candidates(partition_path):
                            sidecar.unlink(missing_ok=True)

                    pending_by_surface[surface].append((idx, source_cell))
                    pending_source_set.add(str(source_cell))

            if not pending_source_set:
                for surface, surface_app in surface_apps.items():
                    if manifest_rows[surface]:
                        write_manifest(manifest_rows[surface], surface_app.paths.manifest_path)
                continue

            pending_source_cells = [
                str(cell) for cell in batch_cells if str(cell) in pending_source_set
            ]
            prepare_with_canopy = bool(pending_by_surface["canopy"])
            preparation_app = canopy_app if prepare_with_canopy else bare_earth_app
            context = prepare_batch_context(
                preparation_app,
                pending_source_cells,
                batch_index=batch_index,
                source_cells_gdf=source_cells_gdf,
                source_sample_points_gdf=source_sample_points,
            )
            context = _attach_batch_runtime_indexes(
                preparation_app,
                context,
                pending_source_cells,
            )
            contexts = {
                "bare_earth": (
                    _bare_earth_context_from_canopy(context) if prepare_with_canopy else context
                ),
                "canopy": context,
            }
            source_planning_started = __import__("time").perf_counter()
            prepared_executions = {
                source_cell: prepare_source_execution(
                    preparation_app,
                    context,
                    source_cell,
                )
                for source_cell in pending_source_cells
            }
            source_planning_elapsed = __import__("time").perf_counter() - source_planning_started

            try:
                for surface, surface_context in contexts.items():
                    if pending_by_surface[surface]:
                        validate_batch_raster_alignment(surface_context)
                source_window_rows = _source_window_plan_rows(context, prepared_executions)
                source_window_plan_paths = {
                    surface: write_source_window_plan(
                        surface_app,
                        context.batch_id,
                        source_window_rows,
                    )
                    for surface, surface_app in surface_apps.items()
                    if pending_by_surface[surface]
                }
                source_order_by_index = {
                    str(source_cell): index for index, source_cell in enumerate(batch_cells)
                }
                scheduled_source_cells = [
                    source_cell
                    for _index, source_cell in _schedule_source_tasks_by_window_cost(
                        [
                            (source_order_by_index[source_cell], source_cell)
                            for source_cell in pending_source_cells
                        ],
                        prepared_executions,
                    )
                ]
                total_workers = max(1, int(canopy_app.batch.max_workers))
                mixed_tasks = _interleave_paired_surface_tasks(
                    pending_by_surface,
                    scheduled_source_cells,
                )
                source_window_pixel_counts = [
                    prepared_executions[str(item[1])].accumulator_pixel_count
                    for _surface, item in mixed_tasks
                ]
                # A worker here owns one source accumulator for one surface.
                # Count bare and canopy tasks together so simultaneous surfaces
                # cannot exceed the batch memory guardrail.
                source_workers, estimated_peak_memory_mb = _memory_bounded_source_worker_count(
                    preparation_app,
                    context,
                    requested_workers=total_workers,
                    pending_source_count=len(mixed_tasks),
                    observer_workers=total_workers,
                    source_window_pixel_counts=source_window_pixel_counts,
                )
                inflight_controller = _ObserverInFlightController(
                    total_workers=total_workers,
                    source_workers=source_workers,
                    remaining_sources=len(mixed_tasks),
                )
                logger.info(
                    "paired_surface_batch_runtime_start batch_id=%s "
                    "n_pending_bare=%d n_pending_canopy=%d observer_workers=%d "
                    "source_workers=%d estimated_peak_memory_mb=%.1f "
                    "scheduler=mixed_surface_observer_queue",
                    context.batch_id,
                    len(pending_by_surface["bare_earth"]),
                    len(pending_by_surface["canopy"]),
                    total_workers,
                    source_workers,
                    estimated_peak_memory_mb,
                )
                logger.info(
                    "paired_source_window_plan batch_id=%s sources=%d tasks=%d "
                    "maximum_pixels=%d paths=%s",
                    context.batch_id,
                    len(prepared_executions),
                    len(mixed_tasks),
                    max(source_window_pixel_counts),
                    source_window_plan_paths,
                )
                paired_batch_start = __import__("time").perf_counter()
                with ThreadPoolExecutor(
                    max_workers=total_workers,
                    thread_name_prefix="viewshed-observer",
                ) as observer_executor:

                    def run_one_surface_source(
                        task: tuple[str, tuple[int, str]],
                        shared_observer_executor: Executor,
                    ) -> tuple[str, dict[str, Any]]:
                        surface, item = task
                        _, source_cell = item
                        surface_app = surface_apps[surface]
                        surface_context = contexts[surface]
                        try:
                            with timer(f"source:{surface}:{source_cell}") as source_timer:
                                result = run_source_cell_in_batch(
                                    surface_app,
                                    surface_context,
                                    source_cell,
                                    max_workers=total_workers,
                                    observer_executor=shared_observer_executor,
                                    observer_inflight_limit=inflight_controller.limit,
                                    prepared_execution=prepared_executions[source_cell],
                                    validate_rasters=False,
                                )
                        finally:
                            inflight_controller.source_completed()
                        return (
                            surface,
                            {
                                "source_h3_cell": source_cell,
                                **sampling_manifest_fields(source_cell),
                                "batch_id": surface_context.batch_id,
                                "status": "ok",
                                "n_rows": int(result.n_rows),
                                "elapsed_seconds": float(source_timer["elapsed_seconds"] or 0.0),
                                "open_water_shortcut_pairs": int(result.open_water_shortcut_pairs),
                                "dem_viewshed_pairs": int(result.dem_viewshed_pairs),
                                "partition_path": (
                                    str(result.partition_path) if result.partition_path else ""
                                ),
                            },
                        )

                    completed_surface_rows = _coordinate_sources_with_observer_executor(
                        mixed_tasks,
                        total_workers=source_workers,
                        observer_executor=observer_executor,
                        run_one_source=run_one_surface_source,
                        source_thread_prefix="viewshed-paired-source",
                    )

                    for surface, row in completed_surface_rows:
                        manifest_rows[surface].append(row)
                        advance_progress(surface, row["source_h3_cell"], row["status"])
                    for surface, surface_app in surface_apps.items():
                        if manifest_rows[surface]:
                            write_manifest(
                                manifest_rows[surface],
                                surface_app.paths.manifest_path,
                            )

                paired_elapsed = __import__("time").perf_counter() - paired_batch_start
                logger.info(
                    "paired_surface_batch_runtime_done batch_id=%s elapsed_seconds=%.3f",
                    context.batch_id,
                    paired_elapsed,
                )
                append_terrain_performance(
                    preparation_app,
                    "paired_batch_complete",
                    batch_id=context.batch_id,
                    pending_bare_source_count=len(pending_by_surface["bare_earth"]),
                    pending_canopy_source_count=len(pending_by_surface["canopy"]),
                    observer_workers=total_workers,
                    source_workers=source_workers,
                    estimated_peak_memory_mb=estimated_peak_memory_mb,
                    scheduler="mixed_surface_observer_queue",
                    batch_preparation_elapsed_seconds=float(
                        (context.surface_metadata or {}).get(
                            "batch_preparation_elapsed_seconds", 0.0
                        )
                    ),
                    source_planning_elapsed_seconds=float(source_planning_elapsed),
                    elapsed_seconds=paired_elapsed,
                    raster_height=int(context.water_mask_arr.shape[0]),
                    raster_width=int(context.water_mask_arr.shape[1]),
                    water_pixel_count=int(np.count_nonzero(context.water_mask_arr)),
                    pixel_h3_index_memory_mb=(
                        float(context.water_pixel_h3_index.memory_bytes) / (1024.0 * 1024.0)
                        if context.water_pixel_h3_index is not None
                        else 0.0
                    ),
                    batch_lookup_pair_count=sum(
                        len(values) for values in (context.lookup_targets_by_source or {}).values()
                    ),
                )
            finally:
                cleanup_batch_context(cleanup_app, context)

        for surface, surface_app in surface_apps.items():
            if manifest_rows[surface]:
                write_manifest(manifest_rows[surface], surface_app.paths.manifest_path)
        if any(stale_partition_counts.values()):
            logger.info(
                "Rebuilt stale paired terrain partitions bare_earth=%d canopy=%d",
                stale_partition_counts["bare_earth"],
                stale_partition_counts["canopy"],
            )
        if bare_earth_app.run.combine_final_parquet:
            combine_partitions(bare_earth_app)
        if canopy_app.run.combine_final_parquet:
            combine_partitions(canopy_app)
        return (
            pd.DataFrame(manifest_rows["bare_earth"]),
            pd.DataFrame(manifest_rows["canopy"]),
        )
    finally:
        progress.close()
        cleanup_batches_dir(cleanup_app)


def run_single_cell(
    app: AppConfig,
    source_cell: str,
) -> SourceViewshedResult:
    validate_viewshed_inputs(app)
    domain_target_water_area_by_h3(app, app.h3.output_resolution)
    context = prepare_batch_context(app, [source_cell], batch_index=1)
    context = _attach_batch_runtime_indexes(app, context, [source_cell])
    try:
        return run_source_cell_in_batch(app, context, source_cell, retain_result=True)
    finally:
        cleanup_batch_context(app, context)


def run_one_latlon(
    app: AppConfig,
    lat: float,
    lon: float,
) -> SourceViewshedResult:
    source_cell = h3_cell_for_latlon(lat, lon, app.h3.source_resolution)
    return run_single_cell(app, source_cell)


# -----------------------------------------------------------------------------
# Optional map helper
# -----------------------------------------------------------------------------


def _distance_color(distance_km: float, max_distance_km: float) -> str:
    if max_distance_km <= 0:
        return "#2c7bb6"
    ratio = min(1.0, max(0.0, distance_km / max_distance_km))
    if ratio < 0.2:
        return "#2c7bb6"
    if ratio < 0.4:
        return "#00a6ca"
    if ratio < 0.6:
        return "#ffff8c"
    if ratio < 0.8:
        return "#fdae61"
    return "#d7191c"


def save_source_cell_map(
    result: SourceViewshedResult, app: AppConfig, out_html: Path | None = None
) -> Path:
    import folium

    source_polygon = source_cell_polygon(result.source_h3_cell)
    out_html = out_html or (app.paths.map_dir / f"{result.source_h3_cell}_viewshed_map.html")
    out_html.parent.mkdir(parents=True, exist_ok=True)

    center = source_polygon.geometry.iloc[0].centroid
    m = folium.Map(location=[center.y, center.x], zoom_start=11, tiles="CartoDB positron")

    folium.GeoJson(
        source_polygon,
        name="Source H3 cell",
        style_function=lambda _: {"color": "#424242", "weight": 2, "fillOpacity": 0.02},
    ).add_to(m)

    if result.combined_h3 is None:
        raise ValueError(
            "Source result does not include combined_h3. Run with retain_result=True for mapping."
        )

    if not result.combined_h3.empty:
        max_distance_km = float(result.combined_h3["nearest_view_distance_km"].max())

        def combined_style(feature: dict) -> dict:
            distance_km = float(feature["properties"].get("nearest_view_distance_km", 0.0))
            color = _distance_color(distance_km, max_distance_km)
            return {
                "color": color,
                "fillColor": color,
                "weight": 0.4,
                "fillOpacity": 0.45,
            }

        folium.GeoJson(
            result.combined_h3,
            name="Visible H3 cells",
            style_function=combined_style,
            tooltip=folium.GeoJsonTooltip(
                fields=[
                    "target_h3_cell",
                    "nearest_view_distance_km",
                    "mean_view_distance_km",
                    "farthest_view_distance_km",
                    "visible_from_n_points",
                    "visible_area_km2_approx_sum",
                ],
                aliases=[
                    "Visible H3",
                    "Nearest km",
                    "Mean km",
                    "Farthest km",
                    "Visible sample points",
                    "Visible area approx km²",
                ],
            ),
        ).add_to(m)

    observer_layer = folium.FeatureGroup(name="Sample observer points", show=True)
    if result.combined_observers is None:
        raise ValueError(
            "Source result does not include observer points. Run with retain_result=True for mapping."
        )

    for point in result.combined_observers.itertuples():
        folium.CircleMarker(
            location=[float(point.lat), float(point.lon)],
            radius=5,
            color="#111111",
            fill=True,
            fill_color="#ffffff",
            fill_opacity=1.0,
            weight=2,
            tooltip=str(point.observer_id),
        ).add_to(observer_layer)

    observer_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(out_html)
    return out_html


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
