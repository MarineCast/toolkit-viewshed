from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
from affine import Affine
from shapely.geometry import Point

from viewshed_toolkit._internal.geo.h3 import cell_to_polygon
from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.config.loader import BatchContext
from viewshed_toolkit.pipeline.prepare.area import batching as area
from viewshed_toolkit.pipeline.prepare.area import validation as batch_validation
from viewshed_toolkit.pipeline.weights import canopy_visibility
from viewshed_toolkit.pipeline.weights.terrain import (
    batches,
    cleanup,
    gdal,
    runner,
    telemetry,
)


def test_batch_shared_observer_executor_bounds_all_sources() -> None:
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    executor_ids: set[int] = set()

    def observer_job() -> None:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1

    def run_one_source(item, observer_executor):
        executor_ids.add(id(observer_executor))
        futures = [observer_executor.submit(observer_job) for _ in range(4)]
        for future in futures:
            future.result()
        return {"source_h3_cell": item[1]}

    pending = [(1, "source-a"), (2, "source-b"), (3, "source-c")]
    with ThreadPoolExecutor(max_workers=2) as observer_executor:
        rows = runner._coordinate_sources_with_observer_executor(
            pending,
            total_workers=2,
            observer_executor=observer_executor,
            run_one_source=run_one_source,
        )

    assert {row["source_h3_cell"] for row in rows} == {
        "source-a",
        "source-b",
        "source-c",
    }
    assert executor_ids == {id(observer_executor)}
    assert maximum_active == 2


def test_bounded_observer_results_do_not_wait_for_first_submitted_job() -> None:
    jobs = iter([("slow", 0.05), ("fast", 0.001), ("later", 0.001)])

    def execute(job: tuple[str, float]) -> str:
        label, delay = job
        time.sleep(delay)
        return label

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            batches._bounded_as_completed(
                executor,
                jobs,
                lambda job: executor.submit(execute, job),
                lambda: 2,
            )
        )

    assert results[0] == "fast"
    assert set(results) == {"slow", "fast", "later"}


