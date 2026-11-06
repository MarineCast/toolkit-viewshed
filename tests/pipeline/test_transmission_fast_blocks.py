import math
import numpy as np
import pytest
from viewshed_toolkit.pipeline.weights.transmission_fast_kernels import ClearBlockPruner
from viewshed_toolkit.pipeline.weights.rays import _rays
from viewshed_toolkit.pipeline.prepare.elevation.buildings import (
    rasterize_building_footprints as batched,
)
from viewshed_toolkit.pipeline.prepare.elevation.buildings import (
    rasterize_building_footprints_reference as rasterize_building_footprints,
)
from shapely.geometry import box, Polygon
from affine import Affine


def compare(
    ground,
    targets,
    *,
    curve=0.85714 / (2 * 6378137),
    eye=1.7,
    ht=1.0,
    ox=15.0,
    oy=20465.0,
    lower=None,
    unknown=None,
    support=None,
    dr=0,
    dc=0,
    radius=30000,
):
    shape = ground.shape
    lower = np.zeros(shape, np.float32) if lower is None else lower
    unknown = np.zeros(shape, bool) if unknown is None else unknown
    clear = np.zeros(shape, bool)
    for a in [ground, lower, unknown, clear, targets] + ([] if support is None else [support]):
        a.flags.writeable = False
    args = (
        ground,
        lower,
        unknown,
        clear,
        ox,
        oy,
        targets,
        0.0,
        20480.0,
        10.0,
        eye,
        ht,
        curve,
        radius,
        support,
        dr,
        dc,
    )
    p = ClearBlockPruner(_rays)
    try:
        expected = _rays(*args)
    except ValueError as e:
        with pytest.raises(ValueError, match=str(e)):
            p(*args)
        return None, p
    actual = p(*args)
    np.testing.assert_array_equal(actual, expected)
    # A second call exercises retained immutable extrema.
    np.testing.assert_array_equal(p(*args), expected)
    return actual, p


@pytest.mark.parametrize("curve", [0.0, 0.85714 / (2 * 6378137), 2 * 0.85714 / (2 * 6378137)])
@pytest.mark.parametrize("floor", [-13.8, 0.0, 25.5])
def test_axes_diagonals_block_edges(curve, floor):
    rng = np.random.default_rng(1703)
    ground = np.full((256, 4096), floor, np.float32)
    targets = np.column_stack(
        (rng.uniform(0.01, 40959.99, 1200), rng.uniform(17920.01, 20479.99, 1200))
    )
    targets = np.vstack(
        (
            targets,
            [
                [15, 20465],
                [1280, 19200],
                [2560, 19200],
                [1279.999999, 20465],
                [1280.000001, 20465],
                [40950, 20465],
                [15, 17935],
                [1280, 19200 + 1e-9],
            ],
        )
    )
    compare(ground, targets, curve=curve, eye=floor + 1.7)


@pytest.mark.parametrize("height", [0.0, 1.0, 1.7, 25.0])
def test_horizon_tangencies(height):
    curve = 0.85714 / (2 * 6378137)
    D = (math.sqrt(height) + math.sqrt(1.0)) / math.sqrt(curve)
    ground = np.zeros((128, 8192), np.float32)
    dist = np.array([D - 1.0, D - 0.001, D - 1e-6, D, D + 1e-6, D + 0.001, D + 1.0])
    targets = np.column_stack((15 + dist, np.full(7, 20465.0)))
    compare(ground, targets, curve=curve, eye=height, radius=81900)


def test_crop_offsets_and_reverse_directions():
    ground = np.full((256, 512), -2.0, np.float32)
    rng = np.random.default_rng(30)
    targets = np.column_stack(
        (2000 + rng.uniform(0.1, 5119.9, 300), 20480 - 1000 - rng.uniform(0.1, 2559.9, 300))
    )
    compare(ground, targets, ox=7100, oy=19000, dr=100, dc=200, eye=3)


@pytest.mark.parametrize(
    "where",
    [
        "ridge",
        "nan",
        "unsupported",
        "unknown_roof",
        "roof",
        "unknown_then_blocked",
        "missing_target",
        "near_tangent_ridge",
    ],
)
def test_fail_closed_on_obstruction_and_unknown(where):
    ground = np.zeros((128, 4096), np.float32)
    lower = np.zeros_like(ground)
    unknown = np.zeros(ground.shape, bool)
    support = np.ones(ground.shape, bool)
    targets = np.array([[30000.0, 20465.0], [8000.0, 20465.0], [1280.0, 20465.0]])
    if where == "ridge":
        ground[:, 140:145] = 50
    elif where == "nan":
        ground[1, 140] = np.nan
    elif where == "unsupported":
        support[1, 140] = False
    elif where == "unknown_roof":
        unknown[1, 140] = True
    elif where == "roof":
        lower[1, 140] = 100
    elif where == "unknown_then_blocked":
        unknown[1, 140] = True
        ground[1, 145] = 100
    elif where == "missing_target":
        unknown[1, 800] = True
    else:
        ground[1, 400] = np.float32(1e-7)
    compare(ground, targets, lower=lower, unknown=unknown, support=support)


