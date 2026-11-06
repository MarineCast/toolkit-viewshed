"""Map-mask reuse preserves native pixel-centre rasterization and source identity."""

import os
import geopandas as gpd
import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import box

from viewshed_toolkit.pipeline.visualization import static_maps


def test_map_mask_reuse_and_same_path_changes(tmp_path, monkeypatch):
    path = tmp_path / "water.parquet"

    def write(geometry):
        gpd.GeoDataFrame(geometry=[geometry], crs=32610).to_parquet(path)

    write(box(0, 0, 20, 40))
    transform = from_origin(0, 40, 10, 10)
    original = gpd.read_parquet
    reads = []

    def counted(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(gpd, "read_parquet", counted)
    kwargs = dict(cache_dir=tmp_path / "cache")
    first = static_maps._water_mask_for_grid(path, "EPSG:32610", (4, 4), transform, **kwargs)
    again = static_maps._water_mask_for_grid(path, "EPSG:32610", (4, 4), transform, **kwargs)
    np.testing.assert_array_equal(first, again)
    assert len(reads) == 1
    before = path.stat()
    write(box(0, 0, 40, 40))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = static_maps._water_mask_for_grid(path, "EPSG:32610", (4, 4), transform, **kwargs)
    assert len(reads) == 2 and changed.sum() > first.sum()
