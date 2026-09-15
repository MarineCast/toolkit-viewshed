from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin, xy
from shapely.geometry import Point

from viewshed_toolkit.pipeline.prepare.elevation.canopy import (
    build_canonical_canopy_base_surface,
    build_canopy_obstacle_surface,
    build_observer_grounded_canopy_surface_from_base,
)


def _write_raster(
    path: Path,
    values: np.ndarray,
    *,
    nodata: float | int,
    dtype: str,
) -> Path:
    transform = from_origin(500_000.0, 5_400_000.0, 30.0, 30.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=dtype,
        crs="EPSG:32610",
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(values.astype(dtype), 1)
    return path


def _observer(row: int, col: int) -> gpd.GeoDataFrame:
    transform = from_origin(500_000.0, 5_400_000.0, 30.0, 30.0)
    x, y = xy(transform, row, col, offset="center")
    return gpd.GeoDataFrame(
        {"observer_id": ["observer-1"]},
        geometry=[Point(float(x), float(y))],
        crs="EPSG:32610",
    )


def test_canopy_surface_uses_dtm_at_observer_and_water_only(tmp_path: Path) -> None:
    endpoint = np.full((5, 6), 100.0, dtype="float32")
    endpoint[:, -1] = 0.0
    water = np.zeros(endpoint.shape, dtype="uint8")
    water[:, -1] = 1
    canopy = np.full(endpoint.shape, 8.0, dtype="float32")

    endpoint_path = _write_raster(
        tmp_path / "endpoint.tif", endpoint, nodata=np.nan, dtype="float32"
    )
    water_path = _write_raster(tmp_path / "water.tif", water, nodata=0, dtype="uint8")
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy, nodata=255.0, dtype="float32")

    result = build_canopy_obstacle_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        canopy_height_path=canopy_path,
        aligned_canopy_path=tmp_path / "canopy_aligned.tif",
        output_surface_path=tmp_path / "obstacle_surface.tif",
        observers_projected=_observer(2, 1),
        resampling="nearest",
        nodata_policy="error",
    )

    with rasterio.open(result.surface_path) as src:
        surface = src.read(1)
        tags = src.tags()

    assert surface[2, 1] == pytest.approx(endpoint[2, 1])
    assert surface[2, 2] == pytest.approx(endpoint[2, 2] + canopy[2, 2])
    assert np.allclose(surface[:, -1], endpoint[:, -1])
    assert result.observer_pixel_count == 1
    assert result.positive_canopy_pixel_count == 25
    assert tags["observer_base_surface"] == "endpoint_dtm"
    assert tags["intervening_obstacle_surface"] == "endpoint_dem_plus_chm"
    assert tags["landcover_used"] == "false"


def test_canopy_surface_rejects_missing_chm_over_land(tmp_path: Path) -> None:
    endpoint = np.zeros((3, 4), dtype="float32")
    water = np.zeros(endpoint.shape, dtype="uint8")
    canopy = np.ones(endpoint.shape, dtype="float32")
    canopy[1, 2] = 255.0

    endpoint_path = _write_raster(
        tmp_path / "endpoint.tif", endpoint, nodata=np.nan, dtype="float32"
    )
    water_path = _write_raster(tmp_path / "water.tif", water, nodata=0, dtype="uint8")
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy, nodata=255.0, dtype="float32")

    with pytest.raises(ValueError, match="missing values over DEM-defined land"):
        build_canopy_obstacle_surface(
            endpoint_dem_path=endpoint_path,
            water_mask_path=water_path,
            canopy_height_path=canopy_path,
            aligned_canopy_path=tmp_path / "canopy_aligned.tif",
            output_surface_path=tmp_path / "obstacle_surface.tif",
            observers_projected=_observer(1, 1),
            resampling="nearest",
            nodata_policy="error",
        )