def _source_and_sample_frames(
    cells: list[str],
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    source = gpd.GeoDataFrame(
        {"h3_cell": cells},
        geometry=[cell_to_polygon(cell) for cell in cells],
        crs="EPSG:4326",
    )
    latlng = [h3.cell_to_latlng(cell) for cell in cells]
    samples = gpd.GeoDataFrame(
        {
            "source_h3_cell": cells,
            "sample_index": [1] * len(cells),
        },
        geometry=[Point(lon, lat) for lat, lon in latlng],
        crs="EPSG:4326",
    )
    return source, samples


def test_h3_parent_batches_pack_nearby_remainders_deterministically() -> None:
    center_parent = h3.latlng_to_cell(48.1181, -123.4307, 7)
    parents = sorted(h3.grid_disk(center_parent, 1))[:5]
    counts = [7, 4, 3, 5, 2]
    cells = [
        child
        for parent, count in zip(parents, counts, strict=True)
        for child in sorted(h3.cell_to_children(parent, 8))[:count]
    ]
    source, samples = _source_and_sample_frames(cells)
    base = load_app_config("configs/salish_sea.yaml")
    app = replace(
        base,
        h3=replace(base.h3, source_resolution=8),
        batch=replace(
            base.batch,
            strategy="h3_parent",
            batch_size_cells=7,
            max_batch_aoi_pixels=20_000_000,
            max_estimated_batch_memory_mb=768.0,
        ),
        viewshed=replace(
            base.viewshed,
            max_distance_m=32_000,
            aoi_margin_m=0,
            dem_resolution_m=30,
            crs_projected="EPSG:32610",
        ),
    )

    packed = area.iter_source_batches(
        source,
        app,
        source_sample_points_gdf=samples,
    )
    shuffled = area.iter_source_batches(
        source.sample(frac=1.0, random_state=7),
        app,
        source_sample_points_gdf=samples.sample(frac=1.0, random_state=11),
    )

    assert packed == shuffled
    assert len(packed) == 3
    assert sorted(len(batch) for batch in packed) == [7, 7, 7]
    assert sorted(cell for batch in packed for cell in batch) == sorted(cells)
    full_parent_cells = set(sorted(h3.cell_to_children(parents[0], 8))[:7])
    assert any(set(batch) == full_parent_cells for batch in packed)


def test_h3_parent_packing_enforces_pixel_and_memory_guardrails() -> None:
    center_parent = h3.latlng_to_cell(48.1181, -123.4307, 7)
    distant_parent = sorted(h3.grid_ring(center_parent, 15))[0]
    cells = [
        *sorted(h3.cell_to_children(center_parent, 8))[:3],
        *sorted(h3.cell_to_children(distant_parent, 8))[:3],
    ]
    source, samples = _source_and_sample_frames(cells)
    base = load_app_config("configs/salish_sea.yaml")
    base = replace(
        base,
        h3=replace(base.h3, source_resolution=8),
        viewshed=replace(
            base.viewshed,
            max_distance_m=1_000,
            aoi_margin_m=0,
            dem_resolution_m=30,
            crs_projected="EPSG:32610",
        ),
    )
    pixel_limited = replace(
        base,
        batch=replace(
            base.batch,
            strategy="h3_parent",
            batch_size_cells=7,
            max_batch_aoi_pixels=100_000,
            max_estimated_batch_memory_mb=None,
        ),
    )
    memory_limited = replace(
        base,
        batch=replace(
            base.batch,
            strategy="h3_parent",
            batch_size_cells=7,
            max_batch_aoi_pixels=None,
            max_estimated_batch_memory_mb=3.0,
        ),
    )

    for app in (pixel_limited, memory_limited):
        batches_out = area.iter_source_batches(
            source,
            app,
            source_sample_points_gdf=samples,
        )
        assert len(batches_out) == 2
        assert sorted(len(batch) for batch in batches_out) == [3, 3]


def test_h3_parent_packing_splits_an_oversized_parent_group() -> None:
    parent = h3.latlng_to_cell(48.1181, -123.4307, 3)
    cells = sorted(h3.cell_to_children(parent, 4))[:5]
    source, samples = _source_and_sample_frames(cells)
    base = load_app_config("configs/salish_sea.yaml")
    app = replace(
        base,
        h3=replace(base.h3, source_resolution=4),
        batch=replace(
            base.batch,
            strategy="h3_parent",
            batch_size_cells=7,
            max_batch_aoi_pixels=10_000_000,
            max_estimated_batch_memory_mb=None,
        ),
        viewshed=replace(
            base.viewshed,
            max_distance_m=30_000,
            aoi_margin_m=1_000,
            dem_resolution_m=30,
            crs_projected="EPSG:32610",
        ),
    )

    batches_out = area.iter_source_batches(
        source,
        app,
        source_sample_points_gdf=samples,
    )
    observer_bounds = batch_validation._source_observer_bounds(
        source,
        samples,
        projected_crs="EPSG:32610",
    )

    assert len(batches_out) > 1
    assert sorted(cell for batch in batches_out for cell in batch) == sorted(cells)
    assert all(
        batch_validation._packing_group_pixel_count(
            batch_validation._packing_group(batch, observer_bounds),
            padding_m=31_000,
            resolution_m=30,
        )
        <= 10_000_000
        for batch in batches_out
    )


def test_paired_surface_tasks_are_interleaved_by_source() -> None:
    tasks = runner._interleave_paired_surface_tasks(
        {
            "bare_earth": [(1, "source-a"), (2, "source-b")],
            "canopy": [(1, "source-a"), (2, "source-b")],
        },
        ["source-a", "source-b"],
    )

    assert tasks == [
        ("bare_earth", (1, "source-a")),
        ("canopy", (1, "source-a")),
        ("bare_earth", (2, "source-b")),
        ("canopy", (2, "source-b")),
    ]


def test_batch_lookup_loads_only_requested_sources(tmp_path: Path) -> None:
    app = load_app_config("configs/salish_sea.yaml")
    app = replace(app, paths=replace(app.paths, output_dir=tmp_path), source_type="land")
    lookup_dir = tmp_path / "lookup"
    lookup_dir.mkdir(parents=True)
    lookup_path = lookup_dir / f"SOURCE_TARGET_LOOKUP_H3R{int(app.h3.source_resolution)}.parquet"
    pd.DataFrame(
        {
            "source_h3": ["source-a", "source-a", "source-b"],
            "target_h3": ["target-1", "target-2", "target-3"],
            "distance_km": [1.0, 2.0, 3.0],
            "source_type": ["land", "land", "land"],
        }
    ).to_parquet(lookup_path, index=False)

    targets, distances = gdal.load_batch_lookup(
        app,
        ["source-a"],
        include_distances=True,
    )

    assert targets == {"source-a": frozenset({"target-1", "target-2"})}
    assert distances == {"source-a": {"target-1": 1.0, "target-2": 2.0}}


def test_observer_inflight_limit_expands_for_the_batch_tail() -> None:
    controller = runner._ObserverInFlightController(
        total_workers=8,
        source_workers=7,
        remaining_sources=7,
    )
    assert controller.limit() == 3
    for _ in range(6):
        controller.source_completed()
    assert controller.limit() == 9


def test_planned_accumulator_window_contains_observer_radius() -> None:
    context = SimpleNamespace(
        water_mask_arr=np.zeros((100, 100), dtype=bool),
        water_transform=(Affine.translation(0.0, 1_000.0) * Affine.scale(10.0, -10.0)),
    )
    row_offset, col_offset, shape = batches._planned_source_accumulator_window(
        context,
        [{"max_distance_m": 200.0}],
        [{"geometry": Point(500.0, 500.0)}],
        use_full_grid=False,
    )

    assert (row_offset, col_offset) == (28, 28)
    assert shape == (44, 44)


def test_source_tasks_are_scheduled_by_descending_window_cost() -> None:
    pending = [(0, "small"), (1, "large"), (2, "medium")]
    prepared = {
        "small": SimpleNamespace(accumulator_pixel_count=100),
        "large": SimpleNamespace(accumulator_pixel_count=900),
        "medium": SimpleNamespace(accumulator_pixel_count=400),
    }

    scheduled = runner._schedule_source_tasks_by_window_cost(pending, prepared)

    assert [source for _index, source in scheduled] == ["large", "medium", "small"]


def test_source_window_plan_is_persisted_per_batch(tmp_path: Path) -> None:
    app = load_app_config("configs/salish_sea.yaml")
    app = replace(
        app,
        paths=replace(app.paths, manifest_path=tmp_path / "terrain.csv"),
    )
    rows = [
        {
            "source_h3_cell": "source-a",
            "row_start": 10,
            "row_end": 30,
            "col_start": 20,
            "col_end": 50,
            "pixel_count": 600,
            "window_fingerprint": "abc123",
        }
    ]

    path = telemetry.write_source_window_plan(app, "batch/001", rows)

    assert path == tmp_path / "terrain.source_windows" / "batch_001.parquet"
    persisted = pd.read_parquet(path)
    assert persisted.to_dict("records") == rows


def test_source_coordinators_are_reduced_to_fit_memory_guardrail() -> None:
    app = load_app_config("configs/salish_sea.yaml")
    app = replace(
        app,
        viewshed=replace(app.viewshed, max_distance_m=30_000, dem_resolution_m=30),
        batch=replace(app.batch, max_estimated_batch_memory_mb=400.0),
    )
    context = SimpleNamespace(
        batch_id="memory-test",
        water_mask_arr=np.zeros((2001, 2001), dtype=bool),
    )

    workers, estimate = runner._memory_bounded_source_worker_count(
        app,
        context,
        requested_workers=8,
        pending_source_count=7,
        observer_workers=8,
    )

    assert workers == 1
    assert estimate <= 400.0


def test_performance_telemetry_is_append_only(tmp_path: Path) -> None:
    app = load_app_config("configs/salish_sea.yaml")
    app = replace(
        app,
        paths=replace(app.paths, manifest_path=tmp_path / "terrain.csv"),
    )

    path = telemetry.append_terrain_performance(app, "source_complete", sequence=1)
    telemetry.append_terrain_performance(app, "source_complete", sequence=2)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert all(row["config_hash"] == app.config_hash for row in rows)


def test_bare_context_reuses_canopy_endpoint_without_canopy_metadata(
    tmp_path: Path,
) -> None:
    endpoint = tmp_path / "endpoint.tif"
    canopy_surface = tmp_path / "canopy.tif"
    context = BatchContext(
        batch_id="batch",
        batch_index=1,
        source_cells=["source-a"],
        all_sample_points=gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"),
        aoi_wgs84=gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"),
        aoi_projected=gpd.GeoDataFrame(geometry=[], crs="EPSG:32610"),
        water_mask_path=tmp_path / "water.tif",
        analysis_dem_path=canopy_surface,
        endpoint_dem_path=endpoint,
        water_mask_arr=np.zeros((1, 1), dtype=bool),
        water_transform=None,
        source_cell_metadata=pd.DataFrame(),
        raw_dem_path=tmp_path / "raw.tif",
        dem_projected_path=tmp_path / "projected.tif",
        dem_clip_path=tmp_path / "clip.tif",
        aligned_canopy_height_path=tmp_path / "chm.tif",
        surface_metadata={
            "terrain_surface_model": "canopy",
            "observer_base_surface": "endpoint_dtm",
            "positive_canopy_pixel_count": 42,
            "maximum_canopy_height_m": 18.0,
        },
    )

    bare = runner._bare_earth_context_from_canopy(context)

    assert bare.analysis_dem_path == endpoint
    assert bare.aligned_canopy_height_path is None
    assert bare.surface_metadata["terrain_surface_model"] == "bare_earth"
    assert bare.surface_metadata["observer_base_surface"] == "endpoint_dtm"
    assert "positive_canopy_pixel_count" not in bare.surface_metadata
    assert "maximum_canopy_height_m" not in bare.surface_metadata
    assert context.analysis_dem_path == canopy_surface


def test_batch_runtime_index_uses_batch_owned_grid_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = BatchContext(
        batch_id="batch",
        batch_index=1,
        source_cells=["source-a", "source-b"],
        all_sample_points=gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"),
        aoi_wgs84=gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"),
        aoi_projected=gpd.GeoDataFrame(geometry=[], crs="EPSG:32610"),
        water_mask_path=tmp_path / "water.tif",
        analysis_dem_path=tmp_path / "analysis.tif",
        endpoint_dem_path=tmp_path / "endpoint.tif",
        water_mask_arr=np.zeros((1, 1), dtype=bool),
        water_transform=None,
        source_cell_metadata=pd.DataFrame(),
        raw_dem_path=tmp_path / "raw.tif",
        dem_projected_path=tmp_path / "projected.tif",
        dem_clip_path=tmp_path / "clip.tif",
        canonical_water_mask_path=tmp_path / "canonical-water.tif",
        surface_metadata={"batch_preparation_elapsed_seconds": 2.0},
    )
    captured: dict[str, object] = {}
    compact_index = object()

    def build_index(app, path, **grid_metadata):
        captured["app"] = app
        captured["path"] = path
        captured.update(grid_metadata)
        return compact_index

    monkeypatch.setattr(runner, "canonical_pixel_h3_window", build_index)
    app = SimpleNamespace(h3=SimpleNamespace(output_resolution=8))
    result = runner._attach_batch_runtime_indexes(
        app,
        context,
        ["source-a", "source-b"],
        lookup_targets={
            "source-a": frozenset({"target-1", "target-2"}),
            "source-b": frozenset({"target-2", "target-3"}),
        },
    )

    assert captured["app"] is app
    assert captured["path"] == context.canonical_water_mask_path
    assert captured["batch_water_mask"] is context.water_mask_arr
    assert captured["batch_transform"] is context.water_transform
    assert captured["batch_crs"] is context.water_crs
    assert result.water_pixel_h3_index is compact_index
    assert result.surface_metadata["batch_runtime_index_elapsed_seconds"] >= 0.0
    assert result.surface_metadata["batch_preparation_elapsed_seconds"] >= 2.0


def test_paired_surface_runner_prepares_batch_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = load_app_config("configs/salish_sea.yaml")
    base = replace(
        base,
        paths=replace(base.paths, output_dir=tmp_path),
        run=replace(
            base.run,
            overwrite=False,
            skip_existing_partitions=True,
            combine_final_parquet=False,
            keep_batch_intermediates=False,
        ),
        source_type="land",
    )
    bare_app = canopy_visibility.make_terrain_variant_app(
        base,
        surface_model="bare_earth",
        terrain_dir=tmp_path / "terrain" / "bare",
    )
    canopy_app = canopy_visibility.make_terrain_variant_app(
        base,
        surface_model="canopy",
        terrain_dir=tmp_path / "terrain" / "canopy",
    )
    cell = "8828d11535fffff"
    sources = gpd.GeoDataFrame(
        {"h3_cell": [cell]},
        geometry=[Point(-123.1, 48.5)],
        crs="EPSG:4326",
    )
    samples = gpd.GeoDataFrame(
        {
            "source_h3_cell": [cell],
            "sample_id": [f"{cell}_sample_001"],
            "sample_index": [1],
        },
        geometry=[Point(-123.1, 48.5)],
        crs="EPSG:4326",
    )
    diagnostics = pd.DataFrame(
        {
            "source_h3": [cell],
            "source_type": ["land"],
            "active_source_fraction": [1.0],
            "source_sampling_mode": ["active_fraction"],
            "sample_points_max": [10],
            "sample_points_min": [1],
            "sample_points_requested": [1],
            "sample_points_actual": [1],
        }
    )
    endpoint = tmp_path / "endpoint.tif"
    canopy_surface = tmp_path / "canopy_surface.tif"
    prepared_context = BatchContext(
        batch_id="shared-batch",
        batch_index=1,
        source_cells=[cell],
        all_sample_points=samples,
        aoi_wgs84=sources,
        aoi_projected=sources.to_crs("EPSG:32610"),
        water_mask_path=tmp_path / "water.tif",
        analysis_dem_path=canopy_surface,
        endpoint_dem_path=endpoint,
        water_mask_arr=np.zeros((1, 1), dtype=bool),
        water_transform=None,
        source_cell_metadata=sources,
        raw_dem_path=tmp_path / "raw.tif",
        dem_projected_path=tmp_path / "projected.tif",
        dem_clip_path=tmp_path / "clip.tif",
        aligned_canopy_height_path=tmp_path / "chm.tif",
        surface_metadata={
            "terrain_surface_model": "canopy",
            "positive_canopy_pixel_count": 1,
        },
    )
    prepare_calls: list[tuple[str, ...]] = []
    run_calls: list[tuple[str, Path]] = []
    mixed_surface_barrier = threading.Barrier(2)
    memory_pending_counts: list[int] = []
    observer_executor_ids: set[int] = set()
    validation_calls: list[Path] = []

    monkeypatch.setattr(runner, "validate_viewshed_inputs", lambda _app: None)
    monkeypatch.setattr(
        runner,
        "validate_batch_raster_alignment",
        lambda context: validation_calls.append(context.analysis_dem_path),
    )
    monkeypatch.setattr(runner, "domain_target_water_area_by_h3", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_attach_batch_runtime_indexes",
        lambda _app, context, _sources: context,
    )
    monkeypatch.setattr(runner, "_load_terrain_source_cells", lambda _app: sources)
    monkeypatch.setattr(runner, "_lookup_source_cells", lambda _app: None)
    monkeypatch.setattr(
        runner,
        "prepare_source_samples",
        lambda *_args, **_kwargs: (samples, diagnostics),
    )
    monkeypatch.setattr(
        runner,
        "write_source_sampling_diagnostics",
        lambda app, _frame: (app.paths.output_dir / "sampling.parquet", {}),
    )
    monkeypatch.setattr(
        runner,
        "iter_source_batches",
        lambda *_args, **_kwargs: [[cell]],
    )
    monkeypatch.setattr(
        runner,
        "_partition_path_for_source",
        lambda app, source: (
            tmp_path / str(app.viewshed.surface_model) / f"source_h3_cell={source}.parquet"
        ),
    )

    def prepare(_app, source_cells, **_kwargs):
        prepare_calls.append(tuple(source_cells))
        return prepared_context

    monkeypatch.setattr(runner, "prepare_batch_context", prepare)
    shared_execution = SimpleNamespace(
        array_row_offset=0,
        array_row_end=1,
        array_col_offset=0,
        array_col_end=1,
        accumulator_shape=(1, 1),
        accumulator_pixel_count=1,
        window_fingerprint="fixture",
        sample_points_actual=1,
        observer_jobs=(({"max_distance_m": 1_000.0}, {}),),
        use_full_grid=False,
    )
    source_execution_calls: list[str] = []

    def prepare_execution(_app, _context, source):
        source_execution_calls.append(str(source))
        return shared_execution

    monkeypatch.setattr(
        runner,
        "prepare_source_execution",
        prepare_execution,
    )

    def memory_bound(
        _app,
        _context,
        *,
        requested_workers,
        pending_source_count,
        observer_workers,
        source_window_pixel_counts,
    ):
        memory_pending_counts.append(int(pending_source_count))
        assert int(observer_workers) == int(requested_workers)
        assert source_window_pixel_counts == [1, 1]
        return min(int(requested_workers), int(pending_source_count)), 1.0

    monkeypatch.setattr(
        runner,
        "_memory_bounded_source_worker_count",
        memory_bound,
    )

    def run_source(app, context, source_cell, **kwargs):
        run_calls.append((str(app.viewshed.surface_model), context.analysis_dem_path))
        observer_executor_ids.add(id(kwargs["observer_executor"]))
        assert kwargs["prepared_execution"] is shared_execution
        assert kwargs["validate_rasters"] is False
        mixed_surface_barrier.wait(timeout=2.0)
        return SimpleNamespace(
            n_rows=1,
            open_water_shortcut_pairs=0,
            dem_viewshed_pairs=1,
            partition_path=tmp_path / f"{app.viewshed.surface_model}-{source_cell}.parquet",
        )

    monkeypatch.setattr(runner, "run_source_cell_in_batch", run_source)
    monkeypatch.setattr(runner, "write_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "cleanup_batch_context", lambda *_args: None)
    monkeypatch.setattr(runner, "cleanup_batches_dir", lambda *_args: None)

    bare_manifest, canopy_manifest = runner.run_paired_surface_source_cells(
        bare_app,
        canopy_app,
    )

    assert prepare_calls == [(cell,)]
    assert source_execution_calls == [cell]
    assert set(run_calls) == {
        ("bare_earth", endpoint),
        ("canopy", canopy_surface),
    }
    assert memory_pending_counts == [2]
    assert set(validation_calls) == {endpoint, canopy_surface}
    assert len(observer_executor_ids) == 1
    assert bare_manifest["status"].tolist() == ["ok"]
    assert canopy_manifest["status"].tolist() == ["ok"]


def test_cleanup_batches_dir_honors_keep_batch_intermediates(
    tmp_path: Path,
) -> None:
    batches = tmp_path / "batches"
    batches.mkdir()
    marker = batches / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    keep_app = SimpleNamespace(
        run=SimpleNamespace(keep_batch_intermediates=True),
        paths=SimpleNamespace(output_dir=tmp_path),
    )

    cleanup.cleanup_batches_dir(keep_app)
    assert marker.exists()

    delete_app = SimpleNamespace(
        run=SimpleNamespace(keep_batch_intermediates=False),
        paths=SimpleNamespace(output_dir=tmp_path),
    )
    cleanup.cleanup_batches_dir(delete_app)
    assert marker.read_text() == "keep"
    marker.unlink()
    cleanup.cleanup_batches_dir(keep_app)
    assert batches.exists()
    cleanup.cleanup_batches_dir(delete_app)
    assert not batches.exists()


def _exhaustive_packing_reference(
    remainder_groups: list[batch_validation._BatchPackingGroup],
    *,
    batch_size: int,
    padding_m: float,
    resolution_m: float,
    max_pixels: int | None,
) -> list[batch_validation._BatchPackingGroup]:
    """Greedily merge nearby parent remainders when raster cost cannot increase."""

    packed = list(remainder_groups)
    while True:
        best: (
            tuple[
                tuple[int, int, int, tuple[str, ...]],
                int,
                int,
                batch_validation._BatchPackingGroup,
            ]
            | None
        ) = None
        for left_index, left in enumerate(packed):
            left_pixels = batch_validation._packing_group_pixel_count(
                left,
                padding_m=padding_m,
                resolution_m=resolution_m,
            )
            for right_index in range(left_index + 1, len(packed)):
                right = packed[right_index]
                if len(left.cells) + len(right.cells) > batch_size:
                    continue
                merged = batch_validation._merge_packing_groups(left, right)
                merged_pixels = batch_validation._packing_group_pixel_count(
                    merged,
                    padding_m=padding_m,
                    resolution_m=resolution_m,
                )
                if max_pixels is not None and merged_pixels > max_pixels:
                    continue
                right_pixels = batch_validation._packing_group_pixel_count(
                    right,
                    padding_m=padding_m,
                    resolution_m=resolution_m,
                )
                pixel_savings = left_pixels + right_pixels - merged_pixels
                if pixel_savings < 0:
                    continue
                # Lowest tuple wins: fill the configured batch capacity first,
                # then maximize pixel savings.  Prioritizing fill prevents a
                # cheap partial merge from stranding groups that could have
                # formed a complete batch, while the non-negative-savings gate
                # above still prevents spatially counterproductive merges.
                score = (
                    -len(merged.cells),
                    -int(pixel_savings),
                    int(merged_pixels),
                    merged.cells,
                )
                candidate = (score, left_index, right_index, merged)
                if best is None or candidate[0] < best[0]:
                    best = candidate
        if best is None:
            break
        _, left_index, right_index, merged = best
        packed = [
            group for index, group in enumerate(packed) if index not in {left_index, right_index}
        ]
        packed.append(merged)
    return packed


def test_heap_packing_matches_exhaustive_policy() -> None:
    rng = np.random.default_rng(1203)
    for count in (0, 1, 8, 40):
        groups = []
        for i in range(count):
            x, y = rng.uniform(0, 200_000, size=2)
            cells = tuple(f"{i:04d}-{j}" for j in range(int(rng.integers(1, 8))))
            groups.append(batch_validation._BatchPackingGroup(cells, x, y, x + 1500, y + 2000))
        for batch_size in (7, 14, 49):
            for limit in (None, 6_000_000):
                kwargs = dict(
                    batch_size=batch_size, padding_m=31_000.0, resolution_m=30.0, max_pixels=limit
                )
                expected = _exhaustive_packing_reference(groups, **kwargs)
                actual = batch_validation._pack_h3_parent_remainders(groups, **kwargs)
                assert actual == expected
                reversed_result = batch_validation._pack_h3_parent_remainders(
                    list(reversed(groups)), **kwargs
                )
                assert sorted(group.cells for group in actual) == sorted(
                    group.cells for group in reversed_result
                )
