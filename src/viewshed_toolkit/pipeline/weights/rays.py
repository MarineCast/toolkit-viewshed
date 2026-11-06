"""Explicit straight-ray intervals without neighbouring-cell horizon interpolation.

This opt-in research model traverses intersected raster cell interiors. It does
not reproduce GDAL Edge. Heights remain opaque prisms on the supplied grid;
finer native inputs reduce footprint broadening but cannot recover crown porosity.
"""

import math

import numpy as np
from affine import Affine
from numba import njit

DIRECT_RAY_CONTRACT = "direct_grid_ray_interval_v2"
DIRECT_ENVELOPE_CONTRACT = "direct_grid_height_envelope_v2"


@njit(cache=True, nogil=True)
def _rays(
    ground,
    lower,
    unknown,
    clearance,
    ox,
    oy,
    targets,
    origin_x,
    origin_y,
    pixel,
    eye,
    target_height,
    curve,
    radius,
    terrain_support,
    row_offset,
    col_offset,
):
    out = np.full(len(targets), -1, dtype=np.int8)
    height, width = ground.shape
    source_col = math.floor((ox - origin_x) / pixel)
    source_row = math.floor((origin_y - oy) / pixel)
    if (
        terrain_support is not None
        and not terrain_support[source_row - row_offset, source_col - col_offset]
    ):
        raise ValueError("Observer outside declared terrain support")
    for n in range(len(targets)):
        tx, ty = targets[n]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        if distance > radius:
            out[n] = -2  # Range exclusion is distinct from obstruction.
            continue
        tr = math.floor((origin_y - ty) / pixel)
        tc = math.floor((tx - origin_x) / pixel)
        if not math.isfinite(ground[tr - row_offset, tc - col_offset]) or (
            terrain_support is not None and not terrain_support[tr - row_offset, tc - col_offset]
        ):
            raise ValueError("Required target terrain missing or outside support")
        endpoint_unknown = (
            unknown[tr - row_offset, tc - col_offset]
            and not clearance[tr - row_offset, tc - col_offset]
        )
        if distance == 0:
            out[n] = -1 if endpoint_unknown else 1
            continue
        slope = (
            ground[tr - row_offset, tc - col_offset] + target_height - eye - curve * distance**2
        ) / distance
        vx, vy = dx / distance, -dy / distance
        col, row = source_col, source_row
        sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
        next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
        next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        delta_x = pixel / abs(vx) if vx != 0 else math.inf
        delta_y = pixel / abs(vy) if vy != 0 else math.inf
        entered, uncertain, blocked = 0.0, endpoint_unknown, False
        while row_offset <= row < row_offset + height and col_offset <= col < col_offset + width:
            exited = min(next_x, next_y, distance)
            # Central source cell is explicitly grounded. Cell boundaries of
            # zero length do not turn an adjacent off-ray roof into a blocker.
            if exited > entered + 1e-9 and (row != source_row or col != source_col):
                if not math.isfinite(ground[row - row_offset, col - col_offset]) or (
                    terrain_support is not None
                    and not terrain_support[row - row_offset, col - col_offset]
                ):
                    raise ValueError("Required ray terrain missing or outside support")
                h = (
                    0.0
                    if clearance[row - row_offset, col - col_offset]
                    else lower[row - row_offset, col - col_offset]
                )
                if (
                    unknown[row - row_offset, col - col_offset]
                    and not clearance[row - row_offset, col - col_offset]
                ):
                    uncertain = True
                if row != tr or col != tc:
                    d = (
                        min(exited, max(entered, -slope / (2 * curve)))
                        if curve > 0
                        else (entered if slope >= 0 else exited)
                    )
                    if (
                        ground[row - row_offset, col - col_offset]
                        + h
                        - eye
                        - slope * d
                        - curve * d * d
                        > 1e-7
                    ):
                        blocked = True
                        break
            if exited >= distance:
                break
            advance_x, advance_y = next_x <= next_y, next_y <= next_x
            entered = exited
            if advance_x:
                col += sx
                next_x += delta_x
            if advance_y:
                row += sy
                next_y += delta_y
        out[n] = 0 if blocked else -1 if uncertain else 1
    return out


