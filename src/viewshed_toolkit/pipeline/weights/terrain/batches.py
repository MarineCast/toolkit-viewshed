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

import hashlib
import json
import logging
import math
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

import pandas as pd
import polars as pl
from rasterio.windows import from_bounds

from viewshed_toolkit._internal.geo.raster import write_array_like

from ...config import (
    AppConfig,
    BatchContext,
    current_process_memory_mb,
    metadata_sidecar_candidates,
    timer,
    viewshed_config_from_app_config,
)
from ...prepare.area.observers import build_observers_from_sample_points
from .. import distance
from .telemetry import append_terrain_performance

LOGGER = logging.getLogger(__name__)

try:
    import xarray as xr
except ImportError:
    xr = None

try:
    import rioxarray
except ImportError:
    rioxarray = None

from .accumulators import TiledAccumulator
from .gdal import (
    _add_partition_metadata_columns,
    _lookup_target_cells_for_source,
    _partition_path_for_source,
    _partition_row_count,
    _source_cumulative_paths,
    _source_type_for_app,
    _terrain_weight_partition_for_storage,
    _water_terrain_prefilter_for_source,
    _write_partition_metadata_sidecar,
    _write_water_open_shortcut_partition,
    expected_partition_metadata,
    partition_metadata_matches,
    validate_batch_raster_alignment,
)
from .los import (
    SourceViewshedResult,
    ViewshedWindowResult,
    _load_xrspatial_viewshed,
    load_xrspatial_analysis_dem_dataarray,
    run_viewshed_backend_to_bool_array,
)
from .summarize import (
    DISTANCE_SUM_SCALE,
    DISTANCE_WEIGHT_SUM_SCALE,
    cumulative_visible_arrays_to_h3,
    domain_target_water_area_by_h3,
)


def _planned_source_accumulator_window(
    context: BatchContext,
    observer_records: Sequence[dict[str, Any]],
    projected_records: Sequence[dict[str, Any]],
    *,
    use_full_grid: bool,
) -> tuple[int, int, tuple[int, int]]:
    """Return a conservative parent-grid window containing every observer radius."""

    if use_full_grid:
        return 0, 0, tuple(int(value) for value in context.water_mask_arr.shape)
    radii = [float(row["max_distance_m"]) for row in observer_records]
    record_radii = tuple(zip(projected_records, radii, strict=True))
    left = min(float(row["geometry"].x) - radius for row, radius in record_radii)
    right = max(float(row["geometry"].x) + radius for row, radius in record_radii)
    bottom = min(float(row["geometry"].y) - radius for row, radius in record_radii)
    top = max(float(row["geometry"].y) + radius for row, radius in record_radii)
    planned = from_bounds(left, bottom, right, top, transform=context.water_transform)
    padding_pixels = 2
    row_offset = max(0, int(math.floor(planned.row_off)) - padding_pixels)
    col_offset = max(0, int(math.floor(planned.col_off)) - padding_pixels)
    row_end = min(
        context.water_mask_arr.shape[0],
        int(math.ceil(planned.row_off + planned.height)) + padding_pixels,
    )
    col_end = min(
        context.water_mask_arr.shape[1],
        int(math.ceil(planned.col_off + planned.width)) + padding_pixels,
    )
    return (
        row_offset,
        col_offset,
        (
            max(0, row_end - row_offset),
            max(0, col_end - col_offset),
        ),
    )


@dataclass(frozen=True)
class PreparedSourceExecution:
    """Immutable per-source setup shared by matched terrain surfaces."""

    source_cell: str
    source_samples: Any
    sample_points_actual: int
    sample_points_requested: int
    observers: Any
    observer_jobs: tuple[tuple[dict[str, Any], dict[str, Any]], ...]
    use_full_grid: bool
    array_row_offset: int
    array_col_offset: int
    array_row_end: int
    array_col_end: int
    accumulator_shape: tuple[int, int]
    accumulator_pixel_count: int
    window_fingerprint: str
    distance_weight_config: Any
    distance_weight_max_km: float
    target_water_area: pl.DataFrame