def test_canopy_surface_can_explicitly_treat_masked_classes_as_zero(tmp_path: Path) -> None:
    endpoint = np.full((3, 4), 10.0, dtype="float32")
    water = np.zeros(endpoint.shape, dtype="uint8")
    canopy = np.full(endpoint.shape, 5.0, dtype="float32")
    canopy[1, 2] = 255.0

    endpoint_path = _write_raster(
        tmp_path / "endpoint.tif", endpoint, nodata=np.nan, dtype="float32"
    )
    water_path = _write_raster(tmp_path / "water.tif", water, nodata=0, dtype="uint8")
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy, nodata=255.0, dtype="float32")

    result = build_canopy_obstacle_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        canopy_height_path=canopy_path,
        aligned_canopy_path=tmp_path / "canopy_aligned.tif",
        output_surface_path=tmp_path / "obstacle_surface.tif",
        observers_projected=_observer(1, 1),
        resampling="nearest",
        nodata_policy="zero",
    )

    with rasterio.open(result.surface_path) as src:
        surface = src.read(1)

    assert surface[1, 2] == pytest.approx(endpoint[1, 2])
    assert result.missing_land_canopy_pixel_count == 1


def test_canopy_surface_cache_invalidates_when_observer_design_changes(
    tmp_path: Path,
) -> None:
    endpoint = np.full((4, 5), 100.0, dtype="float32")
    water = np.zeros(endpoint.shape, dtype="uint8")
    canopy = np.full(endpoint.shape, 9.0, dtype="float32")

    endpoint_path = _write_raster(
        tmp_path / "endpoint.tif", endpoint, nodata=np.nan, dtype="float32"
    )
    water_path = _write_raster(tmp_path / "water.tif", water, nodata=0, dtype="uint8")
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy, nodata=255.0, dtype="float32")
    aligned_path = tmp_path / "canopy_aligned.tif"
    surface_path = tmp_path / "obstacle_surface.tif"

    build_canopy_obstacle_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        canopy_height_path=canopy_path,
        aligned_canopy_path=aligned_path,
        output_surface_path=surface_path,
        observers_projected=_observer(1, 1),
        resampling="nearest",
        nodata_policy="error",
    )
    with rasterio.open(surface_path) as first:
        first_surface = first.read(1)
        first_fingerprint = first.tags()["cache_fingerprint"]

    build_canopy_obstacle_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        canopy_height_path=canopy_path,
        aligned_canopy_path=aligned_path,
        output_surface_path=surface_path,
        observers_projected=_observer(1, 2),
        resampling="nearest",
        nodata_policy="error",
    )
    with rasterio.open(surface_path) as second:
        second_surface = second.read(1)
        second_fingerprint = second.tags()["cache_fingerprint"]
    with rasterio.open(aligned_path) as aligned:
        alignment_tags = aligned.tags()

    assert first_fingerprint != second_fingerprint
    assert first_surface[1, 1] == pytest.approx(100.0)
    assert second_surface[1, 1] == pytest.approx(109.0)
    assert second_surface[1, 2] == pytest.approx(100.0)
    assert alignment_tags["algorithm_version"] == "canopy_alignment_v2"
    assert alignment_tags["cache_fingerprint"]


def test_canonical_batch_surface_defers_grounding(
    tmp_path: Path,
) -> None:
    endpoint = np.full((5, 6), 100.0, dtype="float32")
    endpoint[:, -1] = 0.0
    water = np.zeros(endpoint.shape, dtype="uint8")
    water[:, -1] = 1
    canopy = np.arange(endpoint.size, dtype="float32").reshape(endpoint.shape)
    endpoint_path = _write_raster(
        tmp_path / "endpoint.tif", endpoint, nodata=np.nan, dtype="float32"
    )
    water_path = _write_raster(tmp_path / "water.tif", water, nodata=0, dtype="uint8")
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy, nodata=255.0, dtype="float32")
    observer = _observer(2, 1)

    legacy = build_canopy_obstacle_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        canopy_height_path=canopy_path,
        aligned_canopy_path=tmp_path / "legacy_aligned.tif",
        output_surface_path=tmp_path / "legacy_surface.tif",
        observers_projected=observer,
        resampling="nearest",
        nodata_policy="zero",
    )
    base_path = build_canonical_canopy_base_surface(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        aligned_canopy_path=canopy_path,
        output_surface_path=tmp_path / "canonical_base.tif",
    )
    canonical = build_observer_grounded_canopy_surface_from_base(
        endpoint_dem_path=endpoint_path,
        water_mask_path=water_path,
        aligned_canopy_path=canopy_path,
        base_surface_path=base_path,
        output_surface_path=tmp_path / "canonical_grounded_surface.tif",
        observers_projected=observer,
        nodata_policy="zero",
    )

    with rasterio.open(legacy.surface_path) as legacy_source:
        legacy_values = legacy_source.read(1)
    with rasterio.open(canonical.surface_path) as canonical_source:
        canonical_values = canonical_source.read(1)
        canonical_tags = canonical_source.tags()

    assert canonical_values[2, 1] == endpoint[2, 1] + canopy[2, 1]
    assert legacy_values[2, 1] == endpoint[2, 1]
    with rasterio.open(base_path) as base:
        np.testing.assert_array_equal(canonical_values, base.read(1))
    assert canonical.observer_pixel_count == 0
    assert canonical.positive_canopy_pixel_count == legacy.positive_canopy_pixel_count
    assert canonical_tags["intervening_obstacle_surface"] == ("canonical_endpoint_dem_plus_chm")


