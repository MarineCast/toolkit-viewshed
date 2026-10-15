"""Exact design/coverage invariants for repeated regional preparation."""

import numpy as np
import pytest
from affine import Affine
from shapely.geometry import box

from viewshed_toolkit.pipeline.prepare.area.transmission import WaterTargetProvider, target_design
from viewshed_toolkit.pipeline.weights.vegetation.classified import (
    _compact_class_tables,
    class_obstruction,
)


def original_design(rows, cols, codes, stride, census_threshold):
    counts = np.bincount(codes)
    effective = np.where(counts[codes] < census_threshold, 1, stride)
    rg, cg = rows // effective, cols // effective
    sort = np.lexsort((cg, rg, codes))
    new = np.ones(len(sort), bool)
    new[1:] = (
        (codes[sort[1:]] != codes[sort[:-1]])
        | (rg[sort[1:]] != rg[sort[:-1]])
        | (cg[sort[1:]] != cg[sort[:-1]])
    )
    inverse = np.empty(len(sort), dtype=np.int64)
    inverse[sort] = np.cumsum(new) - 1
    weights = np.bincount(inverse)
    d = ((rows % effective) + 0.5 - effective / 2) ** 2 + (
        (cols % effective) + 0.5 - effective / 2
    ) ** 2
    order = np.lexsort((cols, rows, d, inverse))
    start = np.ones(len(order), bool)
    start[1:] = inverse[order[1:]] != inverse[order[:-1]]
    return order[np.flatnonzero(start)], weights


@pytest.mark.parametrize("stride", [1, 2, 3, 8])
@pytest.mark.parametrize("threshold", [0, 30, 1000])
def test_one_sort_exact_design(stride, threshold):
    rng = np.random.default_rng(782)
    for n in (0, 1, 87, 5000):
        # Negative offsets, duplicate points, distance ties and reordered input.
        rows, cols = rng.integers(-30, 60, size=(2, n))
        codes = rng.integers(0, 10, size=n)
        for ordered in (False, True):
            if ordered:
                order = np.lexsort((cols, rows))
                rows, cols, codes = rows[order], cols[order], codes[order]
            expected = original_design(rows, cols, codes, stride, threshold)
            actual = target_design(rows, cols, codes, stride, census_threshold=threshold)
            for a, b in zip(actual, expected, strict=True):
                np.testing.assert_array_equal(a, b)
            assert actual[1].sum() == n


def test_compact_tables_exhaustive_and_isolated():
    heights, classes = np.indices((256, 256), dtype=np.uint8)
    actual = class_obstruction(heights, classes)
    expected = class_obstruction(heights.astype(np.float64), classes.astype(np.int16))
    for a, b in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(a, b)
    # Returned arrays may be changed without poisoning shared tables.
    actual[0][:] = -1
    actual[1][:] = True
    again = class_obstruction(heights, classes)
    for a, b in zip(again, expected, strict=True):
        np.testing.assert_array_equal(a, b)
    for a in _compact_class_tables():
        assert not a.flags.writeable


@pytest.mark.parametrize("bad", [-1.0, np.nan, np.inf])
def test_generic_height_validation_preserved(bad):
    with pytest.raises(ValueError):
        class_obstruction(np.array([[bad]]), np.array([[10]], dtype=np.uint8))


@pytest.mark.parametrize("reverse", [False, True])
def test_union_reach_preserves_boundary_and_observer_order(reverse):
    grid = Affine(10, 0, 500000, 0, -10, 5300000)
    provider = WaterTargetProvider(box(500000, 5299400, 500600, 5300000), grid)
    xy = np.column_stack(
        provider.coarse * (provider.points[:, 1] + 0.5, provider.points[:, 0] + 0.5)
    )
    observers = np.array([[500015, 5299985, 1.7], [500315, 5299685, 1.7]])
    if reverse:
        observers = observers[::-1]
    for radius in (30.0, np.nextafter(30.0, 0.0), np.nextafter(30.0, np.inf), 300.0, 2000.0):
        expected = np.zeros(len(xy), bool)
        for x, y, _ in observers:
            expected |= np.hypot(xy[:, 0] - x, xy[:, 1] - y) <= radius
        actual = provider.select(
            observers, original_grid=grid, grid_shape=(60, 60), max_distance_m=radius
        )
        np.testing.assert_array_equal(actual[0], xy[expected])


def test_packed_group_overflow_uses_generic_design():
    rows = np.array([-(2**40), 2**40], dtype=np.int64)
    cols = np.array([-(2**40), 2**40], dtype=np.int64)
    codes = np.array([0, 1])
    for a, b in zip(
        target_design(rows, cols, codes, 2), original_design(rows, cols, codes, 2, 0), strict=True
    ):
        np.testing.assert_array_equal(a, b)


@pytest.mark.parametrize("kind", ["list", "float"])
def test_generic_noninteger_or_list_inputs(kind):
    rows = np.array([0, 0, 1, 1, 2, 2])
    cols = np.array([0, 1, 0, 1, 0, 1])
    codes = np.zeros(6, dtype=np.int64)
    if kind == "float":
        rows, cols = rows.astype(float) + 0.1, cols.astype(float) + 0.2
    expected = original_design(rows, cols, codes, 2, 0)
    args = [v.tolist() for v in (rows, cols, codes)] if kind == "list" else (rows, cols, codes)
    for a, b in zip(target_design(*args, 2), expected, strict=True):
        np.testing.assert_array_equal(a, b)