def test_mutable_fields_use_reference():
    ground = np.zeros((128, 256), np.float32)
    zero = np.zeros_like(ground)
    mask = np.zeros(ground.shape, bool)
    targets = np.array([[2550.0, 20465.0]])
    p = ClearBlockPruner(_rays)
    args = (
        ground,
        zero,
        mask,
        mask,
        15.0,
        20465.0,
        targets,
        0.0,
        20480.0,
        10.0,
        1.7,
        1.0,
        0.0,
        4000.0,
        None,
        0,
        0,
    )
    np.testing.assert_array_equal(p(*args), _rays(*args))
    assert not p.cache


@pytest.mark.parametrize("all_touched", [False, True])
@pytest.mark.parametrize("policy", ["assume_complete", "unverified"])
def test_batched_roofs_holes_overlaps_missing_and_order(all_touched, policy):
    # Source EPSG3857 and target EPSG3857 leave geometry projection exact.
    ring = Polygon(
        [(0, 0), (80, 0), (80, 80), (0, 80)], holes=[[(20, 20), (60, 20), (60, 60), (20, 60)]]
    )
    features = [
        (ring.wkb, 1.25),
        (box(35, 35, 75, 75).wkb, 15.123456789),
        (box(10, 10, 50, 50).wkb, None),
        (box(40, 40, 70, 70).wkb, 0.0),
        (box(1000, 1000, 1100, 1100).wkb, 50.0),
        (box(-10, 0, 10, 50).wkb, 7.0),
    ]
    options = dict(
        shape=(12, 12),
        transform=Affine(10, 0, -20, 0, -10, 100),
        crs=3857,
        all_touched=all_touched,
        inventory_policy=policy,
    )
    a = rasterize_building_footprints(features, **options)
    for sequence in [features, list(reversed(features))]:
        b = batched(sequence, **options)
        for field in ["lower_height", "present", "missing_height", "unknown"]:
            assert getattr(a, field).tobytes() == getattr(b, field).tobytes()
        for field in [
            "footprint_count",
            "missing_height_footprint_count",
            "projected_geometry_repair_count",
            "inventory_policy",
            "rasterization_policy",
        ]:
            assert getattr(a, field) == getattr(b, field)


@pytest.mark.parametrize("batch_size", [1, 2, 3, 4096])
def test_batch_boundaries_preserve_max_and_unknown(batch_size):
    features = [(box(0, 0, 70, 70).wkb, h) for h in [4.25, None, 2.0, 8.125, None, 0.0, 7.5]]
    options = dict(
        shape=(8, 8),
        transform=Affine(10, 0, 0, 0, -10, 80),
        crs=3857,
        all_touched=False,
        inventory_policy="assume_complete",
    )
    a = rasterize_building_footprints(features, **options)
    b = batched(features, **options, batch_size=batch_size)
    for k in ["lower_height", "present", "missing_height", "unknown"]:
        assert getattr(a, k).tobytes() == getattr(b, k).tobytes()
    assert (
        a.footprint_count == b.footprint_count
        and a.missing_height_footprint_count == b.missing_height_footprint_count
    )


from viewshed_toolkit.pipeline.weights.transmission_fast_kernels import DepthZeroBlocks
from viewshed_toolkit.pipeline.weights.vegetation.classified import _path_depth


@pytest.mark.parametrize(
    "mode",
    [
        "zero",
        "zero_coefficient",
        "unknown_height",
        "unknown_class",
        "positive",
        "clearance",
        "zero_range",
    ],
)
def test_canopy_depth_exact_unknown_and_zero_blocks(mode):
    dims = (128, 512)
    ground = np.zeros(dims, np.float32)
    canopy = np.zeros(dims, np.uint8)
    missing = np.zeros(dims, bool)
    coef = np.zeros(dims, np.float64)
    missing_class = np.zeros(dims, bool)
    clear = np.zeros(dims, bool)
    targets = np.array([[15.0, 20465.0], [5115.0, 20465.0], [3000.0, 20455.0]])
    baseline = np.ones(3, np.int8)
    if mode in ["zero_coefficient", "unknown_class", "positive", "clearance"]:
        canopy[:, 140:200] = 25
    if mode == "unknown_height":
        missing[:, 140:200] = True
    if mode == "unknown_class":
        missing_class[:, 140:200] = True
    if mode == "positive":
        coef[:, 140:200] = 0.65
    if mode == "clearance":
        missing[:, 140:200] = True
        clear[:, 140:200] = True
    if mode == "zero_range":
        baseline[:] = [1, -1, -2]
    for a in [ground, canopy, missing, coef, missing_class, clear]:
        a.flags.writeable = False
    args = (
        ground,
        canopy,
        missing,
        coef,
        missing_class,
        clear,
        baseline,
        15.0,
        20465.0,
        targets,
        0.0,
        20480.0,
        10.0,
        1.7,
        1.0,
        0.85714 / (2 * 6378137),
        0,
        0,
    )
    expected = _path_depth(*args)
    p = DepthZeroBlocks(_path_depth)
    assert p(*args).tobytes() == expected.tobytes()
    assert p(*args).tobytes() == expected.tobytes()
