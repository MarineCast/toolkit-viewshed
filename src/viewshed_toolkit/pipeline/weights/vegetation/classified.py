"""Opt-in WorldCover/CHM transmission scenario; not calibrated visibility.

Historical class coefficients apply only to ray lengths within ground-to-CHM
columns. An explicit water-class policy can resolve missing zero-floor canopy
as zero vegetation. Terrain and roofs retain their separate direct-ray model.
"""

import math

import numpy as np
from affine import Affine
from numba import njit

from ..rays import direct_ray_visibility
from .defaults import ESA_WORLDCOVER_CLASS_WEIGHTS

CLASSIFIED_CANOPY_CONTRACT = "worldcover_chm_intersection_transmission_v1"
HEIGHT_BREAKS_M = (2.0, 5.0, 15.0)
HEIGHT_OBSTRUCTION = (0.0, 0.25, 0.65, 1.0)
DEFAULT_BETA_PER_M = 0.002


def class_obstruction(canopy: np.ndarray, landcover: np.ndarray):
    """Historical dimensionless coefficients, retaining unrecognized classes.

    WorldCover water with positive qualified CHM is a conflict, not zero trees.
    Floors do not invent a height: a zero-height column never intersects a ray.
    """
    if (
        canopy.ndim != 2
        or not canopy.size
        or canopy.shape != landcover.shape
        or not np.isfinite(canopy).all()
        or (canopy < 0).any()
        or not np.issubdtype(landcover.dtype, np.integer)
    ):
        raise ValueError("Aligned finite nonnegative canopy and integer WorldCover required")
    height_score = np.select(
        [canopy < value for value in HEIGHT_BREAKS_M],
        HEIGHT_OBSTRUCTION[:-1],
        default=HEIGHT_OBSTRUCTION[-1],
    )
    score = np.zeros(canopy.shape, dtype="float64")
    missing = np.ones(canopy.shape, bool)
    for code, values in ESA_WORLDCOVER_CLASS_WEIGHTS.items():
        selected = landcover == code
        score[selected] = np.maximum(
            height_score[selected] * values["obstruction_multiplier"],
            values["obstruction_floor"],
        )
        missing[selected] = False
    missing |= (landcover == 80) & (canopy > 0)
    return score, missing


@njit(cache=True)
def _below_length(start, end, eye, slope, curve, ceiling):
    """Exact length with eye+slope*d+curve*d² below a constant canopy top."""
    if curve == 0:
        if slope == 0:
            return end - start if eye < ceiling else 0.0
        crossing = (ceiling - eye) / slope
        if slope > 0:
            return max(0.0, min(end, crossing) - start)
        return max(0.0, end - max(start, crossing))
    discriminant = slope * slope - 4 * curve * (eye - ceiling)
    if discriminant <= 0:
        return 0.0
    root = math.sqrt(discriminant)
    lower = (-slope - root) / (2 * curve)
    upper = (-slope + root) / (2 * curve)
    return max(0.0, min(end, upper) - max(start, lower))


@njit(cache=True)
def _path_depth(
    ground,
    canopy,
    missing_height,
    coefficient,
    missing_class,
    clearance,
    baseline,
    ox,
    oy,
    targets,
    origin_x,
    origin_y,
    pixel,
    eye,
    target_height,
    curve,
):
    """Return weighted canopy-intersection metres; negative states stay distinct."""
    output = baseline.astype(np.float64)
    source_col = math.floor((ox - origin_x) / pixel)
    source_row = math.floor((origin_y - oy) / pixel)
    height, width = ground.shape
    for n in range(len(targets)):
        if baseline[n] != 1:
            continue
        tx, ty = targets[n]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        output[n] = 0.0
        if distance == 0:
            continue
        tr = math.floor((origin_y - ty) / pixel)
        tc = math.floor((tx - origin_x) / pixel)
        if missing_height[tr, tc] and not clearance[tr, tc]:
            output[n] = -1
            continue
        slope = (ground[tr, tc] + target_height - eye - curve * distance**2) / distance
        vx, vy = dx / distance, -dy / distance
        col, row = source_col, source_row
        sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
        next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
        next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        delta_x = pixel / abs(vx) if vx != 0 else math.inf
        delta_y = pixel / abs(vy) if vy != 0 else math.inf
        entered, depth, uncertain = 0.0, 0.0, False
        while 0 <= row < height and 0 <= col < width:
            exited = min(next_x, next_y, distance)
            if (
                exited > entered + 1e-9
                and (row != source_row or col != source_col)
                and (row != tr or col != tc)
                and not clearance[row, col]
            ):
                if missing_height[row, col]:
                    uncertain = True
                elif canopy[row, col] > 0:
                    length = _below_length(
                        entered, exited, eye, slope, curve, ground[row, col] + canopy[row, col]
                    )
                    if length > 0:
                        if missing_class[row, col]:
                            uncertain = True
                        else:
                            depth += coefficient[row, col] * length
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
        output[n] = -1 if uncertain else depth
    return output


