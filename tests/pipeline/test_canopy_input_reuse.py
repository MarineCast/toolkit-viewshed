from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from viewshed_toolkit.pipeline.prepare.vegetation.rasters import (
    _canopy_metadata_matches_policy,
)
from viewshed_toolkit.pipeline.prepare.vegetation.sources import (
    _canopy_config,
    existing_canopy_30m_is_valid,
)


def _write_aligned_raster(path: Path, *, nodata: float) -> None:
    values = np.ones((60, 80), dtype="float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-122.81, 48.18, 0.001, 0.001),
        nodata=nodata,
    ) as dst:
        dst.write(values, 1)


def test_aligned_canopy_reuse_uses_model_bbox_not_raw_tile_buffer(
    tmp_path: Path,
) -> None:
    dem_path = tmp_path / "dem.tif"
    chm_path = tmp_path / "chm.tif"
    _write_aligned_raster(dem_path, nodata=-9999.0)
    _write_aligned_raster(chm_path, nodata=255.0)

    model_bbox = (-122.80, 48.13, -122.74, 48.17)
    raw_tile_buffered_bbox = (-122.83, 48.10, -122.71, 48.20)

    assert existing_canopy_30m_is_valid(chm_path, dem_path, model_bbox)
    assert not existing_canopy_30m_is_valid(
        chm_path,
        dem_path,
        raw_tile_buffered_bbox,
    )


def test_canopy_acquisition_uses_nested_paths_and_viewshed_max_resampling() -> None:
    config = _canopy_config(
        {
            "viewshed": {"canopy_resampling": "max"},
            "canopy": {"resolution_m": 10},
            "vegetation_data": {
                "canopy": {
                    "raw_dir": "nested/raw",
                    "tile_manifest_path": "nested/tiles.json",
                    "download_std_layer": True,
                }
            },
        }
    )

    assert config["raw_dir"] == "nested/raw"
    assert config["tile_manifest_path"] == "nested/tiles.json"
    assert config["download_std_layer"] is True
    assert config["resampling"] == "max"


def test_canopy_reuse_requires_matching_resampling_metadata(tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"resampling": "average", "selected_tiles": ["N48W123"]}')

    assert not _canopy_metadata_matches_policy(metadata, resampling="max")
    assert _canopy_metadata_matches_policy(metadata, resampling="average")
