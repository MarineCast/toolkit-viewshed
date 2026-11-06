"""Marine-only sea-level assignment preserves inland elevations and missingness."""

from dataclasses import replace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import load_app_config
from viewshed_toolkit.pipeline.api.service import _run_input_fingerprints
from viewshed_toolkit.pipeline.prepare.elevation.terrain import (
    flatten_water_pixels_to_sea_level,
    validate_marine_water_mask,
)
from viewshed_toolkit.pipeline.weights.terrain.gdal import expected_partition_metadata


def raster(path, values, nodata=None, transform=None):
    values = np.asarray(values)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs="EPSG:32610",
        nodata=nodata,
        transform=transform or from_origin(500000, 5400000, 30, 30),
    ) as out:
        out.write(values, 1)
    return path


def test_marine_only_preserves_high_lake_and_sloping_river_and_inland_void(tmp_path):
    dem = np.array([[-9999.0, -4.0, 125.0, 125.0], [50.0, 48.0, 46.0, -9999.0]], dtype="float32")
    src = raster(tmp_path / "dem.tif", dem, -9999)
    marine = raster(tmp_path / "marine.tif", np.array([[1, 1, 0, 0], [0, 0, 0, 0]], dtype="uint8"))
    output = flatten_water_pixels_to_sea_level(
        src, marine, tmp_path / "endpoint.tif", sea_level_m=0.25
    )
    with rasterio.open(output) as ds:
        actual = ds.read(1, masked=True)
        np.testing.assert_array_equal(actual.data[0, :2], [0.25, 0.25])
        np.testing.assert_array_equal(actual.data[:, 2:], dem[:, 2:])
        np.testing.assert_array_equal(actual.data[1, :3], [50, 48, 46])
        assert actual.mask[1, 3] and not actual.mask[0, 0]


def test_no_marine_evidence_preserves_dem_including_nodata(tmp_path):
    values = np.array([[120.0, -9999.0], [0.0, -3.0]], dtype="float32")
    src = raster(tmp_path / "dem.tif", values, -9999)
    output = flatten_water_pixels_to_sea_level(src, None, tmp_path / "endpoint.tif")
    with rasterio.open(output) as ds:
        np.testing.assert_array_equal(ds.read(1), values)


@pytest.mark.parametrize("bad,nodata", [(2, None), (255, 255), (1, 1)])
def test_ambiguous_marine_masks_rejected_before_output(tmp_path, bad, nodata):
    src = raster(tmp_path / "dem.tif", np.ones((2, 2), dtype="float32"), -9999)
    mask = raster(tmp_path / "mask.tif", np.full((2, 2), bad, dtype="uint8"), nodata)
    with pytest.raises(ValueError, match="Marine water mask"):
        flatten_water_pixels_to_sea_level(src, mask, tmp_path / "output.tif")
    assert not (tmp_path / "output.tif").exists()


def test_marine_grid_mismatch_rejected(tmp_path):
    src = raster(tmp_path / "dem.tif", np.ones((2, 2), dtype="float32"), -9999)
    mask = raster(
        tmp_path / "mask.tif",
        np.zeros((2, 2), dtype="uint8"),
        transform=from_origin(500015, 5400000, 30, 30),
    )
    with pytest.raises(ValueError):
        flatten_water_pixels_to_sea_level(src, mask, tmp_path / "output.tif")


def test_marine_content_change_invalidates_land_partition_identity(tmp_path):
    app = load_app_config(coastal_fixture(tmp_path))
    path = raster(tmp_path / "marine.tif", np.zeros((2, 2), dtype="uint8"))
    app = replace(app, paths=replace(app.paths, marine_water_mask_path=path))
    before = expected_partition_metadata(app)
    stages = ("build-dual-surface-canopy-weights",)
    before_stage = _run_input_fingerprints(app, stages)
    with rasterio.open(path, "r+") as ds:
        ds.write(np.ones((2, 2), dtype="uint8"), 1)
    assert expected_partition_metadata(app) != before
    assert _run_input_fingerprints(app, stages) != before_stage
    assert (
        expected_partition_metadata(
            replace(app, paths=replace(app.paths, marine_water_mask_path=None))
        )
        != before
    )


def test_marine_evidence_outside_canonical_water_rejected(tmp_path):
    dem = raster(tmp_path / "dem.tif", np.ones((2, 2), dtype="float32"), -9999)
    marine = raster(tmp_path / "marine.tif", np.array([[1, 0], [0, 0]], dtype="uint8"))
    water = raster(tmp_path / "water.tif", np.array([[0, 1], [0, 0]], dtype="uint8"))
    with pytest.raises(ValueError, match="outside"):
        validate_marine_water_mask(marine, dem, water_mask_path=water)


def test_optional_marine_input_required_for_land_only(tmp_path):
    from viewshed_toolkit.pipeline.prepare.area.inputs import validate_viewshed_inputs

    app = load_app_config(coastal_fixture(tmp_path))
    app = replace(
        app,
        paths=replace(
            app.paths,
            regional_dem_path=tmp_path / "dem.tif",
            canopy_height_path=tmp_path / "chm.tif",
            land_h3_path=app.paths.water_polygon_path,
            marine_water_mask_path=tmp_path / "absent.tif",
        ),
    )
    with pytest.raises(FileNotFoundError, match="marine water mask"):
        validate_viewshed_inputs(app)
    validate_viewshed_inputs(replace(app, source_type="water"))