def prepare_source_execution(
    app: AppConfig,
    context: BatchContext,
    source_cell: str,
) -> PreparedSourceExecution:
    """Resolve observer, window, distance, and aggregation setup once per source."""

    config = viewshed_config_from_app_config(app)
    source_samples = context.all_sample_points[
        context.all_sample_points["source_h3_cell"] == source_cell
    ].copy()
    if source_samples.empty:
        raise ValueError(f"No sample points found for source cell: {source_cell}")
    sample_points_actual = int(len(source_samples))
    if "sample_points_actual" in source_samples.columns:
        recorded_actual = pd.to_numeric(
            source_samples["sample_points_actual"], errors="raise"
        ).drop_duplicates()
        if len(recorded_actual) != 1 or int(recorded_actual.iloc[0]) != sample_points_actual:
            raise ValueError(
                "Prepared source sampling metadata does not match the number of unique "
                f"observer rows for {source_cell}: recorded={recorded_actual.tolist()} "
                f"actual={sample_points_actual}"
            )
    sample_points_requested = (
        int(pd.to_numeric(source_samples["sample_points_requested"], errors="raise").iloc[0])
        if "sample_points_requested" in source_samples.columns
        else sample_points_actual
    )
    observers = build_observers_from_sample_points(source_samples, config)
    observers_projected = observers.to_crs(config.crs_projected)
    observer_records = tuple(observers.to_dict("records"))
    projected_records = tuple(observers_projected.to_dict("records"))
    observer_jobs = tuple(zip(observer_records, projected_records, strict=True))
    use_full_grid = bool(app.run.write_cumulative_rasters) or app.viewshed.backend == "xrspatial"
    array_row_offset, array_col_offset, accumulator_shape = _planned_source_accumulator_window(
        context,
        observer_records,
        projected_records,
        use_full_grid=use_full_grid,
    )
    array_row_end = array_row_offset + int(accumulator_shape[0])
    array_col_end = array_col_offset + int(accumulator_shape[1])
    accumulator_pixel_count = int(accumulator_shape[0] * accumulator_shape[1])
    if accumulator_pixel_count <= 0:
        raise ValueError(
            "Prepared source observer window is empty: "
            f"source_h3_cell={source_cell} row_start={array_row_offset} "
            f"row_end={array_row_end} col_start={array_col_offset} "
            f"col_end={array_col_end}"
        )
    window_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source_h3_cell": str(source_cell),
                "observer_centers_and_radii": [
                    [
                        round(float(projected["geometry"].x), 6),
                        round(float(projected["geometry"].y), 6),
                        round(float(observer["max_distance_m"]), 6),
                    ]
                    for observer, projected in observer_jobs
                ],
                "raster_transform": [float(value) for value in context.water_transform],
                "raster_shape": [int(value) for value in context.water_mask_arr.shape],
                "use_full_grid": use_full_grid,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    distance_weight_config = distance.load_distance_weight_config(app.raw_config)
    distance_weight_max_km = (
        float(distance_weight_config.hard_cutoff_km)
        if distance_weight_config.hard_cutoff_km is not None
        else float(app.viewshed.max_distance_m) / 1_000.0
    )
    return PreparedSourceExecution(
        source_cell=str(source_cell),
        source_samples=source_samples,
        sample_points_actual=sample_points_actual,
        sample_points_requested=sample_points_requested,
        observers=observers,
        observer_jobs=observer_jobs,
        use_full_grid=use_full_grid,
        array_row_offset=array_row_offset,
        array_col_offset=array_col_offset,
        array_row_end=array_row_end,
        array_col_end=array_col_end,
        accumulator_shape=accumulator_shape,
        accumulator_pixel_count=accumulator_pixel_count,
        window_fingerprint=window_fingerprint,
        distance_weight_config=distance_weight_config,
        distance_weight_max_km=distance_weight_max_km,
        target_water_area=domain_target_water_area_by_h3(app, app.h3.output_resolution),
    )


def _bounded_as_completed(
    executor: Executor,
    jobs: Iterator[Any],
    submit_job: Callable[[Any], Future],
    inflight_limit: Callable[[], int],
) -> Iterator[Any]:
    """Submit a bounded job stream and yield results in completion order."""

    pending: set[Future] = set()
    exhausted = False
    while pending or not exhausted:
        while not exhausted and len(pending) < max(1, int(inflight_limit())):
            try:
                job = next(jobs)
            except StopIteration:
                exhausted = True
                break
            pending.add(submit_job(job))
        if pending:
            future = next(as_completed(pending))
            pending.remove(future)
            yield future.result()


