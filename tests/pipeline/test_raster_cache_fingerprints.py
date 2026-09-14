from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from viewshed_toolkit.pipeline.prepare.elevation.acquire import (
    write_dem_source_metadata,
)
from viewshed_toolkit.pipeline.prepare.elevation.terrain import (
    apply_los_dem_nodata_policy,
    clip_raster_to_bounds,
    flatten_water_pixels_to_sea_level,
    rasterize_water_to_match_dem,
    repair_observer_endpoint_nodata,
)
from viewshed_toolkit.pipeline.weights.terrain.los import (
    _gdal_viewshed_fingerprint,
)


def _write_raster(path: Path, values: np.ndarray, *, dtype: str = "float32") -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=dtype,
        crs="EPSG:32610",
        transform=from_origin(500_000.0, 5_400_000.0, 30.0, 30.0),
        nodata=-9999 if dtype == "float32" else 0,
    ) as dst:
        dst.write(values.astype(dtype), 1)
    return path


def _tags(path: Path) -> dict[str, str]:
    with rasterio.open(path) as src:
        return src.tags()


def test_dem_source_metadata_detects_changed_legacy_artifact(tmp_path: Path) -> None:
    dem_path = tmp_path / "dem.tif"
    dem_path.write_bytes(b"first")
    metadata_path = write_dem_source_metadata(
        dem_path,
        bbox_wgs84=(-124.0, 48.0, -123.0, 49.0),
        resolution_m=30,
        provenance_status="legacy_existing_artifact_unverified",
        overwrite=False,
    )

    assert metadata_path.exists()
    dem_path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match its source metadata"):
        write_dem_source_metadata(
            dem_path,
            bbox_wgs84=(-124.0, 48.0, -123.0, 49.0),
            resolution_m=30,
            provenance_status="legacy_existing_artifact_unverified",
            overwrite=False,
        )


def test_batch_dem_clip_invalidates_when_source_raster_changes(tmp_path: Path) -> None:
    source_path = _write_raster(tmp_path / "source.tif", np.ones((5, 5)))
    output_path = tmp_path / "clip.tif"
    bounds = (500_000.0, 5_399_850.0, 500_150.0, 5_400_000.0)

    clip_raster_to_bounds(source_path, output_path, bounds)
    first_fingerprint = _tags(output_path)["cache_fingerprint"]

    _write_raster(source_path, np.full((5, 5), 7.0))
    clip_raster_to_bounds(source_path, output_path, bounds)
    second_fingerprint = _tags(output_path)["cache_fingerprint"]
    with rasterio.open(output_path) as clipped:
        output = clipped.read(1)

    assert first_fingerprint != second_fingerprint
    assert np.all(output == 7.0)


def test_water_mask_invalidates_when_geometry_changes(tmp_path: Path) -> None:
    dem_path = _write_raster(tmp_path / "dem.tif", np.ones((5, 5)))
    output_path = tmp_path / "water.tif"
    first_water = gpd.GeoDataFrame(
        geometry=[box(500_000.0, 5_399_970.0, 500_030.0, 5_400_000.0)],
        crs="EPSG:32610",
    )
    second_water = gpd.GeoDataFrame(
        geometry=[box(500_000.0, 5_399_850.0, 500_150.0, 5_400_000.0)],
        crs="EPSG:32610",
    )

    rasterize_water_to_match_dem(first_water, dem_path, output_path)
    first_fingerprint = _tags(output_path)["cache_fingerprint"]
    rasterize_water_to_match_dem(second_water, dem_path, output_path)
    second_fingerprint = _tags(output_path)["cache_fingerprint"]
    with rasterio.open(output_path) as water:
        water_count = int(np.count_nonzero(water.read(1)))

    assert first_fingerprint != second_fingerprint
    assert water_count > 1


def test_endpoint_cache_invalidates_when_sea_level_changes(tmp_path: Path) -> None:
    dem_path = _write_raster(tmp_path / "dem.tif", np.full((3, 3), 10.0))
    water_values = np.zeros((3, 3), dtype="uint8")
    water_values[1, 1] = 1
    water_path = _write_raster(tmp_path / "water.tif", water_values, dtype="uint8")
    output_path = tmp_path / "endpoint.tif"

    flatten_water_pixels_to_sea_level(dem_path, water_path, output_path, sea_level_m=0.0)
    first_fingerprint = _tags(output_path)["cache_fingerprint"]
    flatten_water_pixels_to_sea_level(dem_path, water_path, output_path, sea_level_m=2.5)
    second_fingerprint = _tags(output_path)["cache_fingerprint"]
    with rasterio.open(output_path) as endpoint:
        output = endpoint.read(1)

    assert first_fingerprint != second_fingerprint
    assert output[1, 1] == 2.5


def test_endpoint_flattens_water_even_when_projected_dem_is_nodata(tmp_path: Path) -> None:
    dem_values = np.full((3, 3), 10.0, dtype="float32")
    dem_values[1, 1] = -9999.0
    dem_path = _write_raster(tmp_path / "dem.tif", dem_values)
    water_values = np.zeros((3, 3), dtype="uint8")
    water_values[1, 1] = 1
    water_path = _write_raster(tmp_path / "water.tif", water_values, dtype="uint8")
    output_path = tmp_path / "endpoint.tif"

    flatten_water_pixels_to_sea_level(dem_path, water_path, output_path, sea_level_m=0.0)

    with rasterio.open(output_path) as endpoint:
        output = endpoint.read(1, masked=True)
        tags = endpoint.tags()

    assert not np.ma.is_masked(output[1, 1])
    assert output[1, 1] == 0.0
    assert tags["algorithm_version"] == "viewshed_water_flattened_endpoint_v3"


