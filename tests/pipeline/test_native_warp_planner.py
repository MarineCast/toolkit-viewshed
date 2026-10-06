"""Compare metadata-clone planning and cropped replay with unchanged GDAL producers."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from viewshed_toolkit._internal.geo.raster import reproject_raster
from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.contracts.warp import validate_global_warp_chunk_plan
from viewshed_toolkit.pipeline.prepare.elevation.canopy import align_canopy_height_to_endpoint_dem
from viewshed_toolkit.pipeline.prepare.elevation.warp_planner import generate_native_warp_chunk_plan
from viewshed_toolkit.pipeline.prepare.elevation.windows import (
    ensure_canopy_window,
    ensure_projected_dem_window,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native_helper(tmp_path_factory):
    spec = importlib.util.spec_from_file_location(
        "native_helper_build", ROOT / "scripts/build_native_warp_helper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = tmp_path_factory.mktemp("native-helper") / "planner"
    module.build(
        ROOT / "src/viewshed_toolkit/resources/native_warp_plan.cpp",
        helper,
        Path(sys.prefix) / "bin/gdal-config",
    )
    return helper


@pytest.mark.parametrize(
    "kind,nodata,dtype,source_crs,size",
    [
        (kind, nodata, dtype, crs, 181)
        for crs in ("EPSG:4326", "EPSG:5070")
        for kind, nodata, dtype in (
            ("dem", None, "float32"),
            ("dem", -9999, "float32"),
            ("dem", np.nan, "float32"),
            ("canopy", 255, "uint8"),
        )
    ]
    + [("canopy", 255, "uint8", "EPSG:4326", 3200)],
)
def test_metadata_clone_chunks_and_pixels_match_original(
    tmp_path, caplog, native_helper, kind, nodata, dtype, source_crs, size
):
    import json

    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    source = tmp_path / "source.tif"
    transform = (
        from_origin(-123.06, 48.06, 0.0003, 0.0003)
        if source_crs == "EPSG:4326"
        else from_origin(-2029873, 3099663, 8.113975, 8.113975)
    )
    rows, columns = (
        np.arange(size, dtype="float32")[:, None],
        np.arange(size + 10, dtype="float32")[None, :],
    )
    values = (20 + 7 * np.sin(rows / 4) + columns / 2).astype(dtype)
    if nodata is not None:
        values[40:120, 40:120] = nodata
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=size + 10,
        height=size,
        count=1,
        dtype=dtype,
        crs=source_crs,
        transform=transform,
        nodata=nodata,
    ) as raster:
        raster.write(values, 1)
    reference = tmp_path / "global-dem.tif"
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rasterio._err"), rasterio.Env(CPL_DEBUG=True):
        reproject_raster(
            source,
            reference,
            "EPSG:32610",
            30,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
    if kind == "canopy":
        caplog.clear()
        canopy_reference = tmp_path / "global-canopy.tif"
        with caplog.at_level(logging.DEBUG, logger="rasterio._err"), rasterio.Env(CPL_DEBUG=True):
            align_canopy_height_to_endpoint_dem(source, reference, canopy_reference)
        expected_path = canopy_reference
    else:
        expected_path = reference
    native_chunks = []
    for record in caplog.records:
        match = re.search(r"GDALWarpKernel\(\).*Dst=(\d+),(\d+),(\d+)x(\d+)", record.getMessage())
        if match:
            native_chunks.append([int(v) for v in match.groups()])
    assert native_chunks
    if size == 3200:
        assert len(native_chunks) > 1
    app = replace(
        app,
        paths=replace(
            app.paths,
            regional_dem_path=source,
            canopy_height_path=source,
            projected_dem_path=tmp_path / "never-created.tif",
        ),
        batch=replace(
            app.batch,
            native_warp_helper_path=str(native_helper),
            native_warp_helper_sha256=hashlib.sha256(native_helper.read_bytes()).hexdigest(),
        ),
    )
    with rasterio.open(reference) as raster:
        plan = generate_native_warp_chunk_plan(
            app, source, raster.transform, raster.width, raster.height, kind=kind
        )
    payload = json.loads(plan.read_text())
    assert payload["chunks"] == native_chunks
    assert payload["planner"]["native_raster_pixel_reads"] == 0
    assert payload["planner"]["regional_rasters_materialized"] == 0
    windows = [Window(7, 9, 21, 19), Window(20, 22, 21, 19)]
    if len(native_chunks) > 1:
        # Cross the native chunk boundary; arbitrary crop chunking changes scales.
        x, y, _width, _height = native_chunks[1]
        windows.append(Window(max(0, x - 7), max(0, y - 9), 21, 19))
    for window in windows:
        with rasterio.open(expected_path) as raster:
            expected = raster.read(1, window=window)
            bounds = raster.window_bounds(window)
            expected_transform = raster.window_transform(window)
        dem = ensure_projected_dem_window(app, bounds)
        output = dem if kind == "dem" else ensure_canopy_window(app, dem)
        with rasterio.open(output) as raster:
            assert raster.transform == expected_transform
            np.testing.assert_array_equal(raster.read(1), expected, strict=True)
    assert not app.paths.projected_dem_path.exists()
    # A changed binary cannot return a warm receipt or start a child.
    altered = replace(app, batch=replace(app.batch, native_warp_helper_sha256="0" * 64))
    with rasterio.open(reference) as raster, pytest.raises(ValueError, match="SHA256 mismatch"):
        generate_native_warp_chunk_plan(
            altered, source, raster.transform, raster.width, raster.height, kind=kind
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dummy_read_calls", -1),
        ("dummy_read_bytes", True),
        ("native_raster_pixel_reads", 1),
        ("regional_rasters_materialized", 1),
        ("metadata_elapsed_seconds", float("nan")),
        ("metadata_elapsed_seconds", -1),
        ("metadata_elapsed_seconds", 62),
        ("peak_child_rss_bytes", 513 * 1024**2),
        ("peak_process_rss_bytes", 769 * 1024**2),
    ],
)
def test_native_planner_receipt_rejects_scope_and_resource_damage(field, value):
    expected = {"schema_version": 2, "shape": [10, 10], "transform": [30, 0, 0, 0, -30, 0, 0, 0, 1]}
    planner = {
        "backend_sha256": "a" * 64,
        "native_raster_pixel_reads": 0,
        "regional_rasters_materialized": 0,
        "dummy_read_calls": 1,
        "dummy_read_bytes": 400,
        "metadata_elapsed_seconds": 0.1,
        "peak_child_rss_bytes": 1,
        "peak_process_rss_bytes": 2,
    }
    payload = {**expected, "chunks": [[0, 0, 10, 10]], "planner": planner}
    validate_global_warp_chunk_plan(payload, expected=expected)
    planner[field] = value
    with pytest.raises(ValueError):
        validate_global_warp_chunk_plan(payload, expected=expected)


def test_metadata_helper_preserves_rejection_of_external_masks(tmp_path, native_helper):
    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    source = tmp_path / "masked.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=50,
        height=50,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-123.06, 48.06, 0.0003, 0.0003),
    ) as raster:
        raster.write(np.ones((50, 50), dtype="float32"), 1)
        raster.write_mask(np.full((50, 50), 255, dtype="uint8"))
    app = replace(
        app,
        paths=replace(app.paths, projected_dem_path=tmp_path / "unused.tif"),
        batch=replace(
            app.batch,
            native_warp_helper_path=str(native_helper),
            native_warp_helper_sha256=hashlib.sha256(native_helper.read_bytes()).hexdigest(),
        ),
    )
    with pytest.raises(RuntimeError, match="preserved diagnostic log"):
        generate_native_warp_chunk_plan(
            app, source, from_origin(500000, 5400000, 30, 30), 40, 40, kind="dem"
        )
    logs = list(tmp_path.rglob("native-*.log"))
    assert len(logs) == 1
    assert "external/alpha masks require separate planner qualification" in logs[0].read_text()
    assert not list(tmp_path.rglob("plan.json"))


def test_aligned_source_mask_cannot_skip_window_qualification(tmp_path):
    source = tmp_path / "aligned-mask.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=50,
        height=50,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=from_origin(500000, 5400000, 30, 30),
    ) as raster:
        raster.write(np.ones((50, 50), dtype="float32"), 1)
        raster.write_mask(np.full((50, 50), 255, dtype="uint8"))
    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    app = replace(
        app,
        paths=replace(
            app.paths, regional_dem_path=source, projected_dem_path=tmp_path / "unused.tif"
        ),
    )
    with pytest.raises(ValueError, match="without external/alpha masks"):
        ensure_projected_dem_window(app, (500000, 5399000, 501000, 5400000))
    assert not list(tmp_path.rglob("projected_dem_windows"))
