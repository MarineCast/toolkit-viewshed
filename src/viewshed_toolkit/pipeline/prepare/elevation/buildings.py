"""Bounded footprint rasterization for the opt-in above-ground research workflow."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
from affine import Affine
from numpy.typing import NDArray
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds
from rasterio.windows import transform as window_transform
from shapely import from_wkb, get_coordinates, make_valid
from shapely.geometry import box
from shapely.ops import transform as project_geometry

BUILDING_RASTER_CONTRACT = "gba_all_touched_max_agl_v1"


@dataclass(frozen=True)
class BuildingSurface:
    lower_height: NDArray[np.float32]
    present: NDArray[np.bool_]
    missing_height: NDArray[np.bool_]
    unknown: NDArray[np.bool_]
    footprint_count: int
    missing_height_footprint_count: int
    projected_geometry_repair_count: int


def rasterize_building_footprints(
    footprints: Iterable[tuple[bytes, float | None]],
    *,
    shape: tuple[int, int],
    transform: Affine,
    crs: CRS | str,
) -> BuildingSurface:
    """Project EPSG:3857 WKB polygons onto one caller-owned projected metre grid.

    Burn all touched pixels at maximum overlapping valid predicted AGL height,
    represented in the retained float32 raster model (not empirical confidence limits),
    independently of feature order. A missing-height footprint retains unknown
    upper support even under a valid overlapping roof. No footprint is unknown,
    never verified absence. Water qualification belongs to later composition.
    Features stream one at a time; rasterization allocates only each clipped window.
    Caller must qualify source subset containment and retain acquisition provenance.
    No acquisition, source geometry clipping, observer clearance or LOS is performed.
    """
    target_crs = CRS.from_user_input(crs)
    if (
        len(shape) != 2
        or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in shape)
        or not target_crs.is_projected
        or target_crs.linear_units not in ("metre", "meter")
        or not all(math.isfinite(v) for v in transform)
        or transform.a <= 0
        or transform.e >= 0
        or transform.b != 0
        or transform.d != 0
        or not math.isclose(transform.a, -transform.e)
    ):
        raise ValueError("Nonempty north-up square projected metre grid required")
    lower = np.zeros(shape, dtype=np.float32)
    present = np.zeros(shape, dtype=np.bool_)
    missing = np.zeros(shape, dtype=np.bool_)
    extent = box(
        transform.c,
        transform.f + shape[0] * transform.e,
        transform.c + shape[1] * transform.a,
        transform.f,
    )
    projector = Transformer.from_crs("EPSG:3857", target_crs, always_xy=True)
    count = missing_count = repairs = 0
    for wkb, height in footprints:
        if height is not None and (
            isinstance(height, bool)
            or not isinstance(height, (int, float))
            or not math.isfinite(height)
            or height < 0
            or height > np.finfo(np.float32).max
        ):
            raise ValueError("Finite nonnegative predicted AGL height or None required")
        geometry = from_wkb(wkb)
        if geometry.geom_type not in ("Polygon", "MultiPolygon") or geometry.is_empty:
            raise ValueError("Nonempty polygonal EPSG:3857 footprint required")
        if not np.isfinite(get_coordinates(geometry)).all():
            raise ValueError("Finite footprint coordinates required")
        geometry = project_geometry(projector.transform, geometry)
        if not geometry.intersects(extent):
            continue
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            repairs += 1
        if not geometry.is_valid or geometry.is_empty or geometry.area <= 0:
            raise ValueError("Projected footprint cannot be qualified")
        if geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError("Projected footprint repair is not polygonal")
        window = from_bounds(*geometry.bounds, transform=transform)
        r0 = max(0, math.floor(window.row_off) - 1)
        c0 = max(0, math.floor(window.col_off) - 1)
        r1 = min(shape[0], math.ceil(window.row_off + window.height) + 1)
        c1 = min(shape[1], math.ceil(window.col_off + window.width) + 1)
        if r1 <= r0 or c1 <= c0:
            continue
        burn = rasterize(
            [(geometry, 1)],
            out_shape=(r1 - r0, c1 - c0),
            transform=window_transform(Window(c0, r0, c1 - c0, r1 - r0), transform),
            all_touched=True,
            dtype="uint8",
        ).astype(np.bool_)
        present[r0:r1, c0:c1] |= burn
        if height is None:
            missing[r0:r1, c0:c1] |= burn
            missing_count += 1
        else:
            view = lower[r0:r1, c0:c1]
            np.maximum(view, np.where(burn, height, 0), out=view)
        count += 1
    return BuildingSurface(
        lower, present, missing, ~present | missing, count, missing_count, repairs
    )
