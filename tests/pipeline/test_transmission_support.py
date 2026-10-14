"""Exact population, stride phase and conservative support regressions."""

import h3
import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Point, box

from viewshed_toolkit.pipeline.prepare.area.transmission import (
    WaterTargetProvider,
    endpoint_grid,
    required_support_mask,
    target_design,
    target_population,
)


@pytest.mark.parametrize("offset", [(0, 0), (3, 9), (-9, -3), (6, -12)])
def test_provider_exact_population_and_phase(offset):
    grid = Affine(10, 0, 500000, 0, -10, 5300000)
    original = grid * Affine.translation(*offset)
    water = box(499000, 5299000, 501000, 5301000).difference(box(500010, 5299700, 500210, 5300100))
    observers = np.array([[500005.0, 5300005.0, 1.7], [500025.0, 5299925.0, 1.7]])
    provider = WaterTargetProvider(water, grid, chunk_rows=7)
    expected = target_population(water, original, (120, 120), observers, max_distance_m=620)
    actual = provider.select(
        observers, original_grid=original, grid_shape=(120, 120), max_distance_m=620
    )
    for a, b in zip(actual[:4], expected, strict=True):
        np.testing.assert_array_equal(a, b)
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(*expected[0].T)
    cells, codes = np.unique(
        [h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)], return_inverse=True
    )
    np.testing.assert_array_equal(actual[4], cells)
    np.testing.assert_array_equal(actual[5], codes)
    for stride in (1, 2, 3):
        a = target_design(actual[1], actual[2], actual[5], stride, census_threshold=20)
        b = target_design(expected[1], expected[2], codes, stride, census_threshold=20)
        for x, y in zip(a, b, strict=True):
            np.testing.assert_array_equal(x, y)


def test_endpoint_envelope_retains_phase_and_full_observer_support():
    original = Affine(10, 0, -810, 0, -10, 810)
    active = box(11, 13, 53, 67)
    water = box(100, -200, 150, 200)
    cropped, shape = endpoint_grid(original, active, water, 300)
    dc, dr = (~original) * (cropped.c, cropped.f)
    assert dc % 6 == dr % 6 == 0
    assert shape[0] % 6 == shape[1] % 6 == 0
    assert cropped.c <= active.bounds[0] - 60
    assert cropped.f >= active.bounds[3] + 60
    assert cropped.c + shape[1] * 10 >= water.bounds[2] + 60
    assert shape[0] * shape[1] < 162 * 162


def test_support_includes_all_ray_cells_and_full_source_but_excludes_irrelevant_gap():
    grid = Affine(10, 0, -1000, 0, -10, 1000)
    active = box(-45, -45, 45, 45)
    water = Point(200, 0).buffer(20)
    mask = required_support_mask(grid, (200, 200), active, water, 250)
    for x, y in [(0, 0), (100, 0), (200, 0), (440, 0), (-40, 40)]:
        col, row = (~grid) * (x, y)
        assert mask[int(row), int(col)]
    assert not mask[0, 0]
    ground = np.ones(mask.shape)
    ground[0, 0] = np.nan
    assert np.isfinite(ground[mask]).all()
    ground[100, 100] = np.nan
    assert not np.isfinite(ground[mask]).all()


def test_provider_rejects_crs_and_lattice_mismatch():
    grid = Affine(10, 0, 500000, 0, -10, 5300000)
    water = box(500000, 5299900, 500100, 5300000)
    with pytest.raises(ValueError, match="EPSG"):
        WaterTargetProvider(water, grid, geometry_crs=4326)
    provider = WaterTargetProvider(water, grid)
    with pytest.raises(ValueError, match="lattice"):
        provider.select(
            np.array([[500005, 5299995, 1.7]]),
            original_grid=grid * Affine.translation(1, 0),
            grid_shape=(30, 30),
        )
    assert not provider.points.flags.writeable


def test_scoped_finite_guard_never_substitutes_missing_terrain():
    from viewshed_toolkit.pipeline.prepare.area.transmission import validate_terrain_support

    ground = np.array([[np.nan, 1.0], [2.0, 3.0]])
    support = np.array([[False, True], [True, True]])
    validate_terrain_support(ground, support)
    assert np.isnan(ground[0, 0])
    support[0, 0] = True
    with pytest.raises(ValueError, match="1 pixels"):
        validate_terrain_support(ground, support)
    with pytest.raises(ValueError, match="Aligned"):
        validate_terrain_support(ground, support.astype(np.uint8))