def classified_ray_support(
    ground: np.ndarray,
    canopy: np.ndarray,
    canopy_unknown: np.ndarray,
    landcover: np.ndarray,
    building_height: np.ndarray,
    building_unknown: np.ndarray,
    *,
    transform: Affine,
    observer_xy: tuple[float, float],
    targets_xy: np.ndarray,
    observer_ground_elevation_m: float,
    beta_per_m: float = DEFAULT_BETA_PER_M,
    water_class_policy: str = "retain_unknown",
    canopy_clearance_mask: np.ndarray | None = None,
    observer_height_m: float = 1.7,
    target_height_m: float = 1.0,
    curvature_coefficient: float = 0.85714,
    earth_radius_m: float = 6378137.0,
    max_distance_m: float = 5000.0,
) -> np.ndarray:
    """Return modeled transmission [0,1], unknown -1, outside range -2.

    0 is resolved terrain/roof blockage; otherwise exp(-beta*weighted canopy
    intersection length). Class coefficients and beta are scenario assumptions.
    Source cell grounding and canopy-only clearance follow direct-ray semantics.
    All inputs are already qualified on one projected grid; no water inference.
    The default preserves missing height/class support. Explicit zero_if_missing
    assumes zero vegetation for WorldCover80 with missing height and zero floor;
    it never clears roofs or positive-height water conflicts. Original arrays are
    preserved. This is a model policy, not an observed canopy height.
    """
    if (
        canopy_unknown.shape != ground.shape
        or canopy_unknown.dtype != np.bool_
        or canopy.shape != ground.shape
        or not math.isfinite(beta_per_m)
        or beta_per_m <= 0
        or water_class_policy not in ("retain_unknown", "zero_if_missing")
    ):
        raise ValueError("Aligned canopy/boolean missing mask and positive finite beta required")
    coefficient, missing_class = class_obstruction(canopy, landcover)
    missing_height = canopy_unknown.copy()
    if water_class_policy == "zero_if_missing":
        missing_height[(landcover == 80) & (canopy == 0)] = False
    if canopy_clearance_mask is None:
        clearance = np.zeros(ground.shape, bool)
    else:
        clearance = canopy_clearance_mask
        if clearance.shape != ground.shape or clearance.dtype != np.bool_:
            raise ValueError("Aligned boolean canopy clearance required")
    targets = np.asarray(targets_xy, dtype="float64")
    baseline = direct_ray_visibility(
        ground,
        building_height,
        building_unknown,
        transform=transform,
        observer_xy=observer_xy,
        targets_xy=targets,
        observer_ground_elevation_m=observer_ground_elevation_m,
        observer_height_m=observer_height_m,
        target_height_m=target_height_m,
        curvature_coefficient=curvature_coefficient,
        earth_radius_m=earth_radius_m,
        max_distance_m=max_distance_m,
    )
    depth = _path_depth(
        ground,
        canopy,
        missing_height,
        coefficient,
        missing_class,
        clearance,
        baseline,
        *observer_xy,
        targets,
        transform.c,
        transform.f,
        transform.a,
        observer_ground_elevation_m + observer_height_m,
        target_height_m,
        curvature_coefficient / (2 * earth_radius_m),
    )
    result = depth.copy()
    visible = baseline == 1
    supported = visible & (depth >= 0)
    result[supported] = np.exp(-beta_per_m * depth[supported])
    return result
