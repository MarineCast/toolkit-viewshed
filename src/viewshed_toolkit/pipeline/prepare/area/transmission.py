"""Deterministic retained-lattice water populations and target quadrature."""

import math
from typing import Any

import numpy as np
from affine import Affine
from shapely import contains_xy

RANGE_M = 24140.16


def target_design(
    rows: Any, cols: Any, codes: Any, stride: Any, *, census_threshold: Any = 0
) -> Any:
    """One actual water point per target/cell-block; weights preserve all pixels."""
    if (
        isinstance(stride, bool)
        or stride < 1
        or not isinstance(stride, int)
        or len(rows) != len(cols)
        or len(rows) != len(codes)
    ):
        raise ValueError("Aligned target rows, columns, codes and positive integer stride required")
    if (
        not isinstance(census_threshold, int)
        or isinstance(census_threshold, bool)
        or census_threshold < 0
    ):
        raise ValueError("Nonnegative integer census threshold required")
    rows, cols, codes = np.asarray(rows), np.asarray(cols), np.asarray(codes)
    counts = np.bincount(codes)
    effective = np.where(counts[codes] < census_threshold, 1, stride)
    rg = rows // effective
    cg = cols // effective
    # At stride 1 or 2 every point in a block has equal centre distance.
    # Canonical populations arrive in row/column order, so a stable packed-key
    # sort also selects the original row/column tie winner. Bound the key using
    # Python integers before doing fixed-width arithmetic; generic callers fall
    # back to the full lexicographic design below.
    ordered = len(rows) < 2 or np.all(
        (rows[1:] > rows[:-1]) | ((rows[1:] == rows[:-1]) & (cols[1:] >= cols[:-1]))
    )
    if (
        stride in (1, 2)
        and len(rows)
        and ordered
        and rg.dtype.kind in "iu"
        and cg.dtype.kind in "iu"
    ):
        rmin, cmin = int(rg.min()), int(cg.min())
        nr, nc = int(rg.max()) - rmin + 1, int(cg.max()) - cmin + 1
        if (int(codes.max()) + 1) * nr * nc <= np.iinfo(np.int64).max:
            key = (codes.astype(np.int64) * nr + (rg - rmin)) * nc + (cg - cmin)
            order = np.argsort(key, kind="stable")
            new = np.ones(len(order), bool)
            new[1:] = key[order[1:]] != key[order[:-1]]
            starts = np.flatnonzero(new)
            return order[starts], np.diff(np.append(starts, len(order)))
    d = ((rows % effective) + 0.5 - effective / 2) ** 2 + (
        (cols % effective) + 0.5 - effective / 2
    ) ** 2
    # Sort groups and their deterministic representative together. The leading
    # code/row-block/column-block order is identical to the original two sorts;
    # distance, row and column still break ties within each group.
    order = np.lexsort((cols, rows, d, cg, rg, codes))
    new = np.ones(len(order), bool)
    if len(order) > 1:
        new[1:] = (
            (codes[order[1:]] != codes[order[:-1]])
            | (rg[order[1:]] != rg[order[:-1]])
            | (cg[order[1:]] != cg[order[:-1]])
        )
    starts = np.flatnonzero(new)
    selected = order[starts]
    weights = np.diff(np.append(starts, len(order)))
    assert weights.sum() == len(rows)
    return selected, weights


def target_population(
    water: Any, transform: Any, grid_shape: Any, observers: Any, *, max_distance_m: Any = RANGE_M
) -> Any:
    """All 30m water centres within range of at least one source observer.

    The lattice extends past the retained obstacle cache. Those targets remain
    explicit missing coverage rather than being dropped from the pair universe.
    """
    if not math.isfinite(max_distance_m) or max_distance_m <= 0:
        raise ValueError("Positive finite target distance required")
    coarse = transform * Affine.scale(3)
    lo = observers[:, :2].min(0) - max_distance_m
    hi = observers[:, :2].max(0) + max_distance_m
    c0 = math.floor((lo[0] - coarse.c) / 30)
    c1 = math.ceil((hi[0] - coarse.c) / 30)
    r0 = math.floor((coarse.f - hi[1]) / 30)
    r1 = math.ceil((coarse.f - lo[1]) / 30)
    rr, cc = np.mgrid[r0:r1, c0:c1]
    x, y = coarse * (cc + 0.5, rr + 0.5)
    selected = contains_xy(water, x, y)
    within = np.zeros(selected.shape, bool)
    for ox, oy, _ in observers:
        within |= np.hypot(x - ox, y - oy) <= max_distance_m
    selected &= within
    targets = np.column_stack((x[selected], y[selected]))
    inside = (
        (targets[:, 0] >= transform.c)
        & (targets[:, 0] < transform.c + grid_shape[1] * 10)
        & (targets[:, 1] <= transform.f)
        & (targets[:, 1] > transform.f - grid_shape[0] * 10)
    )
    return targets, rr[selected], cc[selected], inside


