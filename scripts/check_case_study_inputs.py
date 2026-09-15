"""Count exact raster coverage against the configured generalized land/water mask."""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import Window
from shapely.geometry import box

from viewshed_toolkit import load_app_config
from viewshed_toolkit.pipeline.contracts.components import component_root, write_json


def check_inputs(config: Path) -> Path:
    app = load_app_config(config)
    result = {
        "config_hash": app.config_hash,
        "land_definition": "Natural Earth polygons, pixel-center rasterization",
        "rasters": {},
    }
    for name, path in (("dem", app.paths.regional_dem_path), ("chm", app.paths.canopy_height_path)):
        with rasterio.open(path) as source:
            bounds = rasterio.warp.transform_bounds(source.crs, 4326, *source.bounds)
            land = gpd.read_file(app.paths.land_polygon_path, bbox=bounds)
            land.geometry = land.geometry.make_valid().intersection(box(*bounds))
            land = land.to_crs(source.crs)
            counts = {
                "pixels": 0,
                "land_pixels": 0,
                "missing_land_pixels": 0,
                "water_pixels": 0,
                "missing_water_pixels": 0,
                "observed_zero_land_pixels": 0,
            }
            minimum, maximum = np.inf, -np.inf
            for offset in range(0, source.height, 512):
                window = Window(0, offset, source.width, min(512, source.height - offset))
                values = source.read(1, window=window, masked=True)
                missing = np.ma.getmaskarray(values) | ~np.isfinite(values.data)
                mask = geometry_mask(
                    land.geometry,
                    out_shape=values.shape,
                    transform=source.window_transform(window),
                    invert=True,
                )
                counts["pixels"] += values.size
                counts["land_pixels"] += int(mask.sum())
                counts["water_pixels"] += int((~mask).sum())
                counts["missing_land_pixels"] += int((mask & missing).sum())
                counts["missing_water_pixels"] += int((~mask & missing).sum())
                counts["observed_zero_land_pixels"] += int(
                    (mask & ~missing & (values.data == 0)).sum()
                )
                if (~missing).any():
                    observed = values.data[~missing]
                    minimum = min(minimum, float(observed.min()))
                    maximum = max(maximum, float(observed.max()))
            result["rasters"][name] = {
                **counts,
                "crs": str(source.crs),
                "resolution_m": list(source.res),
                "shape": list(source.shape),
                "missing_land_fraction": counts["missing_land_pixels"] / counts["land_pixels"],
                "observed_minimum_m": minimum if np.isfinite(minimum) else None,
                "observed_maximum_m": maximum if np.isfinite(maximum) else None,
            }
    output = component_root(app) / "analysis" / "input-coverage.json"
    write_json(output, result)
    print(result)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    print(check_inputs(parser.parse_args().config))
