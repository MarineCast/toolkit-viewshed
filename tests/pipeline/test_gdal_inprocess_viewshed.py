from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

from viewshed_toolkit.pipeline.config.loader import BatchContext
from viewshed_toolkit.pipeline.weights.terrain import los


def _fixture_context(tmp_path: Path) -> tuple[BatchContext, SimpleNamespace, float, float]:
    dem_path = tmp_path / "analysis_dem.tif"
    size = 81
    transform = from_origin(500_000.0, 5_400_000.0, 30.0, 30.0)
    rows, columns = np.mgrid[:size, :size]
    elevations = (
        10.0
        + 80.0 * np.exp(-((columns - 48) ** 2 + (rows - 35) ** 2) / 90.0)
        + 35.0 * np.exp(-((columns - 25) ** 2 + (rows - 52) ** 2) / 55.0)
    ).astype("float32")
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=size,
        height=size,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=transform,
    ) as destination:
        destination.write(elevations, 1)

    empty_wgs84 = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    empty_projected = gpd.GeoDataFrame(geometry=[], crs="EPSG:32610")
    context = BatchContext(
        batch_id="in-process-fixture",
        batch_index=0,
        source_cells=["source"],
        all_sample_points=empty_wgs84,
        aoi_wgs84=empty_wgs84,
        aoi_projected=empty_projected,
        water_mask_path=dem_path,
        analysis_dem_path=dem_path,
        endpoint_dem_path=dem_path,
        water_mask_arr=np.ones((size, size), dtype=bool),
        water_transform=transform,
        source_cell_metadata=pd.DataFrame(),
        raw_dem_path=dem_path,
        dem_projected_path=dem_path,
        dem_clip_path=dem_path,
        water_crs=CRS.from_epsg(32610),
        water_shape=(size, size),
        water_pixel_area_m2=900.0,
    )
    app = SimpleNamespace(
        run=SimpleNamespace(
            keep_intermediate_rasters=False,
            overwrite=True,
        ),
        paths=SimpleNamespace(output_dir=tmp_path / "output"),
        viewshed=SimpleNamespace(surface_model="bare_earth"),
    )
    observer_x, observer_y = transform * (40.5, 40.5)
    return context, app, float(observer_x), float(observer_y)


def _arguments(
    context: BatchContext,
    app: SimpleNamespace,
    observer_x: float,
    observer_y: float,
) -> dict[str, object]:
    return {
        "context": context,
        "observer_x": observer_x,
        "observer_y": observer_y,
        "observer_height_m": 1.7,
        "target_height_m": 1.0,
        "max_distance_m": 900.0,
        "curvature_coefficient": 0.85714,
        "app": app,
        "source_cell": "source",
        "sample_index": 1,
        "observer_id": "source_001",
    }


@pytest.mark.skipif(shutil.which("gdal_viewshed") is None, reason="GDAL CLI unavailable")
def test_in_process_mem_viewshed_matches_cli_boolean_grid(tmp_path: Path) -> None:
    context, app, observer_x, observer_y = _fixture_context(tmp_path)
    arguments = _arguments(context, app, observer_x, observer_y)
    los._GDAL_WORKER_DATASETS.datasets = {}

    in_process = los._run_gdal_viewshed_in_process_to_bool_array(**arguments)
    cli = los._run_gdal_viewshed_cli_to_bool_array(**arguments)

    assert in_process.visible.shape == cli.visible.shape
    assert np.array_equal(in_process.visible, cli.visible)
    assert (in_process.y_start, in_process.x_start) == (cli.y_start, cli.x_start)
    assert in_process.metadata["gdal_execution_mode"] == "in_process_mem"
    assert in_process.metadata["uses_external_process"] is False
    assert in_process.metadata["uses_temp_raster"] is False


def test_gdal_dataset_handles_are_reused_only_within_each_worker(
    tmp_path: Path,
) -> None:
    context, _, _, _ = _fixture_context(tmp_path)
    barrier = threading.Barrier(2)

    def open_twice() -> tuple[int, bool]:
        los._GDAL_WORKER_DATASETS.datasets = {}
        barrier.wait(timeout=5)
        _, first = los._worker_gdal_dataset(context.analysis_dem_path)
        _, second = los._worker_gdal_dataset(context.analysis_dem_path)
        barrier.wait(timeout=5)
        return id(first), first is second

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: open_twice(), range(2)))

    assert all(reused for _, reused in results)
    assert len({dataset_id for dataset_id, _ in results}) == 2