@pytest.mark.parametrize("row,col,radius", [(15, 16, 0.0), (15, 16, 30.0), (0, 0, 45.0)])
def test_private_vrt_matches_full_copy_and_gdal_los(tmp_path, row, col, radius):
    gdal = pytest.importorskip("osgeo.gdal")
    from viewshed_toolkit.pipeline.prepare.elevation.canopy import (
        isolate_observer_canopy_surface,
        isolate_observer_canopy_vrt,
    )

    rng = np.random.default_rng(341)
    ground = rng.uniform(0, 30, size=(32, 32)).astype("float32")
    canopy = ground + rng.uniform(0, 40, size=(32, 32)).astype("float32")
    canopy[-1, -1] = -9999.0
    base = _write_raster(tmp_path / "base.tif", canopy, nodata=-9999.0, dtype="float32")
    endpoint = _write_raster(tmp_path / "ground.tif", ground, nodata=-9999.0, dtype="float32")
    point = _observer(row, col).geometry.iloc[0]
    kwargs = dict(
        base_surface_path=base,
        endpoint_dem_path=endpoint,
        observer_x=point.x,
        observer_y=point.y,
        clearance_radius_m=radius,
    )
    reference = isolate_observer_canopy_surface(output_path=tmp_path / "reference.tif", **kwargs)
    virtual = isolate_observer_canopy_vrt(output_path=tmp_path / "virtual.vrt", **kwargs)
    with rasterio.open(reference) as a, rasterio.open(virtual) as b:
        np.testing.assert_array_equal(a.read(), b.read())
        assert (a.crs, a.transform, a.nodata) == (b.crs, b.transform, b.nodata)
    with rasterio.open(base) as unchanged:
        np.testing.assert_array_equal(unchanged.read(1), canopy)
    with rasterio.open(virtual.with_suffix(".patch.tif")) as patch:
        assert patch.width <= 5 and patch.height <= 5
    results = []
    transforms = []
    for path in (reference, virtual):
        dataset = gdal.Open(str(path))
        output = gdal.ViewshedGenerate(
            dataset.GetRasterBand(1),
            "MEM",
            "",
            [],
            point.x,
            point.y,
            1.7,
            1.0,
            1.0,
            0.0,
            0.0,
            -1.0,
            0.85714,
            gdal.GVM_Edge,
            1000.0,
            None,
            None,
            gdal.GVOT_NORMAL,
            [],
        )
        results.append(output.ReadAsArray())
        transforms.append(output.GetGeoTransform())
        output = dataset = None
    np.testing.assert_array_equal(*results)
    assert transforms[0] == transforms[1]


def test_private_vrt_rejects_missing_observer_ground(tmp_path):
    pytest.importorskip("osgeo.gdal")
    from viewshed_toolkit.pipeline.prepare.elevation.canopy import isolate_observer_canopy_vrt

    base = _write_raster(tmp_path / "base.tif", np.ones((3, 3)), nodata=-9999, dtype="float32")
    endpoint = _write_raster(
        tmp_path / "ground.tif", np.full((3, 3), -9999), nodata=-9999, dtype="float32"
    )
    point = _observer(1, 1).geometry.iloc[0]
    with pytest.raises(ValueError, match="Observer ground elevation is unavailable"):
        isolate_observer_canopy_vrt(
            base_surface_path=base,
            endpoint_dem_path=endpoint,
            output_path=tmp_path / "virtual.vrt",
            observer_x=point.x,
            observer_y=point.y,
        )
