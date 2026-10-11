import math

import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.weights.vegetation.classified import (
    _below_length,
    class_obstruction,
    classified_ray_support,
)


def inputs():
    ground = np.zeros((7, 7))
    return dict(
        ground=ground,
        canopy=ground.copy(),
        canopy_unknown=np.zeros((7, 7), bool),
        landcover=np.full((7, 7), 10, dtype="uint8"),
        building_height=ground.copy(),
        building_unknown=np.zeros((7, 7), bool),
        transform=Affine(10, 0, 0, 0, -10, 70),
        observer_xy=(15, 35),
        targets_xy=np.array([[55, 35]]),
        observer_ground_elevation_m=0,
        curvature_coefficient=0,
    )


def test_classes_modify_only_intersected_positive_height_columns():
    data = inputs()
    assert classified_ray_support(**data).tolist() == [1]
    data["canopy"][3, 3] = 20
    assert classified_ray_support(**data)[0] == pytest.approx(math.exp(-0.002 * 10))
    data["landcover"][3, 3] = 30
    assert classified_ray_support(**data)[0] == pytest.approx(math.exp(-0.002 * 0.15 * 10))
    data["canopy"][3, 3] = 1  # Entire ray segment is above low vegetation.
    assert classified_ray_support(**data).tolist() == [1]


def test_length_and_beta_change_transmission_without_changing_geometry():
    data = inputs()
    data["canopy"][3, 2:4] = 20
    a = classified_ray_support(**data, beta_per_m=0.002)[0]
    b = classified_ray_support(**data, beta_per_m=0.004)[0]
    assert a == pytest.approx(math.exp(-0.002 * 20))
    assert b == pytest.approx(a * a)
    assert 0 <= b <= a <= 1


def test_missing_height_and_intersected_unknown_class_remain_unknown():
    data = inputs()
    data["canopy_unknown"][3, 3] = True
    assert classified_ray_support(**data).tolist() == [-1]
    data["canopy_unknown"][:] = False
    data["landcover"][3, 3] = 0
    assert classified_ray_support(**data).tolist() == [1]  # Known zero height suffices.
    data["canopy"][3, 3] = 20
    assert classified_ray_support(**data).tolist() == [-1]
    data["canopy"][3, 3] = 1
    assert classified_ray_support(**data).tolist() == [1]  # Ray is above supported height.
    data["canopy_unknown"][3, 5] = True
    assert classified_ray_support(**data).tolist() == [-1]  # Missing target support.


def test_water_height_conflict_is_unknown_when_intersected():
    data = inputs()
    data["landcover"][3, 3] = 80
    data["canopy"][3, 3] = 20
    assert classified_ray_support(**data).tolist() == [-1]
    assert classified_ray_support(**data, water_class_policy="zero_if_missing").tolist() == [-1]


def test_explicit_water_policy_resolves_missing_vegetation_without_clearing_roofs():
    data = inputs()
    data["landcover"][3, 3] = 80
    data["canopy_unknown"][3, 3] = True
    assert classified_ray_support(**data).tolist() == [-1]
    assert classified_ray_support(**data, water_class_policy="zero_if_missing").tolist() == [1]
    assert data["canopy_unknown"][3, 3]
    data["building_height"][3, 3] = 20
    assert classified_ray_support(**data, water_class_policy="zero_if_missing").tolist() == [0]
    data["building_height"][3, 3] = 0
    data["building_unknown"][3, 3] = True
    assert classified_ray_support(**data, water_class_policy="zero_if_missing").tolist() == [-1]


def test_water_policy_never_resolves_missing_land_canopy():
    data = inputs()
    data["canopy_unknown"][3, 3] = True
    for code in (10, 20, 30, 40, 50, 60, 90):
        data["landcover"][3, 3] = code
        assert classified_ray_support(**data, water_class_policy="zero_if_missing").tolist() == [-1]
    with pytest.raises(ValueError):
        classified_ray_support(**data, water_class_policy="ignore")


def test_known_roof_can_resolve_missing_canopy_and_clearance_never_removes_roof():
    data = inputs()
    data["canopy_unknown"][3, 2] = True
    data["building_height"][3, 3] = 20
    assert classified_ray_support(**data).tolist() == [0]
    data["canopy_clearance_mask"] = np.ones((7, 7), bool)
    assert classified_ray_support(**data).tolist() == [0]


def test_clearance_and_grounding_are_private_and_outside_range_is_distinct():
    data = inputs()
    data["canopy"][3, 1] = 20
    data["canopy_unknown"][3, 1] = True
    assert classified_ray_support(**data).tolist() == [1]
    data["canopy"][3, 3] = 20
    data["canopy_clearance_mask"] = np.zeros((7, 7), bool)
    data["canopy_clearance_mask"][3, 3] = True
    copies = {k: v.copy() for k, v in data.items() if isinstance(v, np.ndarray)}
    assert classified_ray_support(**data).tolist() == [1]
    assert classified_ray_support(**data, max_distance_m=30).tolist() == [-2]
    for key, previous in copies.items():
        np.testing.assert_array_equal(data[key], previous)


def test_height_bins_and_class_coefficients_match_declared_historical_scenario():
    height = np.array([[1.99, 2, 5, 15]])
    coefficients, missing = class_obstruction(height, np.full((1, 4), 10, dtype="uint8"))
    np.testing.assert_allclose(coefficients, [[0.1, 0.25, 0.65, 1]])
    assert not missing.any()


@pytest.mark.parametrize(
    "start,end,eye,slope,curve,ceiling,expected",
    [
        (0, 10, 1, 0, 0, 2, 10),
        (0, 10, 2, 0, 0, 1, 0),
        (0, 10, 0, 1, 0, 5, 5),
        (0, 10, 10, -1, 0, 5, 5),
        (0, 10, 0, 0, 1, 25, 5),
        (0, 10, 0, -10, 1, -16, 6),
        (0, 10, 0, 0, 1, 0, 0),
    ],
)
def test_exact_intersection_length(start, end, eye, slope, curve, ceiling, expected):
    assert _below_length(start, end, eye, slope, curve, ceiling) == pytest.approx(expected)


@pytest.mark.parametrize("beta", [0, -1, math.nan, math.inf])
def test_invalid_attenuation_is_rejected(beta):
    with pytest.raises(ValueError):
        classified_ray_support(**inputs(), beta_per_m=beta)


def test_float_classes_are_rejected():
    data = inputs()
    data["landcover"] = data["landcover"].astype(float)
    with pytest.raises(ValueError):
        classified_ray_support(**data)