def test_prepared_water_grid_metadata_avoids_raster_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _, _ = _fixture_context(tmp_path)
    monkeypatch.setattr(
        los.rasterio,
        "open",
        lambda *_args, **_kwargs: pytest.fail("prepared water grid should not be reopened"),
    )

    crs, transform, shape = los._context_water_grid(context)

    assert crs == CRS.from_epsg(32610)
    assert transform == context.water_transform
    assert shape == context.water_mask_arr.shape


def test_default_gdal_path_falls_back_to_cli_with_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace()
    app = SimpleNamespace(run=SimpleNamespace(keep_intermediate_rasters=False))
    fallback = los.ViewshedWindowResult(
        visible=np.ones((1, 1), dtype=bool),
        y_start=0,
        x_start=0,
        elapsed_seconds=0.1,
        metadata={"gdal_execution_mode": "cli_fallback"},
    )
    monkeypatch.setattr(los, "_load_gdal_python", lambda: SimpleNamespace())
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_in_process_to_bool_array",
        lambda **_kwargs: (_ for _ in ()).throw(ImportError("binding unavailable")),
    )
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_cli_to_bool_array",
        lambda **_kwargs: fallback,
    )

    result = los.run_gdal_viewshed_to_bool_array(**_arguments(context, app, 1.0, 2.0))

    assert result.metadata["gdal_execution_mode"] == "cli_fallback"
    assert result.metadata["in_process_fallback_error"] == ("ImportError: binding unavailable")


def test_cached_missing_gdal_python_api_routes_directly_to_cli_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = SimpleNamespace()
    app = SimpleNamespace(run=SimpleNamespace(keep_intermediate_rasters=False))
    fallback = los.ViewshedWindowResult(
        visible=np.ones((1, 1), dtype=bool),
        y_start=0,
        x_start=0,
        elapsed_seconds=0.1,
        metadata={"gdal_execution_mode": "cli_fallback"},
    )
    monkeypatch.setattr(los, "_GDAL_PYTHON", None)
    monkeypatch.setattr(los, "_GDAL_PYTHON_IMPORT_ERROR", AttributeError("missing API"))
    monkeypatch.setattr(los, "_GDAL_CLI_FALLBACK_NOTICE_LOGGED", False)
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_in_process_to_bool_array",
        lambda **_kwargs: pytest.fail("known-missing Python API should not be retried"),
    )
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_cli_to_bool_array",
        lambda **_kwargs: fallback,
    )
    caplog.set_level("INFO", logger=los.__name__)

    first = los.run_gdal_viewshed_to_bool_array(**_arguments(context, app, 1.0, 2.0))
    second = los.run_gdal_viewshed_to_bool_array(**_arguments(context, app, 1.0, 2.0))

    assert first.metadata["gdal_execution_mode"] == "cli_fallback"
    assert second.metadata["gdal_execution_mode"] == "cli_fallback"
    notices = [
        record
        for record in caplog.records
        if "using the gdal_viewshed CLI for this process" in record.getMessage()
    ]
    assert len(notices) == 1


def test_retained_debug_raster_routes_directly_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace()
    app = SimpleNamespace(run=SimpleNamespace(keep_intermediate_rasters=True))
    retained = los.ViewshedWindowResult(
        visible=np.ones((1, 1), dtype=bool),
        y_start=0,
        x_start=0,
        elapsed_seconds=0.1,
        metadata={"gdal_execution_mode": "cli_retained_debug"},
    )
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_in_process_to_bool_array",
        lambda **_kwargs: pytest.fail("in-process path should not run"),
    )
    monkeypatch.setattr(
        los,
        "_run_gdal_viewshed_cli_to_bool_array",
        lambda **_kwargs: retained,
    )

    result = los.run_gdal_viewshed_to_bool_array(**_arguments(context, app, 1.0, 2.0))

    assert result is retained