def direct_ray_visibility(
    ground: np.ndarray,
    lower_height: np.ndarray,
    unknown: np.ndarray,
    *,
    transform: Affine,
    observer_xy: tuple[float, float],
    targets_xy: np.ndarray,
    observer_ground_elevation_m: float,
    clearance_mask: np.ndarray | None = None,
    terrain_support_mask: np.ndarray | None = None,
    ray_reference_transform: Affine | None = None,
    observer_height_m: float = 1.7,
    target_height_m: float = 1.0,
    curvature_coefficient: float = 0.85714,
    earth_radius_m: float = 6378137.0,
    max_distance_m: float = 5000.0,
) -> np.ndarray:
    """Return clear1, blocked0, unknown-1, outside-range-2 for explicit targets.

    Caller supplies a north-up square projected metre grid, aligned surfaces,
    complete terrain, nonnegative supported height floors and upper uncertainty.
    Ground is target/intervening terrain; source ground is independently supplied.
    Clearance changes only the supplied obstruction component, so compose cleared
    canopy and intact buildings before passing joint arrays. The source cell is
    grounded explicitly. Native input units, CRS and origin belong in provenance.
    """
    if (
        ground.ndim != 2
        or not ground.size
        or ground.shape != lower_height.shape
        or ground.shape != unknown.shape
        or unknown.dtype != np.bool_
        or not np.isfinite(lower_height).all()
        or (lower_height < 0).any()
    ):
        raise ValueError("Aligned finite terrain/lower heights and boolean unknown required")
    if terrain_support_mask is None:
        finite = np.isfinite(ground).all()
    else:
        if terrain_support_mask.shape != ground.shape or terrain_support_mask.dtype != np.bool_:
            raise ValueError("Aligned boolean terrain support required")
        finite = np.isfinite(ground[terrain_support_mask]).all()
    if not finite:
        raise ValueError("Required terrain support must be finite")
    if (
        not all(math.isfinite(v) for v in transform)
        or transform.a <= 0
        or transform.e >= 0
        or transform.b != 0
        or transform.d != 0
        or not math.isclose(transform.a, -transform.e)
    ):
        raise ValueError("North-up square projected metre transform required")
    targets = np.asarray(targets_xy, dtype=np.float64)
    pars = (
        *observer_xy,
        observer_ground_elevation_m,
        observer_height_m,
        target_height_m,
        curvature_coefficient,
        earth_radius_m,
        max_distance_m,
    )
    if (
        targets.ndim != 2
        or targets.shape[1] != 2
        or not np.isfinite(targets).all()
        or not all(math.isfinite(v) for v in pars)
        or min(observer_height_m, target_height_m, curvature_coefficient) < 0
        or min(earth_radius_m, max_distance_m) <= 0
    ):
        raise ValueError("Finite coordinates and valid geometry parameters required")
    points = np.vstack([observer_xy, targets])
    cols = (points[:, 0] - transform.c) / transform.a
    rows = (transform.f - points[:, 1]) / transform.a
    if (
        (cols < 0).any()
        or (cols >= ground.shape[1]).any()
        or (rows < 0).any()
        or (rows >= ground.shape[0]).any()
    ):
        raise ValueError("Source and target points must lie inside the grid")
    if clearance_mask is None:
        clearance = np.zeros(ground.shape, bool)
    else:
        clearance = clearance_mask
        if clearance.shape != ground.shape or clearance.dtype != np.bool_:
            raise ValueError("Aligned boolean clearance mask required")
    reference = transform if ray_reference_transform is None else ray_reference_transform
    if reference.a != transform.a or reference.e != transform.e or reference.b or reference.d:
        raise ValueError("Ray reference must share the extraction pixel lattice")
    dr = (reference.f - transform.f) / transform.a
    dc = (transform.c - reference.c) / transform.a
    if not math.isfinite(dr) or not math.isfinite(dc) or not dr.is_integer() or not dc.is_integer():
        raise ValueError("Ray reference offset must be integral pixels")
    return _rays(
        ground,
        lower_height,
        unknown,
        clearance,
        *observer_xy,
        targets,
        reference.c,
        reference.f,
        transform.a,
        observer_ground_elevation_m + observer_height_m,
        target_height_m,
        curvature_coefficient / (2 * earth_radius_m),
        max_distance_m,
        terrain_support_mask,
        int(dr),
        int(dc),
    )


def direct_ray_envelope_visibility(
    ground: np.ndarray,
    height_floor: np.ndarray,
    height_ceiling: np.ndarray,
    unknown: np.ndarray,
    **geometry,
) -> np.ndarray:
    """Classify rays across an explicit floor/ceiling obstruction model.

    Clear requires a clear ceiling ray; blocked requires a blocked floor ray.
    Other in-range outcomes remain unknown. Missing upper support remains unknown
    in both evaluations. For canopy-height-only uncertainty, use floor zero and
    the CHM ceiling: a canopy crossing alone cannot establish solid obstruction
    below a crown. Roofs may retain equal modeled floor/ceiling heights. These are
    modeled surfaces, not empirically calibrated height or visibility intervals.
    This function does not infer porosity, probability or an intermediate score.
    """
    if (
        height_ceiling.shape != height_floor.shape
        or not np.isfinite(height_ceiling).all()
        or (height_ceiling < height_floor).any()
    ):
        raise ValueError("Aligned finite height ceiling must enclose the floor")
    floor = direct_ray_visibility(ground, height_floor, unknown, **geometry)
    ceiling = direct_ray_visibility(ground, height_ceiling, unknown, **geometry)
    if np.any((floor == 0) & (ceiling == 1)):
        raise RuntimeError("Height-envelope monotonicity violated")
    return np.where(floor == -2, -2, np.where(floor == 0, 0, np.where(ceiling == 1, 1, -1))).astype(
        np.int8
    )