def _nested_grid(grid: Any) -> None:
    if grid.a != 10 or grid.e != -10 or grid.b or grid.d:
        raise ValueError("North-up nested 10m grid required")


def _reach(geometry: Any, radius: float) -> Any:
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Positive finite reach required")
    # Shapely buffers are inscribed. Expand to contain the requested disk.
    return geometry.buffer(radius / math.cos(math.pi / 256), quad_segs=64)


def endpoint_grid(
    original_grid: Any, active: Any, reporting_water: Any, radius: float, *, padding_m: float = 60
) -> Any:
    """Crop to every possible endpoint, retaining the original stride-two phase.

    This changes the extraction window only, never the canonical warp grid or
    full-cell observer polygon. All straight rays lie in the endpoint envelope.
    """
    from shapely.ops import unary_union

    _nested_grid(original_grid)
    if active.is_empty or padding_m < 60 or not math.isfinite(padding_m):
        raise ValueError("Nonempty full source support and at least60m padding required")
    endpoints = unary_union((active, reporting_water.intersection(_reach(active, radius))))
    minx, miny, maxx, maxy = endpoints.bounds
    c0 = math.floor((minx - padding_m - original_grid.c) / 60) * 6
    c1 = math.ceil((maxx + padding_m - original_grid.c) / 60) * 6
    r0 = math.floor((original_grid.f - maxy - padding_m) / 60) * 6
    r1 = math.ceil((original_grid.f - miny + padding_m) / 60) * 6
    return original_grid * Affine.translation(c0, r0), (r1 - r0, c1 - c0)


def required_support_mask(
    grid: Any, grid_shape: Any, active: Any, reporting_water: Any, radius: float
) -> np.ndarray:
    """Conservative support for ray cells and all full-cell observer ground.

    A ray within R of a reporting-water target stays within that target's R
    disk. The60m margin covers intersected pixel footprints, not unknown-value
    substitution. Missing terrain inside this mask must reject execution.
    """
    from shapely.ops import unary_union

    _nested_grid(grid)
    support = unary_union((_reach(reporting_water, radius + 60), active.buffer(60)))
    result = np.empty(grid_shape, dtype=bool)
    cols = np.arange(grid_shape[1])
    for start in range(0, grid_shape[0], 128):
        rows = np.arange(start, min(start + 128, grid_shape[0]))
        x, y = grid * (cols[None, :] + 0.5, rows[:, None] + 0.5)
        result[start : start + len(rows)] = contains_xy(support, x, y)
    return result


