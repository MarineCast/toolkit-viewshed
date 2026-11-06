from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from viewshed_toolkit.pipeline.weights.terrain import pixel_index


@pytest.mark.parametrize("bad_code", [-2, 2])
@pytest.mark.parametrize("bad_pixel_is_water", [False, True])
def test_canonical_window_rejects_invalid_codes_before_any_masking(
    tmp_path: Path, monkeypatch, bad_code, bad_pixel_is_water
) -> None:
    codes = np.array([[0, 1], [-1, bad_code]], dtype="int32")
    path = tmp_path / "codes.tif"
    transform = from_origin(500_000, 5_400_000, 30, 30)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="int32",
        crs="EPSG:32610",
        transform=transform,
        nodata=-1,
    ) as destination:
        destination.write(codes, 1)
    artifact = pixel_index.CanonicalPixelH3Artifact(
        path, tmp_path / "lookup.parquet", ("target-a", "target-b"), "fixture"
    )
    monkeypatch.setattr(pixel_index, "ensure_canonical_pixel_h3_artifact", lambda *_args: artifact)
    mask = np.ones((2, 2), dtype=bool)
    mask[1, 1] = bad_pixel_is_water
    with pytest.raises(ValueError, match="invalid target code"):
        pixel_index.canonical_pixel_h3_window(
            None,
            tmp_path / "unused-water.tif",
            batch_transform=transform,
            batch_crs="EPSG:32610",
            batch_water_mask=mask,
        )
    with rasterio.open(path) as source:
        np.testing.assert_array_equal(source.read(1), codes)


@pytest.mark.parametrize("shape", [(2,), (1, 2, 2), (2, 3)])
def test_pixel_index_requires_exact_two_dimensional_reference_shape(shape):
    index = pixel_index.WaterPixelH3Index(np.zeros(shape, dtype="int32"), ("target",))
    with pytest.raises(ValueError, match="shape does not match"):
        pixel_index.validate_pixel_h3_index(index, (2, 2))


def test_pixel_index_accepts_sentinel_and_lookup_boundaries_without_mutation():
    codes = np.array([[-1, 0], [1, -1]], dtype="int32")
    codes.flags.writeable = False
    index = pixel_index.WaterPixelH3Index(codes, ("target-a", "target-b"))
    pixel_index.validate_pixel_h3_index(index, (2, 2))
    assert index.code_grid is codes
    np.testing.assert_array_equal(codes, [[-1, 0], [1, -1]])
    pixel_index.validate_pixel_h3_index(
        pixel_index.WaterPixelH3Index(np.full((2, 2), -1, dtype="int32"), ()), (2, 2)
    )


def test_pixel_index_rejects_non_integer_codes():
    index = pixel_index.WaterPixelH3Index(np.array([[0.5]], dtype="float64"), ("target",))
    with pytest.raises(ValueError, match="integer target codes"):
        pixel_index.validate_pixel_h3_index(index, (1, 1))
