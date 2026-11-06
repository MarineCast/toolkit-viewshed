"""Nested 30m terrain/10m obstruction grid preparation for direct rays."""

import math
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT


def extended_grid(transform: Any, observers: Any, radius: Any) -> Any:
    """Pad the entire observer reach on the original nested 30m/10m lattice."""
    if (
        transform.a != 10
        or transform.e != -10
        or transform.b
        or transform.d
        or not math.isfinite(radius)
        or radius <= 0
        or observers.ndim != 2
        or observers.shape[1] != 3
        or not len(observers)
        or not np.isfinite(observers).all()
    ):
        raise ValueError("Finite observers, positive radius and nested 10m grid required")
    lo, hi = observers[:, :2].min(0) - radius, observers[:, :2].max(0) + radius
    c0 = math.floor((lo[0] - transform.c) / 30) - 1
    c1 = math.ceil((hi[0] - transform.c) / 30) + 1
    r0 = math.floor((transform.f - hi[1]) / 30) - 1
    r1 = math.ceil((transform.f - lo[1]) / 30) + 1
    return transform * Affine.translation(c0 * 3, r0 * 3), ((r1 - r0) * 3, (c1 - c0) * 3)


def align(path: Any, grid_shape: Any, transform: Any, *, nodata: Any, dtype: Any) -> Any:
    with (
        rasterio.open(path) as src,
        WarpedVRT(
            src,
            crs="EPSG:32610",
            transform=transform,
            width=grid_shape[1],
            height=grid_shape[0],
            resampling=Resampling.nearest,
            nodata=nodata,
            dtype=dtype,
            warp_mem_limit=64,
            tolerance=1e-8,
        ) as vrt,
    ):
        validate_native_band(src)
        return vrt.read(1)


def load_classes(assets: Any, grid_shape: Any, transform: Any) -> Any:
    """Categorical alignment with near-exact transforms across tile boundaries."""
    result = np.zeros(grid_shape, dtype="uint8")
    for asset in assets:
        layer = align(asset["path"], grid_shape, transform, nodata=0, dtype="uint8")
        if ((result != 0) & (layer != 0) & (result != layer)).any():
            raise ValueError("Conflicting overlapping WorldCover classifications")
        result[layer != 0] = layer[layer != 0]
    return result


def validate_native_band(source: Any) -> Any:
    """Require decoded heights; unknown metadata is a caller declaration, not certification."""
    if source.count != 1 or source.crs is None:
        raise ValueError("Single-band georeferenced native raster required")
    if source.scales[0] != 1 or source.offsets[0] != 0:
        raise ValueError("Decode native raster scale/offset before transmission preparation")
    units = (
        source.units[0] or source.tags().get("vertical_units") or source.tags().get("height_units")
    )
    if units is not None and units.lower() not in {"m", "metre", "meter", "metres", "meters"}:
        raise ValueError("Native heights must be metres")