def run_source_cell_in_batch(
    app: AppConfig,
    context: BatchContext,
    source_cell: str,
    max_workers: int | None = None,
    retain_result: bool = False,
    observer_executor: Executor | None = None,
    observer_inflight_limit: int | Callable[[], int] | None = None,
    prepared_execution: PreparedSourceExecution | None = None,
    validate_rasters: bool = True,
) -> SourceViewshedResult:
    """Run terrain visibility for one source cell and write its compact partition.

    Production batch runs should leave ``retain_result=False`` so the full
    per-source H3 dataframe can be garbage-collected after the compact partition
    is written. Single-cell/map/debug workflows can set ``retain_result=True``.
    When ``observer_executor`` is supplied, every observer job is submitted to
    that caller-owned pool instead of creating a source-local worker pool.
    """

    config = viewshed_config_from_app_config(app)
    if validate_rasters:
        validate_batch_raster_alignment(context)
    partition_path = _partition_path_for_source(app, source_cell)
    expected_metadata = expected_partition_metadata(app)

    prepared = prepared_execution or prepare_source_execution(app, context, source_cell)
    if prepared.source_cell != str(source_cell):
        raise ValueError(
            "Prepared source execution does not match requested source cell: "
            f"prepared={prepared.source_cell} requested={source_cell}"
        )
    source_samples = prepared.source_samples
    sample_points_actual = prepared.sample_points_actual
    sample_points_requested = prepared.sample_points_requested
    observers = prepared.observers

    if app.run.skip_existing_partitions and not app.run.overwrite and partition_path.exists():
        if not partition_metadata_matches(partition_path, expected_metadata):
            LOGGER.warning(
                "Existing terrain partition is stale and will be rebuilt: %s",
                partition_path,
            )
            partition_path.unlink(missing_ok=True)
            for sidecar in metadata_sidecar_candidates(partition_path):
                sidecar.unlink(missing_ok=True)
        else:
            row_count = _partition_row_count(partition_path)
            combined = pd.read_parquet(partition_path) if retain_result else None
            return SourceViewshedResult(
                source_h3_cell=source_cell,
                partition_path=partition_path,
                n_rows=int(row_count),
                n_visible_target_cells=int(row_count),
                open_water_shortcut_pairs=0,
                dem_viewshed_pairs=int(row_count),
                combined_h3=combined,
                combined_observers=observers if retain_result else None,
            )

    open_water_rows = pd.DataFrame()
    dem_target_override: set[str] | None = None
    if _source_type_for_app(app) == "water":
        open_water_rows, dem_target_override = _water_terrain_prefilter_for_source(
            app,
            source_cell,
            source_points_wgs84=list(source_samples.geometry),
            target_distances_override=(
                context.lookup_distances_by_source.get(str(source_cell), {})
                if context.lookup_distances_by_source is not None
                else None
            ),
        )
        if not open_water_rows.empty:
            open_water_rows = open_water_rows.copy()
            open_water_rows["sample_points_requested"] = sample_points_requested
            open_water_rows["sample_points_actual"] = sample_points_actual
            open_water_rows["n_observers"] = sample_points_actual
        if dem_target_override is not None:
            LOGGER.info(
                "water_terrain_prefilter source_h3_cell=%s open_water_shortcut_pairs=%d dem_fallback_targets=%d",
                source_cell,
                len(open_water_rows),
                len(dem_target_override),
            )
        if dem_target_override is not None and not dem_target_override:
            _write_water_open_shortcut_partition(app, source_cell, open_water_rows)
            combined = (
                _add_partition_metadata_columns(open_water_rows.copy(), expected_metadata)
                if retain_result
                else None
            )
            return SourceViewshedResult(
                source_h3_cell=source_cell,
                partition_path=partition_path,
                n_rows=int(len(open_water_rows)),
                n_visible_target_cells=(
                    int(open_water_rows["target_h3_cell"].nunique())
                    if "target_h3_cell" in open_water_rows.columns
                    else int(len(open_water_rows))
                ),
                open_water_shortcut_pairs=int(len(open_water_rows)),
                dem_viewshed_pairs=0,
                combined_h3=combined,
                combined_observers=observers if retain_result else None,
            )

    xrspatial_dem_da = None
    if app.viewshed.backend == "xrspatial":
        if xr is None or rioxarray is None:
            raise ImportError(
                "viewshed.backend='xrspatial' requires xarray-spatial, xarray, and rioxarray. "
                "Install with: pip install xarray-spatial xarray rioxarray"
            )
        _load_xrspatial_viewshed()
        xrspatial_dem_da = (
            context.xrspatial_dem_da
            if context.xrspatial_dem_da is not None
            else load_xrspatial_analysis_dem_dataarray(context.analysis_dem_path)
        )

    def run_one_observer(
        observer_row: dict[str, Any], observer_projected_row: dict[str, Any]
    ) -> tuple[int, float, float, ViewshedWindowResult]:
        observer_id = str(observer_row["observer_id"]).replace("/", "_")
        sample_index = int(observer_row["sample_index"])
        geom = observer_projected_row["geometry"]
        observer_x = float(geom.x)
        observer_y = float(geom.y)

        viewshed_result = run_viewshed_backend_to_bool_array(
            backend=app.viewshed.backend,
            context=context,
            xrspatial_dem_da=xrspatial_dem_da,
            observer_x=observer_x,
            observer_y=observer_y,
            observer_height_m=float(observer_row["observer_height_m"]),
            target_height_m=float(observer_row["target_height_m"]),
            max_distance_m=float(observer_row["max_distance_m"]),
            curvature_coefficient=config.curvature_coefficient,
            earth_radius_m=config.earth_radius_m,
            app=app,
            source_cell=source_cell,
            sample_index=sample_index,
            observer_id=observer_id,
        )
        return sample_index, observer_x, observer_y, viewshed_result

    observer_jobs = prepared.observer_jobs
    worker_budget = app.batch.max_workers if max_workers is None else max_workers
    if app.viewshed.backend == "xrspatial":
        if observer_executor is not None:
            raise ValueError("A shared observer executor is only supported by the GDAL backend.")
        worker_count = 1
    else:
        worker_count = max(1, min(int(worker_budget), len(observer_jobs)))
    LOGGER.info(
        "viewshed_backend_profile backend=%s worker_count=%d curvature_coefficient=%s "
        "observer_eye_height_m=%s target_height_m=%s max_distance_m=%s "
        "terrain_surface_model=%s analysis_surface=%s executor_scope=%s",
        app.viewshed.backend,
        worker_count,
        config.curvature_coefficient,
        config.observer_eye_height_m,
        config.target_height_m,
        config.max_distance_m,
        config.surface_model,
        context.analysis_dem_path,
        "batch_shared" if observer_executor is not None else "source_local",
    )

    # Production runs accumulate only into the union of the GDAL viewshed windows
    # for this source. Compute that conservative window before launching workers
    # so completed observer arrays can be accumulated and released immediately.
    # When cumulative rasters are requested (or xrspatial returns a parent-sized
    # result), keep the full grid so outputs remain georeferenced like the mask.
    array_row_offset = prepared.array_row_offset
    array_col_offset = prepared.array_col_offset
    shape_ = prepared.accumulator_shape

    tile_size = int(getattr(app.batch, "accumulator_tile_size", 512))
    accumulator = TiledAccumulator(
        shape_,
        row_offset=array_row_offset,
        col_offset=array_col_offset,
        n_observers=len(observers),
        tile_size=tile_size,
    )
    distance_weight_config = prepared.distance_weight_config
    distance_weight_max_km = prepared.distance_weight_max_km

    def requested_inflight_limit() -> int:
        raw_limit = (
            observer_inflight_limit()
            if callable(observer_inflight_limit)
            else observer_inflight_limit
        )
        return max(1, int(worker_count if raw_limit is None else raw_limit))

    def completed_results(
        executor: Executor,
    ) -> Iterator[tuple[int, float, float, ViewshedWindowResult]]:
        yield from _bounded_as_completed(
            executor,
            iter(observer_jobs),
            lambda job: executor.submit(run_one_observer, job[0], job[1]),
            requested_inflight_limit,
        )

    backend_times: list[float] = []
    postprocess_elapsed_seconds = 0.0

    def consume_results(
        results: Iterator[tuple[int, float, float, ViewshedWindowResult]],
    ) -> None:
        nonlocal postprocess_elapsed_seconds
        for sample_index, observer_x, observer_y, viewshed_result in results:
            backend_times.append(float(viewshed_result.elapsed_seconds))
            row_start = int(viewshed_result.y_start)
            col_start = int(viewshed_result.x_start)
            row_end = row_start + viewshed_result.visible.shape[0]
            col_end = col_start + viewshed_result.visible.shape[1]
            if (
                row_start < array_row_offset
                or col_start < array_col_offset
                or row_end > array_row_offset + shape_[0]
                or col_end > array_col_offset + shape_[1]
            ):
                raise ValueError(
                    "Observer viewshed exceeded its conservative source accumulator window: "
                    f"source_h3_cell={source_cell} sample_index={sample_index} "
                    f"observer_window={(row_start, col_start, row_end, col_end)} "
                    f"accumulator_window={(array_row_offset, array_col_offset, shape_)}"
                )
            visible = (
                viewshed_result.visible
                & context.water_mask_arr[row_start:row_end, col_start:col_end]
            )
            postprocess_start = time.perf_counter()
            accumulator.accumulate(
                visible=visible,
                row_start=row_start,
                col_start=col_start,
                transform=context.water_transform,
                observer_x=observer_x,
                observer_y=observer_y,
                sample_index=sample_index,
                distance_weight_config=distance_weight_config,
                distance_weight_max_km=distance_weight_max_km,
            )
            postprocess_elapsed_seconds += time.perf_counter() - postprocess_start

    if observer_executor is not None:
        consume_results(completed_results(observer_executor))
    elif worker_count == 1:
        consume_results(
            iter(
                run_one_observer(observer_row, observer_projected_row)
                for observer_row, observer_projected_row in observer_jobs
            )
        )
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            consume_results(completed_results(executor))

    postprocess_timer = {"elapsed_seconds": postprocess_elapsed_seconds}

    count_path = min_path = mean_path = max_path = None
    if app.run.write_cumulative_rasters:
        count_path, min_path, mean_path, max_path = _source_cumulative_paths(app, source_cell)
        for field, path, dtype in (
            ("visible_count", count_path, "uint16"),
            ("min_distance", min_path, "float32"),
            ("mean_distance", mean_path, "float32"),
            ("max_distance", max_path, "float32"),
        ):
            materialized = accumulator.materialize(field)
            write_array_like(
                context.water_mask_path,
                path,
                materialized,
                dtype,
                nodata=0,
                overwrite=True,
                compress=app.raster.final_compress,
                block_size=app.raster.block_size,
            )
            del materialized

    include_target_geometry = bool(
        app.run.write_pair_geometry or app.run.write_geojson or app.run.write_maps
    )
    allowed_targets = (
        dem_target_override
        if dem_target_override is not None
        else (
            set(context.lookup_targets_by_source.get(str(source_cell), frozenset()))
            if context.lookup_targets_by_source is not None
            else _lookup_target_cells_for_source(app, source_cell)
        )
    )
    target_water_area = prepared.target_water_area
    stride = 1 if app.h3.aggregation_mode == "full" else max(1, int(app.h3.pixel_stride))
    sparse = accumulator.sparse_arrays(stride=stride)
    with timer(f"clear_sky_h3_aggregation:{source_cell}") as h3_timer:
        combined_h3 = cumulative_visible_arrays_to_h3(
            visible_count=sparse.visible_count,
            distance_weight_sum=sparse.distance_weight_sum,
            min_distance=sparse.min_distance,
            mean_distance=sparse.mean_distance,
            max_distance=sparse.max_distance,
            observer_mask=sparse.observer_mask,
            reference_raster_path=context.water_mask_path,
            h3_resolution=app.h3.output_resolution,
            target_water_area=target_water_area,
            pixel_stride=app.h3.pixel_stride,
            aggregation_mode=app.h3.aggregation_mode,
            min_visible_sampled_pixel_count=app.h3.min_visible_sampled_pixel_count,
            min_visible_area_km2=app.h3.min_visible_area_km2,
            include_geometry=include_target_geometry,
            n_observer_points=len(observers),
            sample_points_requested=sample_points_requested,
            sample_points_actual=sample_points_actual,
            allowed_target_h3=allowed_targets,
            sparse_global_rows=sparse.rows,
            sparse_global_cols=sparse.cols,
            pixel_h3_index=context.water_pixel_h3_index,
            reference_shape=context.water_shape,
            reference_crs=context.water_crs,
            pixel_area_m2=context.water_pixel_area_m2,
            distance_weight_sum_scale=DISTANCE_WEIGHT_SUM_SCALE,
        )

    if _source_type_for_app(app) == "water" and not open_water_rows.empty:
        combined_h3 = pd.concat(
            [combined_h3, open_water_rows],
            ignore_index=True,
            sort=False,
        )

    if not combined_h3.empty:
        combined_h3["source_h3_cell"] = source_cell

    partition_path.parent.mkdir(parents=True, exist_ok=True)
    combined_h3 = _add_partition_metadata_columns(combined_h3, expected_metadata)
    partition_df = _terrain_weight_partition_for_storage(combined_h3)
    with timer(f"clear_sky_partition_write:{source_cell}") as write_timer:
        partition_df.to_parquet(partition_path, index=False)
        partition_sidecar_metadata = {
            **expected_metadata,
            **(context.surface_metadata or {}),
            "observer_result_reduction": "as_completed_fixed_point",
            "distance_sum_scale_per_metre": DISTANCE_SUM_SCALE,
            "distance_weight_sum_scale": DISTANCE_WEIGHT_SUM_SCALE,
        }
        _write_partition_metadata_sidecar(partition_path, partition_sidecar_metadata)

    if app.run.write_geojson and "geometry" in combined_h3.columns:
        combined_h3.to_file(partition_path.with_suffix(".geojson"), driver="GeoJSON")

    n_rows = int(len(partition_df))
    n_visible_target_cells = (
        int(partition_df["target_h3"].nunique()) if not partition_df.empty else 0
    )
    open_water_shortcut_pairs = int(len(open_water_rows))
    dem_viewshed_pairs = max(0, n_rows - open_water_shortcut_pairs)
    LOGGER.info(
        "clear_sky_source_runtime batch_id=%s source_h3_cell=%s n_observers=%d "
        "backend_elapsed_seconds_sum=%.3f backend_elapsed_seconds_max=%.3f "
        "postprocess_elapsed_seconds=%.3f h3_aggregation_elapsed_seconds=%.3f "
        "partition_write_elapsed_seconds=%.3f n_visible_pixels=%d n_target_cells=%d "
        "raster_width=%d raster_height=%d accumulator_tiles=%d "
        "accumulator_allocated_pixels=%d accumulator_memory_mb=%.2f "
        "process_memory_mb=%s partition_path=%s",
        context.batch_id,
        source_cell,
        len(observers),
        float(sum(backend_times)),
        float(max(backend_times) if backend_times else 0.0),
        float(postprocess_timer["elapsed_seconds"] or 0.0),
        float(h3_timer["elapsed_seconds"] or 0.0),
        float(write_timer["elapsed_seconds"] or 0.0),
        accumulator.visible_pixel_count,
        n_visible_target_cells,
        int(shape_[1]) if len(shape_) == 2 else 0,
        int(shape_[0]) if len(shape_) == 2 else 0,
        len(accumulator.tiles),
        accumulator.allocated_pixel_count,
        accumulator.memory_bytes / (1024.0 * 1024.0),
        current_process_memory_mb(),
        partition_path,
    )
    append_terrain_performance(
        app,
        "source_complete",
        batch_id=context.batch_id,
        source_h3_cell=source_cell,
        n_observers=len(observers),
        backend_elapsed_seconds_sum=float(sum(backend_times)),
        backend_elapsed_seconds_max=float(max(backend_times) if backend_times else 0.0),
        postprocess_elapsed_seconds=float(postprocess_elapsed_seconds),
        h3_aggregation_elapsed_seconds=float(h3_timer["elapsed_seconds"] or 0.0),
        partition_write_elapsed_seconds=float(write_timer["elapsed_seconds"] or 0.0),
        visible_pixel_count=accumulator.visible_pixel_count,
        accumulator_tile_size=accumulator.tile_size,
        accumulator_tile_count=len(accumulator.tiles),
        accumulator_allocated_pixel_count=accumulator.allocated_pixel_count,
        accumulator_memory_mb=accumulator.memory_bytes / (1024.0 * 1024.0),
        target_cell_count=n_visible_target_cells,
        accumulator_width=int(shape_[1]) if len(shape_) == 2 else 0,
        accumulator_height=int(shape_[0]) if len(shape_) == 2 else 0,
        observer_result_reduction="as_completed_fixed_point",
        distance_sum_scale_per_metre=DISTANCE_SUM_SCALE,
        distance_weight_sum_scale=DISTANCE_WEIGHT_SUM_SCALE,
        partition_path=str(partition_path),
    )

    return SourceViewshedResult(
        source_h3_cell=source_cell,
        partition_path=partition_path,
        n_rows=n_rows,
        n_visible_target_cells=n_visible_target_cells,
        open_water_shortcut_pairs=open_water_shortcut_pairs,
        dem_viewshed_pairs=dem_viewshed_pairs,
        cumulative_visible_count_raster=count_path,
        cumulative_min_distance_raster=min_path,
        cumulative_mean_distance_raster=mean_path,
        cumulative_max_distance_raster=max_path,
        combined_h3=combined_h3 if retain_result else None,
        combined_observers=observers if retain_result else None,
    )


# -----------------------------------------------------------------------------
# Multi-source orchestration
# -----------------------------------------------------------------------------
