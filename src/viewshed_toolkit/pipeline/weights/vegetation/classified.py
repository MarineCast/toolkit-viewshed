"""Opt-in WorldCover/CHM transmission scenario; not calibrated visibility.

Historical class coefficients apply only to ray lengths within ground-to-CHM
columns. An explicit water-class policy can resolve missing zero-floor canopy
as zero vegetation. Terrain and roofs retain their separate direct-ray model.
"""

import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
from affine import Affine
from numba import njit

from ..rays import direct_ray_visibility
from .defaults import ESA_WORLDCOVER_CLASS_WEIGHTS

CLASSIFIED_CANOPY_CONTRACT = "worldcover_chm_intersection_transmission_v2"
HEIGHT_BREAKS_M = (2.0, 5.0, 15.0)
HEIGHT_OBSTRUCTION = (0.0, 0.25, 0.65, 1.0)
DEFAULT_BETA_PER_M = 0.002


@lru_cache(maxsize=1)
def _compact_class_tables():
    """One immutable table for all uint8 height/class combinations (~576 KiB)."""
    height = np.broadcast_to(np.arange(256, dtype=np.float64)[:, None], (256, 256))
    classes = np.broadcast_to(np.arange(256, dtype=np.int16)[None, :], (256, 256))
    # The generic formula is the authority; float heights avoid this fast branch.
    coefficient, missing = class_obstruction(height, classes)
    coefficient.flags.writeable = False
    missing.flags.writeable = False
    return coefficient, missing


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
    if canopy.dtype == np.uint8 and landcover.dtype == np.uint8:
        coefficient, missing = _compact_class_tables()
        return coefficient[canopy, landcover], missing[canopy, landcover]
    # Bin indices are lossless;0.65 and resulting coefficients stay float64.
    height_band = np.zeros(canopy.shape, dtype="uint8")
    for index, threshold in enumerate(HEIGHT_BREAKS_M, start=1):
        height_band[canopy >= threshold] = index
    lookup = np.zeros((256, 4), dtype="float64")
    known = np.zeros(256, bool)
    for code, values in ESA_WORLDCOVER_CLASS_WEIGHTS.items():
        known[code] = True
        for band, weight in enumerate(HEIGHT_OBSTRUCTION):
            lookup[code, band] = max(
                weight * values["obstruction_multiplier"], values["obstruction_floor"]
            )
    outside = (landcover < 0) | (landcover >= 256)
    index = landcover if landcover.dtype == np.uint8 else np.where(outside, 0, landcover)
    score = lookup[index, height_band]
    missing = outside | ~known[index] | ((landcover == 80) & (canopy > 0))
    return score, missing


@dataclass(frozen=True)
class PreparedClassifiedCanopy:
    """Owned, immutable stationary inputs shared across observer rays."""

    canopy: np.ndarray
    landcover: np.ndarray
    input_unknown: np.ndarray
    coefficient: np.ndarray
    missing_class: np.ndarray
    missing_height: np.ndarray
    water_class_policy: str
    zero_clearance: np.ndarray
    class_preparation_seconds: float


def prepare_classified_canopy(
    canopy: Any, canopy_unknown: Any, landcover: Any, water_class_policy: Any
) -> PreparedClassifiedCanopy:
    """Validate/copy once; per-observer clearance remains private."""
    if canopy_unknown.shape != canopy.shape or canopy_unknown.dtype != np.bool_:
        raise ValueError("Aligned boolean canopy missing mask required")
    if water_class_policy not in ("retain_unknown", "zero_if_missing"):
        raise ValueError("Unknown water-class policy")
    owned_canopy = np.array(canopy, copy=True)
    owned_classes = np.array(landcover, copy=True)
    owned_unknown = np.array(canopy_unknown, copy=True)
    started = time.perf_counter()
    coefficient, missing_class = class_obstruction(owned_canopy, owned_classes)
    class_seconds = time.perf_counter() - started
    missing_height = owned_unknown.copy()
    if water_class_policy == "zero_if_missing":
        missing_height[(owned_classes == 80) & (owned_canopy == 0)] = False
    zero_clearance = np.zeros(canopy.shape, bool)
    for array in (
        owned_canopy,
        owned_classes,
        owned_unknown,
        coefficient,
        missing_class,
        missing_height,
        zero_clearance,
    ):
        array.flags.writeable = False
    return PreparedClassifiedCanopy(
        owned_canopy,
        owned_classes,
        owned_unknown,
        coefficient,
        missing_class,
        missing_height,
        water_class_policy,
        zero_clearance,
        class_seconds,
    )