def test_canopy_fixed_observer_is_independent_of_other_observers(tmp_path: Path) -> None:
    """Exercise the real GDAL dispatch, with an obstructing second observer pixel."""
    from dataclasses import replace

    from viewshed_toolkit.pipeline.prepare.elevation.canopy import (
        build_canonical_canopy_base_surface,
        build_observer_grounded_canopy_surface_from_base,
    )

    pytest.importorskip("osgeo.gdal", reason="GDAL Python bindings required for canopy integration")
    context, app, x, y = _fixture_context(tmp_path)
    app.viewshed.surface_model = "canopy"
    app.viewshed.observer_canopy_clearance_radius_m = 0.0
    with rasterio.open(context.endpoint_dem_path) as src:
        profile = src.profile
        shape = (src.height, src.width)
    chm = tmp_path / "chm.tif"
    water = tmp_path / "water.tif"
    for path, data in (
        (chm, np.full(shape, 35, dtype="float32")),
        (water, np.zeros(shape, dtype="float32")),
    ):
        with rasterio.open(path, "w", **profile) as out:
            out.write(data, 1)
    base = build_canonical_canopy_base_surface(
        endpoint_dem_path=context.endpoint_dem_path,
        water_mask_path=water,
        aligned_canopy_path=chm,
        output_surface_path=tmp_path / "base.tif",
    )
    arrays = []
    for index, points in enumerate(([(x, y)], [(x + 30, y), (x, y)], [(x, y)])):
        prepared = build_observer_grounded_canopy_surface_from_base(
            endpoint_dem_path=context.endpoint_dem_path,
            water_mask_path=water,
            aligned_canopy_path=chm,
            base_surface_path=base,
            output_surface_path=tmp_path / f"batch{index}.tif",
            observers_projected=gpd.GeoDataFrame(
                geometry=gpd.points_from_xy(*zip(*points)), crs=profile["crs"]
            ),
        )
        batch = replace(context, analysis_dem_path=prepared.surface_path)
        result = los.run_gdal_viewshed_to_bool_array(**_arguments(batch, app, x, y))
        arrays.append(result.visible)
        with rasterio.open(prepared.surface_path) as src, rasterio.open(base) as expected:
            np.testing.assert_array_equal(src.read(1), expected.read(1))
    for array in arrays[1:]:
        np.testing.assert_array_equal(array, arrays[0])


def test_canonical_ground_two_source_elevations_one_coarse_void_is_batch_invariant(tmp_path):
    from dataclasses import replace

    from shapely.geometry import Point

    from viewshed_toolkit.pipeline.prepare.elevation.terrain import (
        repair_observer_endpoint_nodata,
    )

    pytest.importorskip("osgeo.gdal")
    context, app, x, y = _fixture_context(tmp_path)
    with rasterio.open(context.endpoint_dem_path) as src:
        original = src.read(1)
        profile = src.profile
    original[40, 40] = np.nan
    # Two authoritative 10 m pixels inside the same 30 m endpoint void.
    source_path = tmp_path / "source_fine.tif"
    fine = np.repeat(np.repeat(np.nan_to_num(original, nan=10), 3, axis=0), 3, axis=1)
    fine[121, 120], fine[121, 122] = 20, 80
    fine[121, 121] = 42
    with rasterio.open(
        source_path,
        "w",
        **{
            **profile,
            "height": 243,
            "width": 243,
            "transform": from_origin(500000, 5400000, 10, 10),
        },
    ) as dst:
        dst.write(fine, 1)
    a, b = Point(x - 10, y), Point(x + 10, y)
    baseline = None
    for index, points in enumerate(([a], [a, b], [b, a], [a])):
        path = tmp_path / f"endpoint_{index}.tif"
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(original, 1)
        observers = gpd.GeoDataFrame(geometry=points, crs=profile["crs"])
        repair_observer_endpoint_nodata(path, source_path, observers)
        # Idempotence exercises the resumed preparation path.
        repair_observer_endpoint_nodata(path, source_path, observers)
        with rasterio.open(path) as src:
            assert src.read(1)[40, 40] == 42
        fixed = replace(context, endpoint_dem_path=path, analysis_dem_path=path)
        result = los.run_gdal_viewshed_to_bool_array(**_arguments(fixed, app, a.x, a.y))
        assert result.metadata["gdal_execution_mode"] == "in_process_mem"
        if baseline is None:
            baseline = result.visible
        else:
            np.testing.assert_array_equal(result.visible, baseline)
