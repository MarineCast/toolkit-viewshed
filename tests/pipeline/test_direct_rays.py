import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit import direct_ray_envelope_visibility, direct_ray_visibility
from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_canopy_visibility

GRID = Affine(10, 0, 0, 0, -10, 90)


def run(lower, unknown=None, targets=None, **kwargs):
    return direct_ray_visibility(
        np.zeros((9, 9)),
        lower,
        np.zeros((9, 9), bool) if unknown is None else unknown,
        transform=GRID,
        observer_xy=(45, 45),
        targets_xy=np.array([(85, 45)] if targets is None else targets),
        observer_ground_elevation_m=0,
        curvature_coefficient=0,
        **kwargs,
    )


def test_off_ray_obstacle_does_not_block_and_intersected_obstacle_does():
    lower = np.zeros((9, 9))
    lower[3, 5] = 50
    assert run(lower).tolist() == [1]
    lower[4, 5] = 50
    assert run(lower).tolist() == [0]


def test_unknown_is_retained_but_known_floor_can_prove_blockage():
    lower = np.zeros((9, 9))
    unknown = np.zeros((9, 9), bool)
    unknown[4, 5] = True
    assert run(lower, unknown).tolist() == [-1]
    lower[4, 5] = 20
    assert run(lower, unknown).tolist() == [0]


def test_clearance_is_explicit_and_does_not_mutate_arrays():
    lower = np.zeros((9, 9))
    lower[4, 5] = 20
    clearance = np.zeros((9, 9), bool)
    clearance[4, 5] = True
    assert run(lower, clearance_mask=clearance).tolist() == [1]
    assert lower[4, 5] == 20


def test_diagonal_corner_does_not_expand_neighboring_footprint():
    lower = np.zeros((9, 9))
    lower[4, 5] = 50
    assert run(lower, targets=[(85, 5)]).tolist() == [1]
    lower[5, 5] = 50
    assert run(lower, targets=[(85, 5)]).tolist() == [0]


def test_range_exclusion_empty_population_and_rotation():
    lower = np.zeros((9, 9))
    assert run(lower, max_distance_m=30).tolist() == [-2]
    assert run(lower, targets=np.empty((0, 2))).size == 0
    lower[4, 5] = 10
    targets = [(85, 45), (45, 5), (5, 45), (45, 85)]
    expected = run(lower, targets=targets)
    rotated = run(np.rot90(lower), targets=targets)
    assert np.array_equal(rotated, np.roll(expected, -1))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -1])
def test_bad_heights_fail(bad):
    lower = np.zeros((9, 9))
    lower[0, 0] = bad
    with pytest.raises(ValueError):
        run(lower)


def test_independent_source_ground_does_not_change_target_or_shared_terrain():
    ground = np.zeros((1, 601))
    kwargs = dict(observer_row=0, observer_col=0, pixel_size_m=30)
    old = partial_canopy_visibility(ground, ground, np.zeros_like(ground, bool), **kwargs)
    corrected = partial_canopy_visibility(
        ground,
        ground,
        np.zeros_like(ground, bool),
        observer_ground_elevation_m=0.874050915,
        **kwargs,
    )
    assert old[0, 320] == 0 and corrected[0, 320] == 1
    assert not ground.any()


def test_canopy_envelope_keeps_crossings_uncertain_and_intact_roofs_blocked():
    floor = np.zeros((9, 9))
    ceiling = floor.copy()
    ceiling[4, 5] = 20
    geometry = dict(
        transform=GRID,
        observer_xy=(45, 45),
        targets_xy=np.array([(85, 45), (5, 45)]),
        observer_ground_elevation_m=0,
        curvature_coefficient=0,
    )
    unknown = np.zeros((9, 9), bool)
    assert direct_ray_envelope_visibility(floor, floor, ceiling, unknown, **geometry).tolist() == [
        -1,
        1,
    ]
    floor[4, 5] = 20
    assert direct_ray_envelope_visibility(
        np.zeros((9, 9)), floor, ceiling, unknown, **geometry
    ).tolist() == [0, 1]
    with pytest.raises(ValueError, match="enclose"):
        direct_ray_envelope_visibility(floor, ceiling, np.zeros((9, 9)), unknown, **geometry)


def test_canopy_envelope_preserves_missing_support_and_range():
    ground = np.zeros((9, 9))
    unknown = np.zeros((9, 9), bool)
    unknown[4, 5] = True
    geometry = dict(
        transform=GRID,
        observer_xy=(45, 45),
        targets_xy=np.array([(85, 45)]),
        observer_ground_elevation_m=0,
        curvature_coefficient=0,
    )
    assert direct_ray_envelope_visibility(ground, ground, ground, unknown, **geometry).tolist() == [
        -1
    ]
    assert direct_ray_envelope_visibility(
        ground, ground, ground, unknown, max_distance_m=30, **geometry
    ).tolist() == [-2]


def test_direct_curvature_matches_flat_water_horizon_with_independent_source_ground():
    ground = np.zeros((1, 1201))
    geometry = dict(
        transform=Affine(10, 0, 0, 0, -10, 10),
        observer_xy=(5, 5),
        targets_xy=np.array([(8005, 5), (9505, 5), (11005, 5)]),
        max_distance_m=12000,
    )
    # Analytic refracted flat-water horizon is about 8.9 km for 1.7 m eye +
    # 1 m target; 0.874 m higher source ground moves it to about 10.1 km.
    unknown = np.zeros(ground.shape, bool)
    assert direct_ray_visibility(
        ground, ground, unknown, observer_ground_elevation_m=0, **geometry
    ).tolist() == [1, 0, 0]
    assert direct_ray_visibility(
        ground, ground, unknown, observer_ground_elevation_m=0.874050915, **geometry
    ).tolist() == [1, 1, 0]


def test_envelope_known_terrain_block_survives_missing_canopy():
    ground = np.zeros((9, 9))
    ground[4, 6] = 50
    unknown = np.zeros((9, 9), bool)
    unknown[4, 5] = True
    zero = np.zeros((9, 9))
    result = direct_ray_envelope_visibility(
        ground,
        zero,
        zero,
        unknown,
        transform=GRID,
        observer_xy=(45, 45),
        targets_xy=np.array([(85, 45)]),
        observer_ground_elevation_m=0,
        curvature_coefficient=0,
    )
    assert result.tolist() == [0]


def test_unknown_target_does_not_hide_proven_intervening_blockage():
    lower = np.zeros((9, 9))
    lower[4, 5] = 20
    unknown = np.zeros((9, 9), bool)
    unknown[4, 8] = True
    assert run(lower, unknown).tolist() == [0]
    lower[4, 5] = 0
    assert run(lower, unknown).tolist() == [-1]