def test_los_dem_nodata_is_replaced_by_conservative_opaque_barrier(
    tmp_path: Path,
) -> None:
    values = np.full((3, 3), 10.0, dtype="float32")
    values[1, 1] = -9999.0
    raster_path = _write_raster(tmp_path / "analysis.tif", values)

    diagnostics = apply_los_dem_nodata_policy(
        raster_path,
        policy="opaque_barrier",
        opaque_barrier_height_m=100_000.0,
    )

    with rasterio.open(raster_path) as source:
        result = source.read(1)
        tags = source.tags()
    assert diagnostics["invalid_pixel_count"] == 1
    assert result[1, 1] == 100_000.0
    assert tags["los_dem_nodata_policy"] == "opaque_barrier"


def test_los_dem_nodata_error_policy_fails_closed(tmp_path: Path) -> None:
    values = np.full((3, 3), 10.0, dtype="float32")
    values[1, 1] = -9999.0
    raster_path = _write_raster(tmp_path / "analysis.tif", values)

    with pytest.raises(ValueError, match="contains nodata"):
        apply_los_dem_nodata_policy(
            raster_path,
            policy="error",
            opaque_barrier_height_m=100_000.0,
        )


def test_observer_endpoint_nodata_is_repaired_from_exact_source_sample(
    tmp_path: Path,
) -> None:
    endpoint_values = np.full((3, 3), 10.0, dtype="float32")
    endpoint_values[1, 1] = -9999.0
    endpoint_path = _write_raster(tmp_path / "endpoint.tif", endpoint_values)
    source_values = np.full((3, 3), 10.0, dtype="float32")
    source_values[1, 1] = 42.5
    source_path = _write_raster(tmp_path / "source.tif", source_values)
    observers = gpd.GeoDataFrame(
        {"observer_id": ["observer-1"]},
        geometry=[Point(500_045.0, 5_399_955.0)],
        crs="EPSG:32610",
    )

    diagnostics = repair_observer_endpoint_nodata(
        endpoint_path,
        source_path,
        observers,
    )

    with rasterio.open(endpoint_path) as endpoint:
        output = endpoint.read(1, masked=True)
        tags = endpoint.tags()

    assert output[1, 1] == 42.5
    assert diagnostics["repaired_observer_count"] == 1
    assert diagnostics["repaired_pixel_count"] == 1
    assert diagnostics["exact_source_repaired_observer_count"] == 1
    assert diagnostics["neighborhood_repaired_observer_count"] == 0
    assert tags["observer_endpoint_repair_algorithm_version"] == ("canonical_grid_center_repair_v3")


def test_observer_endpoint_isolated_source_nodata_is_repaired_from_neighbors(
    tmp_path: Path,
) -> None:
    endpoint_values = np.full((3, 3), 10.0, dtype="float32")
    endpoint_values[1, 1] = -9999.0
    endpoint_path = _write_raster(tmp_path / "endpoint.tif", endpoint_values)
    source_values = np.full((3, 3), 10.0, dtype="float32")
    source_values[1, 1] = -9999.0
    source_path = _write_raster(tmp_path / "source.tif", source_values)
    observers = gpd.GeoDataFrame(
        geometry=[Point(500_045.0, 5_399_955.0)],
        crs="EPSG:32610",
    )

    diagnostics = repair_observer_endpoint_nodata(endpoint_path, source_path, observers)

    with rasterio.open(endpoint_path) as endpoint:
        output = endpoint.read(1, masked=True)

    assert output[1, 1] == 10.0
    assert diagnostics["exact_source_repaired_observer_count"] == 0
    assert diagnostics["neighborhood_repaired_observer_count"] == 1


def test_observer_endpoint_nodata_fails_for_broad_source_dem_gap(
    tmp_path: Path,
) -> None:
    endpoint_values = np.full((3, 3), 10.0, dtype="float32")
    endpoint_values[1, 1] = -9999.0
    endpoint_path = _write_raster(tmp_path / "endpoint.tif", endpoint_values)
    source_values = np.full((3, 3), -9999.0, dtype="float32")
    source_values[0, 0] = 10.0
    source_values[0, 1] = 11.0
    source_path = _write_raster(tmp_path / "source.tif", source_values)
    observers = gpd.GeoDataFrame(
        geometry=[Point(500_045.0, 5_399_955.0)],
        crs="EPSG:32610",
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "fewer than three immediate authoritative neighbor pixels are valid",
    ):
        repair_observer_endpoint_nodata(endpoint_path, source_path, observers)

    with rasterio.open(endpoint_path) as endpoint:
        output = endpoint.read(1, masked=True)

    assert np.ma.is_masked(output[1, 1])


def test_gdal_observer_fingerprint_covers_observer_and_dem_identity(tmp_path: Path) -> None:
    dem_path = _write_raster(tmp_path / "dem.tif", np.ones((3, 3)))
    parameters = {
        "observer_x": 500_015.0,
        "observer_y": 5_399_985.0,
        "observer_height_m": 1.7,
        "target_height_m": 1.0,
        "max_distance_m": 15_000.0,
        "curvature_coefficient": 0.85714,
    }

    baseline = _gdal_viewshed_fingerprint(dem_path, **parameters)
    moved = _gdal_viewshed_fingerprint(
        dem_path,
        **{**parameters, "observer_x": parameters["observer_x"] + 30.0},
    )
    _write_raster(dem_path, np.full((3, 3), 4.0))
    changed_dem = _gdal_viewshed_fingerprint(dem_path, **parameters)

    assert baseline != moved
    assert baseline != changed_dem
