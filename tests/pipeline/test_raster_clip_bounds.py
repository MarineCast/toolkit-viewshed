from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from viewshed_toolkit._internal.geo.raster import clip_raster_to_bounds


def _write_raster(path: Path) -> np.ndarray:
    values = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=10,
        height=10,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=from_origin(0.0, 10.0, 1.0, 1.0),
    ) as destination:
        destination.write(values, 1)
    return values


def test_clip_intersects_source_extent_without_shifting_transform(tmp_path: Path) -> None:
    source = tmp_path / "source.tif"
    output = tmp_path / "clip.tif"
    values = _write_raster(source)

    clip_raster_to_bounds(source, output, (-5.0, -5.0, 8.0, 8.0))

    with rasterio.open(output) as clipped:
        assert tuple(clipped.bounds) == pytest.approx((0.0, 0.0, 8.0, 8.0))
        assert clipped.transform == from_origin(0.0, 8.0, 1.0, 1.0)
        np.testing.assert_array_equal(clipped.read(1), values[2:, :8])


def test_clip_rejects_non_overlapping_bounds(tmp_path: Path) -> None:
    source = tmp_path / "source.tif"
    output = tmp_path / "clip.tif"
    _write_raster(source)

    with pytest.raises(ValueError, match="do not overlap"):
        clip_raster_to_bounds(source, output, (20.0, 20.0, 30.0, 30.0))

    assert not output.exists()