@njit(cache=True, nogil=True)
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


@njit(cache=True, nogil=True)
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
    row_offset,
    col_offset,
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
        if (
            missing_height[tr - row_offset, tc - col_offset]
            and not clearance[tr - row_offset, tc - col_offset]
        ):
            output[n] = -1
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
        entered, depth, uncertain = 0.0, 0.0, False
        while row_offset <= row < row_offset + height and col_offset <= col < col_offset + width:
            exited = min(next_x, next_y, distance)
            if (
                exited > entered + 1e-9
                and (row != source_row or col != source_col)
                and (row != tr or col != tc)
                and not clearance[row - row_offset, col - col_offset]
            ):
                if missing_height[row - row_offset, col - col_offset]:
                    uncertain = True
                elif canopy[row - row_offset, col - col_offset] > 0:
                    length = _below_length(
                        entered,
                        exited,
                        eye,
                        slope,
                        curve,
                        ground[row - row_offset, col - col_offset]
                        + canopy[row - row_offset, col - col_offset],
                    )
                    if length > 0:
                        if missing_class[row - row_offset, col - col_offset]:
                            uncertain = True
                        else:
                            depth += coefficient[row - row_offset, col - col_offset] * length
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
    terrain_support_mask: np.ndarray | None = None,
    ray_reference_transform: Affine | None = None,
    prepared_canopy: PreparedClassifiedCanopy | None = None,
    profile: dict | None = None,
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
    if prepared_canopy is None:
        prepared_canopy = prepare_classified_canopy(
            canopy, canopy_unknown, landcover, water_class_policy
        )
    elif (
        prepared_canopy.canopy is not canopy
        or prepared_canopy.landcover is not landcover
        or prepared_canopy.input_unknown is not canopy_unknown
        or prepared_canopy.water_class_policy != water_class_policy
    ):
        raise ValueError("Prepared canopy must own the supplied immutable inputs and policy")
    coefficient = prepared_canopy.coefficient
    missing_class = prepared_canopy.missing_class
    missing_height = prepared_canopy.missing_height
    if canopy_clearance_mask is None:
        clearance = np.zeros(ground.shape, bool)
    else:
        clearance = canopy_clearance_mask
        if clearance.shape != ground.shape or clearance.dtype != np.bool_:
            raise ValueError("Aligned boolean canopy clearance required")
    targets = np.asarray(targets_xy, dtype="float64")
    baseline_started = time.perf_counter()
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
        terrain_support_mask=terrain_support_mask,
        ray_reference_transform=ray_reference_transform,
        clearance_mask=prepared_canopy.zero_clearance,
    )
    reference = transform if ray_reference_transform is None else ray_reference_transform
    row_offset = round((reference.f - transform.f) / transform.a)
    col_offset = round((transform.c - reference.c) / transform.a)
    depth_started = time.perf_counter()
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
        reference.c,
        reference.f,
        transform.a,
        observer_ground_elevation_m + observer_height_m,
        target_height_m,
        curvature_coefficient / (2 * earth_radius_m),
        row_offset,
        col_offset,
    )
    if profile is not None:
        profile["roof_baseline_seconds"] = depth_started - baseline_started
        profile["canopy_depth_seconds"] = time.perf_counter() - depth_started
    result = depth.copy()
    visible = baseline == 1
    supported = visible & (depth >= 0)
    result[supported] = np.exp(-beta_per_m * depth[supported])
    return result
