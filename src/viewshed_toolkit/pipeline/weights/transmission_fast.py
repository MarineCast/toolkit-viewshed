"""One bounded target pool; exact per-ray arithmetic and original full-row reductions."""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from typing import Any

import numpy as np
import rasterio

from . import rays
from .distance.compute import distance_weight_values
from .transmission_fast_kernels import ClearBlockPruner, DepthZeroBlocks
from .transmission_reduce import aggregate_support, reduce_weights
from .vegetation import classified


def calculate_chunked(
    prepared: Any,
    observers: Any,
    targets: Any,
    codes: Any,
    weights: Any,
    cells: Any,
    radius: Any,
    *,
    distance_config: Any = None,
    observer_workers: Any = 4,
    chunks_per_observer: Any = 2,
    pool: Any = None,
    raw_expected: Any = None,
    profile: Any = None,
) -> Any:

    started = time.perf_counter()
    ground = prepared.ground
    pc = prepared.canopy
    g = prepared.transform
    ref = prepared.ray_reference or g
    targets = np.asarray(targets, dtype=np.float64)
    observers = np.asarray(observers, dtype=np.float64)
    # Hoist the original static-grid/coordinate validation. Per-ray support and
    # endpoint checks remain inside the unchanged native kernels.
    for lower, unknown in [
        (prepared.building, prepared.building_unknown),
        (np.zeros((1, 1), np.float32), pc.zero_clearance),
    ]:
        if lower.shape == ground.shape and (not np.isfinite(lower).all() or (lower < 0).any()):
            raise ValueError("Aligned finite terrain/lower heights and boolean unknown required")
        if unknown.shape != ground.shape or unknown.dtype != np.bool_:
            raise ValueError("Aligned finite terrain/lower heights and boolean unknown required")
    if prepared.terrain_support is None:
        finite = np.isfinite(ground).all()
    else:
        finite = np.isfinite(ground[prepared.terrain_support]).all()
    if not finite:
        raise ValueError("Required terrain support must be finite")
    if (
        not all(math.isfinite(v) for v in g)
        or g.a <= 0
        or g.e >= 0
        or g.b
        or g.d
        or not math.isclose(g.a, -g.e)
    ):
        raise ValueError("North-up square projected metre transform required")
    if not np.isfinite(targets).all() or not np.isfinite(observers).all() or radius <= 0:
        raise ValueError("Finite coordinates and valid geometry parameters required")
    if (
        ground.ndim != 2
        or not ground.size
        or observers.ndim != 2
        or observers.shape[1] != 3
        or not len(observers)
        or targets.ndim != 2
        or targets.shape[1] != 2
    ):
        raise ValueError("Nonempty aligned observer and target coordinates required")
    if (
        pc.canopy.shape != ground.shape
        or pc.input_unknown.shape != ground.shape
        or pc.input_unknown.dtype != np.bool_
    ):
        raise ValueError("Aligned canopy inputs required")
    points = np.vstack((observers[:, :2], targets))
    cc = (points[:, 0] - g.c) / g.a
    rr = (g.f - points[:, 1]) / g.a
    if (
        (cc < 0).any()
        or (cc >= ground.shape[1]).any()
        or (rr < 0).any()
        or (rr >= ground.shape[0]).any()
    ):
        raise ValueError("Source and target points must lie inside the grid")
    if ref.a != g.a or ref.e != g.e or ref.b or ref.d:
        raise ValueError("Ray reference must share the extraction pixel lattice")
    for x, y, z in observers[:1]:
        rays.direct_ray_visibility(
            ground,
            prepared.building,
            prepared.building_unknown,
            transform=g,
            observer_xy=(x, y),
            targets_xy=np.empty((0, 2)),
            observer_ground_elevation_m=float(z),
            terrain_support_mask=prepared.terrain_support,
            ray_reference_transform=ref,
            clearance_mask=pc.zero_clearance,
            max_distance_m=radius,
        )
    dr = (ref.f - g.f) / g.a
    dc = (g.c - ref.c) / g.a
    if not dr.is_integer() or not dc.is_integer():
        raise ValueError("Ray reference offset must be integral pixels")
    dr, dc = int(dr), int(dc)
    nobs, nt = len(observers), len(targets)
    roof = np.empty((nobs, nt), np.int8)
    bare = np.empty((nobs, nt), np.int8)
    depth = np.empty((nobs, nt), np.float64)
    zero: Any = np.zeros_like(ground)
    zero.flags.writeable = False
    clearances = []
    for x, y, _elevation in observers:
        clear = np.zeros(ground.shape, bool)
        row, col = rasterio.transform.rowcol(g, x, y)
        r0, r1, c0, c1 = (
            max(0, row - 4),
            min(ground.shape[0], row + 5),
            max(0, col - 4),
            min(ground.shape[1], col + 5),
        )
        r, c = np.mgrid[r0:r1, c0:c1]
        xx, yy = g * (c + 0.5, r + 0.5)
        clear[r0:r1, c0:c1] = np.hypot(xx - x, yy - y) <= 30
        clear[row, col] = True
        clear.flags.writeable = False
        clearances.append(clear)
    surface = ClearBlockPruner(rays._rays)
    canopy = DepthZeroBlocks(classified._path_depth)
    curve = 0.85714 / (2 * 6378137.0)
    jobs: list[Any] = []
    edges = np.linspace(0, nt, chunks_per_observer + 1, dtype=int)
    for i in range(nobs):
        for a, b in pairwise(edges):
            if b > a:
                jobs.append((i, int(a), int(b)))

    def one(job: Any) -> None:
        i, a, b = job
        x, y, elevation = observers[i]
        t = targets[a:b]
        common = (
            x,
            y,
            t,
            ref.c,
            ref.f,
            g.a,
            float(elevation) + 1.7,
            1.0,
            curve,
            radius,
            prepared.terrain_support,
            dr,
            dc,
        )
        r = surface(
            ground, prepared.building, prepared.building_unknown, pc.zero_clearance, *common
        )
        roof[i, a:b] = r
        depth[i, a:b] = canopy(
            ground,
            pc.canopy,
            pc.missing_height,
            pc.coefficient,
            pc.missing_class,
            clearances[i],
            r,
            x,
            y,
            t,
            ref.c,
            ref.f,
            g.a,
            float(elevation) + 1.7,
            1.0,
            curve,
            dr,
            dc,
        )
        bare[i, a:b] = surface(ground, zero, pc.zero_clearance, pc.zero_clearance, *common)

    own = pool is None
    if own:
        pool = ThreadPoolExecutor(max_workers=observer_workers, thread_name_prefix="target-chunks")
    try:
        list(pool.map(one, jobs))
    finally:
        if own:
            pool.shutdown()
    if raw_expected is not None:
        for i, (x, y, _z) in enumerate(observers):
            assert (
                roof[i].tobytes() == raw_expected["rays"][("roof", x, y)].tobytes()
            ), "Roof ray parity"
            assert (
                bare[i].tobytes() == raw_expected["rays"][("bare", x, y)].tobytes()
            ), "Bare ray parity"
            assert (
                depth[i].tobytes() == raw_expected["depth"][(x, y)].tobytes()
            ), "Canopy depth parity"
    # Exponentiate once per full observer row, then perform the exact original
    # whole-matrix reductions in their original row/target order.
    # Raw depth is private and no longer needed after parity assertions.
    # Reuse its owned matrix; whole-row exponentiation/order is unchanged.
    clearances.clear()
    surface.cache.clear()
    surface.roles.clear()
    canopy.cache.clear()
    zero = None
    values = depth
    decay = np.empty(values.shape, np.float64)
    for i, (x, y, _z) in enumerate(observers):
        supported = (roof[i] == 1) & (depth[i] >= 0)
        values[i, supported] = np.exp(-0.002 * depth[i, supported])
        if distance_config is not None:
            decay[i] = distance_weight_values(
                np.hypot(targets[:, 0] - x, targets[:, 1] - y) / 1000,
                distance_config,
                radius / 1000,
            )
    result = aggregate_support(values, codes, weights, len(cells))
    if distance_config is not None:
        result["diagnostics"] = reduce_weights(bare, values, decay, codes, weights, len(cells))
    elapsed = time.perf_counter() - started
    if profile is not None:
        profile.update(prepared.profile)
        profile.update(
            {
                "jobs": len(jobs),
                "chunks_per_observer": chunks_per_observer,
                "observer_workers": observer_workers,
                "scoring_wall_seconds": elapsed,
            }
        )
    return result, elapsed
