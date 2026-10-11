import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, box
from shapely.ops import transform as project

from viewshed_toolkit import above_ground_visibility, rasterize_building_footprints
from viewshed_toolkit.pipeline.prepare.elevation.buildings import BuildingSurface

GRID = Affine(30, 0, 500000, 0, -30, 5400000)


def footprint(geometry):
    projector = Transformer.from_crs(32610, 3857, always_xy=True)
    return project(projector.transform, geometry).wkb


def test_reprojection_max_and_missing_overlap_are_order_independent():
    a = footprint(box(500031, 5399939, 500059, 5399969))
    b = footprint(box(500040, 5399940, 500080, 5399980))
    items = [(a, 7), (b, 12), (a, None)]
    surface = rasterize_building_footprints(items, shape=(4, 4), transform=GRID, crs="EPSG:32610")
    reordered = rasterize_building_footprints(
        reversed(items), shape=(4, 4), transform=GRID, crs="EPSG:32610"
    )
    for field in ("lower_height", "present", "missing_height", "unknown"):
        assert np.array_equal(getattr(surface, field), getattr(reordered, field))
    assert surface.lower_height[1, 1] == 12  # Maximum, not 19 or final feature.
    assert surface.present[1, 1] and surface.missing_height[1, 1] and surface.unknown[1, 1]
    assert surface.unknown[3, 3] and not surface.present[3, 3]  # No inferred absence.
    assert surface.footprint_count == 3 and surface.missing_height_footprint_count == 1


def test_empty_and_zero_predictions_preserve_support():
    empty = rasterize_building_footprints([], shape=(2, 2), transform=GRID, crs="EPSG:32610")
    assert empty.unknown.all() and not empty.present.any()
    zero = rasterize_building_footprints(
        [(footprint(box(500001, 5399971, 500029, 5399999)), 0)],
        shape=(2, 2),
        transform=GRID,
        crs="EPSG:32610",
    )
    assert zero.present[0, 0] and zero.lower_height[0, 0] == 0 and not zero.unknown[0, 0]


@pytest.mark.parametrize("height", [-1, np.nan, np.inf, True])
def test_invalid_height_rejected(height):
    with pytest.raises(ValueError, match="height"):
        rasterize_building_footprints(
            [(footprint(box(500001, 5399971, 500029, 5399999)), height)],
            shape=(2, 2),
            transform=GRID,
            crs="EPSG:32610",
        )


@pytest.mark.parametrize(
    "crs,grid,shape",
    [
        ("EPSG:4326", GRID, (2, 2)),
        ("EPSG:32610", Affine(30, 0, 0, 0, -60, 0), (2, 2)),
        ("EPSG:32610", GRID, (0, 2)),
    ],
)
def test_invalid_grid_rejected(crs, grid, shape):
    with pytest.raises(ValueError, match="grid"):
        rasterize_building_footprints([], shape=shape, transform=grid, crs=crs)


def test_projected_invalid_polygon_repair_recorded():
    bow = Polygon([(500001, 5399971), (500029, 5399999), (500029, 5399971), (500001, 5399999)])
    surface = rasterize_building_footprints(
        [(footprint(bow), 3)], shape=(2, 2), transform=GRID, crs="EPSG:32610"
    )
    assert surface.projected_geometry_repair_count == 1 and surface.present.any()


def test_joint_caller_never_clears_neighboring_building():
    ground = np.zeros((9, 9), dtype="float32")
    floor = ground.copy()
    floor[4, 5] = 100
    present = np.ones(ground.shape, bool)
    absent = np.zeros(ground.shape, bool)
    building = BuildingSurface(floor, present, absent, absent, 1, 0, 0)
    # Broad private canopy clearance intentionally includes the neighboring roof.
    canopy_clearance = present.copy()
    states = above_ground_visibility(
        ground,
        ground,
        absent,
        building,
        qualified_water=absent,
        observer_row=4,
        observer_col=4,
        pixel_size_m=30,
        canopy_clearance_mask=canopy_clearance,
        grounded_observer=True,
    )
    assert states[4, 7] == 0 and floor[4, 5] == 100
    unknown = present.copy()
    building = BuildingSurface(ground, absent, absent, unknown, 0, 0, 0)
    states = above_ground_visibility(
        ground,
        ground,
        absent,
        building,
        qualified_water=absent,
        observer_row=4,
        observer_col=4,
        pixel_size_m=30,
    )
    assert np.all(states == -1)  # Grounding must be an explicit caller assumption.