class WaterTargetProvider:
    """Immutable canonical30m water centers and numeric H3 labels.

    Construction is row-chunked; retained storage is12 bytes per water center
    (row, column, label), avoiding millions of Python geometry/H3 objects.
    Geometry SHA/CRS and canonical grid are part of the explicit identity.
    """

    def __init__(
        self,
        water: Any,
        canonical_grid: Any,
        *,
        geometry_crs: Any = "EPSG:32610",
        resolution: int = 7,
        chunk_rows: int = 128,
    ) -> None:
        import hashlib

        import h3
        from pyproj import CRS, Transformer

        _nested_grid(canonical_grid)
        if CRS.from_user_input(geometry_crs) != CRS.from_epsg(32610):
            raise ValueError("Reporting-water geometry must be EPSG:32610")
        if water.is_empty or not 0 <= resolution <= 15 or chunk_rows <= 0:
            raise ValueError("Nonempty water, valid H3 resolution and positive chunk size required")
        self.grid = canonical_grid
        self.coarse = canonical_grid * Affine.scale(3)
        self.identity = dict(
            geometry_sha256=hashlib.sha256(water.wkb).hexdigest(),
            geometry_crs="EPSG:32610",
            grid=list(canonical_grid)[:6],
            h3_resolution=resolution,
            population="strict_geometry_contains_30m_centers_v1",
        )
        minx, miny, maxx, maxy = water.bounds
        c0 = math.floor((minx - self.coarse.c) / 30)
        c1 = math.ceil((maxx - self.coarse.c) / 30)
        r0 = math.floor((self.coarse.f - maxy) / 30)
        r1 = math.ceil((self.coarse.f - miny) / 30)
        project = Transformer.from_crs(32610, 4326, always_xy=True)
        parts = []
        labels: dict[str, int] = {}
        cols = np.arange(c0, c1)
        for start in range(r0, r1, chunk_rows):
            rows = np.arange(start, min(start + chunk_rows, r1))
            rr, cc = np.broadcast_arrays(rows[:, None], cols[None, :])
            x, y = self.coarse * (cc + 0.5, rr + 0.5)
            keep = contains_xy(water, x, y)
            lon, lat = project.transform(x[keep], y[keep])
            codes = np.fromiter(
                (
                    labels.setdefault(h3.latlng_to_cell(y, x, resolution), len(labels))
                    for x, y in zip(lon, lat, strict=True)
                ),
                dtype=np.int32,
                count=len(lon),
            )
            parts.append(np.column_stack((rr[keep], cc[keep], codes)).astype(np.int32))
        self.points = np.concatenate(parts) if parts else np.empty((0, 3), np.int32)
        self.cells = np.asarray(list(labels))
        self.points.flags.writeable = False
        self.cells.flags.writeable = False

    def select(
        self,
        observers: Any,
        *,
        original_grid: Any,
        grid_shape: Any,
        max_distance_m: float = RANGE_M,
    ) -> Any:
        """Match target_population order and original-source quadrature phase."""
        _nested_grid(original_grid)
        if not math.isfinite(max_distance_m) or max_distance_m <= 0:
            raise ValueError("Positive finite target distance required")
        if observers.ndim != 2 or observers.shape[1] != 3 or not len(observers):
            raise ValueError("Nonempty observer XYZ array required")
        if not np.isfinite(observers).all():
            raise ValueError("Finite observers required")
        dc = (original_grid.c - self.grid.c) / 30
        dr = (self.grid.f - original_grid.f) / 30
        if not dc.is_integer() or not dr.is_integer():
            raise ValueError("Source grid must share the canonical30m lattice")
        lo, hi = observers[:, :2].min(0) - max_distance_m, observers[:, :2].max(0) + max_distance_m
        coarse = original_grid * Affine.scale(3)
        c0, c1 = math.floor((lo[0] - coarse.c) / 30), math.ceil((hi[0] - coarse.c) / 30)
        r0, r1 = math.floor((coarse.f - hi[1]) / 30), math.ceil((coarse.f - lo[1]) / 30)
        begin, end = np.searchsorted(self.points[:, 0], [r0 + int(dr), r1 + int(dr)])
        strip = self.points[begin:end]
        local_cc = strip[:, 1] - int(dc)
        index = np.flatnonzero((local_cc >= c0) & (local_cc < c1)) + begin
        rr, cc = self.points[index, 0] - int(dr), self.points[index, 1] - int(dc)
        x, y = coarse * (cc + 0.5, rr + 0.5)
        within = np.zeros(len(index), bool)
        pending = np.arange(len(index))
        for ox, oy, _ in observers:
            # A union needs only one successful observer. Retain the original
            # hypot comparison (including its boundary rounding) for every test.
            reached = np.hypot(x[pending] - ox, y[pending] - oy) <= max_distance_m
            within[pending[reached]] = True
            pending = pending[~reached]
            if not len(pending):
                break
        targets = np.column_stack((x[within], y[within]))
        rr, cc, index = rr[within], cc[within], index[within]
        inside = (
            (targets[:, 0] >= original_grid.c)
            & (targets[:, 0] < original_grid.c + grid_shape[1] * 10)
            & (targets[:, 1] <= original_grid.f)
            & (targets[:, 1] > original_grid.f - grid_shape[0] * 10)
        )
        # Group the large population using integer dictionary codes. Only the
        # small output dictionary crosses the original lexical H3 boundary.
        labels = self.points[index, 2]
        dictionary = np.flatnonzero(np.bincount(labels, minlength=len(self.cells)))
        order = np.argsort(self.cells[dictionary])
        remap = np.empty(len(self.cells), dtype=np.intp)
        remap[dictionary[order]] = np.arange(len(order))
        cells, codes = self.cells[dictionary[order]], remap[labels]
        return targets, rr, cc, inside, cells, codes


def validate_terrain_support(ground: Any, support_mask: Any) -> None:
    """Reject every missing required terrain value without changing any array."""
    if ground.ndim != 2 or support_mask.shape != ground.shape or support_mask.dtype != bool:
        raise ValueError("Aligned two-dimensional terrain and boolean support required")
    missing = support_mask & ~np.isfinite(ground)
    if missing.any():
        raise ValueError(f"Missing terrain inside required support: {int(missing.sum())} pixels")
