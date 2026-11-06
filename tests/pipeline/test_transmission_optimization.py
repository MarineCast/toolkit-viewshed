import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.weights import transmission
from viewshed_toolkit.pipeline.weights.rays import direct_ray_visibility
from viewshed_toolkit.pipeline.weights.vegetation import classified


def fixture():
    shape = (12, 15)
    g = np.zeros(shape, dtype="float32")
    data = dict(
        ground=g,
        canopy=np.full(shape, 4, dtype="float32"),
        canopy_unknown=np.zeros(shape, bool),
        building=np.zeros(shape, dtype="float32"),
        building_unknown=np.zeros(shape, bool),
        building_present=np.zeros(shape, bool),
        qualified_water=np.zeros(shape, bool),
        transform=Affine(10, 0, 0, 0, -10, 120),
    )
    data["canopy_unknown"][3, 6] = True
    data["building"][5, 7] = 9
    classes = np.full(shape, 10, dtype="uint8")
    classes[4, 6] = 0
    classes[2, 6] = 80
    observers = np.array([[15.0, 105.0, 0.0], [15.0, 85.0, 0.0], [35.0, 65.0, 0.0]])
    targets = np.array([[125.0, 105.0], [125.0, 85.0], [125.0, 65.0], [125.0, 25.0]])
    return data, classes, observers, targets


def equal(a, b):
    for key in ("lower", "upper", "unknown", "out_of_range"):
        np.testing.assert_array_equal(a[key], b[key])
    assert a["score"] == b["score"]
    assert a["diagnostics"] == b["diagnostics"]


def test_stationary_once_and_threads_identical(monkeypatch):
    data, classes, observers, targets = fixture()
    calls = []
    original = classified.class_obstruction

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(classified, "class_obstruction", counted)
    prepared = transmission.prepare_calculation(data, classes)
    kwargs = dict(distance_config=DistanceWeightConfig())
    a, _ = transmission.calculate_prepared(
        prepared, observers, targets, np.array([0, 0, 1, 1]), np.ones(4), ["a", "b"], 200, **kwargs
    )
    for workers in (2, 4):
        b, _ = transmission.calculate_prepared(
            prepared,
            observers,
            targets,
            np.array([0, 0, 1, 1]),
            np.ones(4),
            ["a", "b"],
            200,
            observer_workers=workers,
            **kwargs,
        )
        equal(a, b)
    assert len(calls) == 1
    assert not prepared.ground.flags.writeable
    assert not prepared.canopy.coefficient.flags.writeable
    data["ground"][:] = 100
    c, _ = transmission.calculate_prepared(
        prepared, observers, targets, np.array([0, 0, 1, 1]), np.ones(4), ["a", "b"], 200, **kwargs
    )
    equal(a, c)


def test_masked_nan_remains_and_required_cell_rejected():
    ground = np.zeros((5, 5))
    ground[4, 0] = np.nan
    support = np.ones((5, 5), bool)
    support[4, 0] = False
    kwargs = dict(
        transform=Affine(10, 0, 0, 0, -10, 50),
        observer_xy=(5.0, 45.0),
        targets_xy=np.array([[45.0, 45.0]]),
        observer_ground_elevation_m=0,
        max_distance_m=100,
    )
    height = np.zeros_like(ground)
    unknown = np.zeros(ground.shape, bool)
    with pytest.raises(ValueError):
        direct_ray_visibility(ground, height, unknown, **kwargs)
    assert direct_ray_visibility(
        ground, height, unknown, terrain_support_mask=support, **kwargs
    ).tolist() == [1]
    assert np.isnan(ground[4, 0])
    support[0, 2] = False
    with pytest.raises(ValueError, match="Required ray"):
        direct_ray_visibility(ground, height, unknown, terrain_support_mask=support, **kwargs)
    support[0, 2] = True
    ground[0, 2] = np.nan
    with pytest.raises(ValueError, match="Required terrain"):
        direct_ray_visibility(ground, height, unknown, terrain_support_mask=support, **kwargs)


def test_crop_uses_original_ray_reference_exactly():
    rng = np.random.default_rng(4)
    shape = (60, 80)
    original = Affine(10, 0, 432840, 0, -10, 5347260)
    data = dict(
        ground=np.zeros(shape, dtype="float32"),
        canopy=rng.integers(0, 25, shape).astype("float32"),
        canopy_unknown=rng.random(shape) < 0.01,
        building=np.zeros(shape, dtype="float32"),
        building_unknown=rng.random(shape) < 0.01,
        building_present=np.zeros(shape, bool),
        qualified_water=np.zeros(shape, bool),
        transform=original,
    )
    data["building"][25, 40] = 8
    classes = np.full(shape, 10, dtype="uint8")
    observers = np.array([[433015.1, 5347055.2, 0.0], [433005.7, 5346965.3, 0.0]])
    targets = np.array([[433405.8, 5347045.1], [433475.2, 5346915.6]])
    codes, weights, cells = np.array([0, 0]), np.ones(2), ["a"]
    kwargs = dict(distance_config=DistanceWeightConfig())
    expected, _ = transmission.calculate(
        data, classes, observers, targets, codes, weights, cells, 700, **kwargs
    )
    cropped = {
        key: value[6:54, 6:74].copy()
        for key, value in data.items()
        if isinstance(value, np.ndarray)
    }
    cropped["transform"] = original * Affine.translation(6, 6)
    cropped["ray_reference_transform"] = original
    actual, _ = transmission.calculate(
        cropped,
        classes[6:54, 6:74],
        observers,
        targets,
        codes,
        weights,
        cells,
        700,
        observer_workers=2,
        **kwargs,
    )
    equal(expected, actual)
