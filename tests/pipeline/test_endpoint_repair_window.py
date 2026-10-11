import numpy as np
import rasterio
import pytest
from rasterio.transform import from_origin

from viewshed_toolkit.pipeline.prepare.elevation.terrain import canonical_endpoint_repair
from viewshed_toolkit.pipeline.prepare.elevation.terrain import sample_observer_ground_elevation


def test_observer_ground_repairs_source_void_and_rejects_unresolved_ground(tmp_path):
    profile = dict(
        driver="GTiff",
        width=3,
        height=3,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=from_origin(0, 3, 1, 1),
        nodata=-9999,
    )
    projected = tmp_path / "projected.tif"
    source = tmp_path / "source.tif"
    empty = np.full((3, 3), -9999, dtype="float32")
    with rasterio.open(projected, "w", **profile) as ds:
        ds.write(empty, 1)
    values = empty.copy()
    values[0] = [2, 4, 6]
    with rasterio.open(source, "w", **profile) as ds:
        ds.write(values, 1)
    assert sample_observer_ground_elevation(projected, source, 1.5, 1.5) == 4
    with rasterio.open(source, "r+") as ds:
        ds.write(empty, 1)
    with pytest.raises(ValueError, match="remains unresolved"):
        sample_observer_ground_elevation(projected, source, 1.5, 1.5)
    with pytest.raises(ValueError, match="outside"):
        sample_observer_ground_elevation(projected, source, -1, -1)


def test_tiled_repair_matches_source_center_and_neighbor_rule(tmp_path):
    source = np.arange(25, dtype="float32").reshape(5, 5)
    source[2, 2] = -9999
    profile = dict(
        driver="GTiff",
        width=5,
        height=5,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=from_origin(0, 5, 1, 1),
        nodata=-9999,
    )
    path = tmp_path / "source.tif"
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(source, 1)
    endpoint = tmp_path / "endpoint.tif"
    # Larger endpoint covers outside-source pixels too. Existing water zero stays zero.
    target = np.full((7, 7), -9999, dtype="float32")
    target[1, 1] = 0
    with rasterio.open(
        endpoint, "w", **(profile | dict(width=7, height=7, transform=from_origin(-1, 6, 1, 1)))
    ) as ds:
        ds.write(target, 1)
    counts = canonical_endpoint_repair(endpoint, path)
    expected = np.full((7, 7), -9999, dtype="float32")
    expected[1:6, 1:6] = source
    expected[3, 3] = 12
    with rasterio.open(endpoint) as ds:
        np.testing.assert_array_equal(ds.read(1), expected)
    assert counts == {"exact": 23, "neighborhood": 1}
    assert canonical_endpoint_repair(endpoint, path) == {"exact": 0, "neighborhood": 0}


def test_cache_identity_ignores_json_object_key_order():
    from viewshed_toolkit.pipeline.prepare.elevation.terrain import (
        projected_dem_metadata_matches,
    )

    expected = dict(
        raw_dem_path="dem",
        raw_dem_signature={"sha256": "hash"},
        raw_dem_source_metadata={"path": "source", "checksum": "abc"},
        target_crs="EPSG:32610",
        dem_resolution_m=30,
    )
    actual = expected | {"raw_dem_source_metadata": {"checksum": "abc", "path": "source"}}
    assert projected_dem_metadata_matches(actual, expected)
    actual["raw_dem_source_metadata"]["checksum"] = "changed"
    assert not projected_dem_metadata_matches(actual, expected)
