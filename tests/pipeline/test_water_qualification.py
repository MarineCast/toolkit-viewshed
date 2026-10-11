import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box

from viewshed_toolkit.pipeline.prepare.elevation.canopy import (
    qualify_partial_canopy_grid,
    qualify_water_footprints,
)
from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_canopy_visibility


def qualify(land, water=None, support=None):
    return qualify_water_footprints(
        box(-10, -10, 100, 100) if water is None else water,
        land,
        shape=(3, 3),
        transform=from_origin(0, 90, 30, 30),
        geometry_crs=32610,
        raster_crs=32610,
        supported_mask=np.ones((3, 3), bool) if support is None else support,
    )


def test_pure_water_known_zero_not_unknown_obstruction():
    pure = qualify(box(200, 200, 201, 201))
    h, u = qualify_partial_canopy_grid(
        np.full((3, 3), np.nan), np.ones((3, 3), bool), pure, qualified_water=pure
    )
    assert np.all(h == 0) and not u.any()
    g = np.zeros((3, 3))
    v = partial_canopy_visibility(
        g, h, u, observer_row=1, observer_col=1, pixel_size_m=30, curvature_coefficient=0
    )
    assert np.all(v == 1)


@pytest.mark.parametrize("land", [box(1, 61, 4, 64), box(0, 60, 15, 90)])
def test_tiny_island_and_mixed_cell_preserve_land_unknown(land):
    pure = qualify(land)
    assert not pure[0, 0]  # island never covers cell center
    h, u = qualify_partial_canopy_grid(
        np.full((3, 3), np.nan), np.ones((3, 3), bool), np.ones((3, 3), bool), qualified_water=pure
    )
    assert u[0, 0] and not u[1, 1]
    known = np.full((3, 3), 7.0)
    h, u = qualify_partial_canopy_grid(
        known, np.zeros((3, 3), bool), np.ones((3, 3), bool), qualified_water=pure
    )
    assert h[0, 0] == 7 and h[1, 1] == 0


def test_coast_shift_or_geometry_overlap_does_not_erase_land():
    pure = qualify(box(0, 0, 31, 90), water=box(15, -10, 100, 100))
    assert not pure[:, 0].any() and not pure[:, 1].any()
    assert pure[:, 2].all()


def test_valid_zero_missing_land_and_unsupported_water_are_distinct():
    support = np.ones((3, 3), bool)
    support[2, 2] = False
    pure = qualify(box(1, 61, 4, 64), support=support)
    h = np.zeros((3, 3))
    u = np.zeros((3, 3), bool)
    h[2, 2] = np.nan
    u[2, 2] = True
    heights, unknown = qualify_partial_canopy_grid(
        h, u, np.ones((3, 3), bool), qualified_water=pure
    )
    assert heights[0, 0] == 0 and not unknown[0, 0]
    assert unknown[2, 2]
    with pytest.raises(ValueError):
        qualify_water_footprints(
            box(0, 0, 1, 1),
            box(2, 2, 3, 3),
            transform=from_origin(0, 90, 30, 30),
            shape=(3, 3),
            geometry_crs=4326,
            raster_crs=32610,
            supported_mask=support,
        )
    with pytest.raises(ValueError):
        qualify_partial_canopy_grid(h, u, np.zeros((3, 3), bool), qualified_water=pure)
