"""Unknown-preserving GDAL edge-model canopy intervals for bounded research runs.

Not a production finalizer input. Unknown nonnegative canopy has lower height zero
and unbounded upper height; zero is a bound, never an imputed observation.
"""

from __future__ import annotations

import numpy as np
from numba import njit

PARTIAL_CANOPY_CONTRACT = "canopy_edge_interval_v1"


@njit(cache=True)
def _quadrant(ground, canopy, unknown, eye, pixel, curve, radius, target):
    ny, nx = ground.shape
    horizon = np.empty((ny, nx), dtype=np.float64)
    uncertain = np.zeros((ny, nx), dtype=np.bool_)
    out = np.full((ny, nx), -1, dtype=np.int8)
    for j in range(ny):
        for i in range(nx):
            r2 = (i * pixel) ** 2 + (j * pixel) ** 2
            z = float(ground[j, i]) - eye - curve * r2
            obstacle = z + (0.0 if unknown[j, i] else canopy[j, i])
            p = -np.inf
            u = False
            if max(i, j) > 1:
                if j == 0:
                    p = horizon[j, i - 1] * i / (i - 1)
                    u = uncertain[j, i - 1]
                elif i == 0:
                    p = horizon[j - 1, i] * j / (j - 1)
                    u = uncertain[j - 1, i]
                elif i == j:
                    p = horizon[j - 1, i - 1] * i / (i - 1)
                    u = uncertain[j - 1, i - 1]
                elif i > j:
                    p = (horizon[j - 1, i - 1] * j + horizon[j, i - 1] * (i - j)) / (i - 1)
                    u = uncertain[j - 1, i - 1] or uncertain[j, i - 1]
                else:
                    p = (horizon[j - 1, i - 1] * i + horizon[j - 1, i] * (j - i)) / (j - 1)
                    u = uncertain[j - 1, i - 1] or uncertain[j - 1, i]
            horizon[j, i] = max(obstacle, p)
            uncertain[j, i] = u or unknown[j, i]
            if r2 > radius * radius:
                out[j, i] = 0
            elif z + target < p:
                out[j, i] = 0
            elif not u:
                out[j, i] = 1
    return out


def partial_canopy_visibility(
    ground: np.ndarray,
    canopy: np.ndarray,
    unknown: np.ndarray,
    *,
    observer_row: int,
    observer_col: int,
    pixel_size_m: float,
    observer_height_m: float = 1.7,
    target_height_m: float = 1.0,
    curvature_coefficient: float = 0.85714,
    earth_radius_m: float = 6378137.0,
    max_distance_m: float = 30000.0,
    clearance_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Return -1 unknown, 0 blocked/out-of-range, 1 visible at ground targets.

    All arrays must already share one projected square grid. Caller qualifies
    water and native-subpixel validity before resampling. Endpoint terrain must
    be complete. An explicit clearance mask applies only to this observer, and
    must include its cell; otherwise unknown observer canopy invalidates in-range
    results. Known canopy is nonnegative. Curvature uses cell-centre distances.
    """
    if (
        ground.ndim != 2
        or not ground.size
        or ground.shape != canopy.shape
        or ground.shape != unknown.shape
    ):
        raise ValueError("Nonempty aligned 2D arrays required")
    if unknown.dtype != np.bool_ or not np.all(np.isfinite(ground)):
        raise ValueError("Boolean unknown mask and finite ground required")
    if not np.all(np.isfinite(canopy[~unknown])) or np.any(canopy[~unknown] < 0):
        raise ValueError("Known canopy must be finite and nonnegative")
    pars = (
        pixel_size_m,
        observer_height_m,
        target_height_m,
        curvature_coefficient,
        earth_radius_m,
        max_distance_m,
    )
    if (
        not all(np.isfinite(p) for p in pars)
        or min(pixel_size_m, earth_radius_m, max_distance_m) <= 0
        or min(observer_height_m, target_height_m, curvature_coefficient) < 0
    ):
        raise ValueError("Invalid geometry parameters")
    oy, ox = observer_row, observer_col
    if not (0 <= oy < ground.shape[0] and 0 <= ox < ground.shape[1]):
        raise ValueError("Observer outside grid")
    canopy = canopy.copy()
    unknown = unknown.copy()
    if clearance_mask is not None:
        if (
            clearance_mask.shape != ground.shape
            or clearance_mask.dtype != np.bool_
            or not clearance_mask[oy, ox]
        ):
            raise ValueError("Clearance must be aligned boolean and include observer")
        canopy[clearance_mask] = 0
        unknown[clearance_mask] = False
    if unknown[oy, ox]:
        yy, xx = np.indices(ground.shape)
        return np.where(
            ((yy - oy) ** 2 + (xx - ox) ** 2) * pixel_size_m**2 > max_distance_m**2, 0, -1
        ).astype(np.int8)
    if canopy[oy, ox] != 0:
        raise ValueError("Grounded observer requires explicit clearance")
    result = np.empty(ground.shape, dtype=np.int8)
    for sy in (-1, 1):
        rows = np.arange(oy, -1 if sy < 0 else ground.shape[0], sy)
        for sx in (-1, 1):
            cols = np.arange(ox, -1 if sx < 0 else ground.shape[1], sx)
            idx = np.ix_(rows, cols)
            result[idx] = _quadrant(
                ground[idx],
                canopy[idx],
                unknown[idx],
                float(ground[oy, ox]) + observer_height_m,
                pixel_size_m,
                curvature_coefficient / (2 * earth_radius_m),
                max_distance_m,
                target_height_m,
            )
    return result


def partial_kernel_summary(
    *,
    known_weight_sum: float,
    unknown_weight_sum: float,
    observer_count: int,
    equivalent_water_pixels: float,
    bare_kernel: float,
) -> dict[str, float | str | None]:
    """Compose one complete candidate pair without renormalizing partial support.

    Caller reduces quantized nonnegative per-observation weights using the full
    fixed observer and target-water population, including zero contributions.
    Bounds on an unknown result are not point estimates or available-value sums.
    """
    values = (known_weight_sum, unknown_weight_sum, equivalent_water_pixels, bare_kernel)
    if not all(np.isfinite(v) for v in values) or min(values) < 0:
        raise ValueError("Finite nonnegative kernel inputs required")
    if (
        observer_count < 1
        or int(observer_count) != observer_count
        or equivalent_water_pixels <= 0
        or bare_kernel > 1
    ):
        raise ValueError("Positive immutable denominator and bounded baseline required")
    denominator = observer_count * equivalent_water_pixels
    lower = min(known_weight_sum / denominator, bare_kernel)
    upper = min((known_weight_sum + unknown_weight_sum) / denominator, bare_kernel)
    combined = lower if lower == upper else None
    return {
        "combined_lower": lower,
        "combined_upper": upper,
        "weight_combined": combined,
        "weight_vegetation": (
            combined / bare_kernel if combined is not None and bare_kernel > 0 else None
        ),
        "vegetation_status": (
            "no_baseline_support_neutral"
            if bare_kernel == 0
            else "partial_unknown_paths" if combined is None else "computed_canopy_model"
        ),
        "combined_status": "partial_unknown_paths" if combined is None else "computed_canopy_model",
    }
