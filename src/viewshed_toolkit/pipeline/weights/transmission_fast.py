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
    sampled_subset: Any = None,
    block_jumps: bool = False,
) -> Any:

    started = time.perf_counter()
    ground = prepared.ground
    pc = prepared.canopy
    g = prepared.transform
    ref = prepared.ray_reference or g
    targets = np.asarray(targets, dtype=np.float64)
    observers = np.asarray(observers, dtype=np.float64)
    if sampled_subset is not None:
        selected, sample_weights = (np.asarray(v) for v in sampled_subset)
        if (
            selected.ndim != 1
            or selected.dtype.kind not in "iu"
            or sample_weights.shape != selected.shape
            or (selected < 0).any()
            or (selected >= len(targets)).any()
            or len(np.unique(selected)) != len(selected)
            or not np.isfinite(sample_weights).all()
            or (sample_weights <= 0).any()
        ):
            raise ValueError("Unique in-range sample indices and positive aligned weights required")
        sampled_subset = selected, sample_weights
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
    validation_seconds = time.perf_counter() - started
    setup_started = time.perf_counter()
    nobs, nt = len(observers), len(targets)
    roof = np.empty((nobs, nt), np.int8)
    bare = np.empty((nobs, nt), np.int8)
    depth = np.empty((nobs, nt), np.float64)
    zero: Any = np.zeros_like(ground)
    zero.flags.writeable = False
    clearances = []
    for x, y, _elevation in observers:
        row, col = rasterio.transform.rowcol(g, x, y)
        r0, r1, c0, c1 = (
            max(0, row - 4),
            min(ground.shape[0], row + 5),
            max(0, col - 4),
            min(ground.shape[1], col + 5),
        )
        r, c = np.mgrid[r0:r1, c0:c1]
        xx, yy = g * (c + 0.5, r + 0.5)
        clear = np.hypot(xx - x, yy - y) <= 30
        clear[row - r0, col - c0] = True
        clear.flags.writeable = False
        clearances.append((clear, (r0, c0)))
    # Use the validated 10 m / 30 km numerical envelope. Generic callers outside
    # it retain the incremental fast path, including its native fallbacks.
    jump_eligible = bool(
        block_jumps
        and ground.dtype == np.float32
        and g.a == 10
        and radius <= 30000
        and np.max(np.abs(points)) <= 1e8
        and max(abs(ref.c), abs(ref.f)) <= 1e8
        and np.max(np.abs(observers[:, 2] + 1.7)) <= 1e6
    )
    if jump_eligible:
        from .transmission_jump_kernels import _path_depth_zero, _rays_block_cached

        surface = ClearBlockPruner(rays._rays, kernel=_rays_block_cached)
        canopy = DepthZeroBlocks(classified._path_depth, kernel=_path_depth_zero)
    else:
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
        terrain_blocked = np.zeros(len(t), dtype=bool)
        r = surface(
            ground,
            prepared.building,
            prepared.building_unknown,
            pc.zero_clearance,
            *common,
            terrain_blocked=terrain_blocked,
        )
        roof[i, a:b] = r
        depth[i, a:b] = canopy(
            ground,
            pc.canopy,
            pc.missing_height,
            pc.coefficient,
            pc.missing_class,
            clearances[i][0],
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
            clearance_origin=clearances[i][1],
        )
        # Both surfaces share endpoints, eye height, terrain, curvature, range
        # and zero clearance. Validated nonnegative building heights cannot hide
        # a bare blocker on a roof-visible ray. A certified terrain hit also
        # resolves bare blockage. Other blocked/unknown rays still traverse bare
        # terrain; range exclusions share identical geometry.
        resolved = (r == 1) | (r == -2) | terrain_blocked
        bare[i, a:b] = r
        remaining = ~resolved
        if remaining.any():
            bare_common = (*common[:2], t[remaining], *common[3:])
            bare[i, a:b][remaining] = surface(
                ground, zero, pc.zero_clearance, pc.zero_clearance, *bare_common
            )

    setup_seconds = time.perf_counter() - setup_started
    traversal_started = time.perf_counter()
    own = pool is None
    if own:
        pool = ThreadPoolExecutor(max_workers=observer_workers, thread_name_prefix="target-chunks")
    try:
        list(pool.map(one, jobs))
    finally:
        if own:
            pool.shutdown()
    traversal_seconds = time.perf_counter() - traversal_started
    parity_started = time.perf_counter()
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
    parity_seconds = time.perf_counter() - parity_started
    reduction_started = time.perf_counter()
    # Exponentiate once per full observer row, then perform the exact original
    # whole-matrix reductions in their original row/target order.
    # Raw depth is private and no longer needed after parity assertions.
    # Reuse its owned matrix; whole-row exponentiation/order is unchanged.
    clearance_bytes = sum(p.nbytes for p, _origin in clearances)
    clearances.clear()
    surface.cache.clear()
    surface.roles.clear()
    canopy.cache.clear()
    zero = None
    sample_result = None
    sample_seconds = 0.0
    if sampled_subset is not None:
        selected, sample_weights = sampled_subset
        sample_started = time.perf_counter()
        # Select raw depth before exponentiation to preserve the original sample
        # row's arithmetic, SIMD tails and ordered reductions exactly.
        sample_result = _reduce_raw(
            roof[:, selected],
            bare[:, selected],
            depth[:, selected].copy(),
            observers,
            targets[selected],
            np.asarray(codes)[selected],
            sample_weights,
            cells,
            radius,
            distance_config,
        )
        sample_seconds = time.perf_counter() - sample_started
    result = _reduce_raw(
        roof, bare, depth, observers, targets, codes, weights, cells, radius, distance_config
    )
    if sample_result is not None:
        result["sampled_result"] = sample_result
        result["sampled_reduction_seconds"] = sample_seconds
    elapsed = time.perf_counter() - started
    if profile is not None:
        profile.update(prepared.profile)
        profile.update(
            {
                "validation_seconds": validation_seconds,
                "clearance_and_dispatch_seconds": setup_seconds,
                "traversal_seconds": traversal_seconds,
                "raw_parity_seconds": parity_seconds,
                "reduction_seconds": time.perf_counter() - reduction_started,
                "clearance_bytes": clearance_bytes,
                "jobs": len(jobs),
                "block_jumps": jump_eligible,
                "chunks_per_observer": chunks_per_observer,
                "observer_workers": observer_workers,
                "scoring_wall_seconds": elapsed,
            }
        )
    return result, elapsed


def _reduce_raw(
    roof: Any,
    bare: Any,
    depth: Any,
    observers: Any,
    targets: Any,
    codes: Any,
    weights: Any,
    cells: Any,
    radius: Any,
    distance_config: Any,
) -> Any:
    """Consume private raw depth using unchanged full-row numerical operations."""
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
    return result
