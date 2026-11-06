"""Indexed coastal clearances match independent whole-geometry distances."""

import numpy as np
import pytest
import shapely
from shapely.geometry import Polygon, box

from viewshed_toolkit.pipeline.weights.terrain.land_mask import (
    LandExclusionIndex,
    load_land_exclusion_index,
)


@pytest.mark.parametrize("clearance", [0.0, 0.2, 0.6])
def test_indexed_clearance_matches_independent_whole_geometry_distance(clearance):
    holes = [
        box(5 + 3 * x, 5 + 3 * y, 7 + 3 * x, 7 + 3 * y).exterior
        for x in range(20)
        for y in range(20)
    ]
    main = Polygon(box(0, 0, 70, 70).exterior, holes)
    island = box(5.8, 5.8, 6.2, 6.2)
    land = shapely.MultiPolygon([main, island])
    rng = np.random.default_rng(20261104)
    rays = shapely.linestrings(rng.uniform(-2, 72, (1000, 2, 2)))
    expected = shapely.distance(land, rays) <= clearance
    actual = LandExclusionIndex.from_land_parts(list(land.geoms), clearance).blocked(rays)
    np.testing.assert_array_equal(actual, expected)


def test_clearance_boundary_is_inclusive_and_not_arc_approximated():
    from shapely.geometry import LineString

    land = box(0, 0, 10, 10)
    index = LandExclusionIndex.from_land_parts([land], 30)
    rays = np.asarray(
        [
            LineString([(40, -2), (40, 2)]),
            LineString([(40.000001, -2), (40.000001, 2)]),
            LineString([(5, 5), (6, 6)]),
            LineString([(31.2132034, 31.2132034), (32, 32)]),
        ],
        dtype=object,
    )
    np.testing.assert_array_equal(index.blocked(rays), [True, False, True, True])
    assert not index.blocked(np.asarray([], dtype=object)).any()


def test_clearance_cache_reuses_exact_parts_and_detects_corruption(tmp_path, monkeypatch):
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit import load_app_config
    from viewshed_toolkit.pipeline.prepare.area.domains import DomainGeometries

    app = load_app_config(coastal_fixture(tmp_path))
    land = box(0, 0, 10, 10)
    domain = DomainGeometries(box(-20, -20, 20, 20), land, box(11, 0, 20, 10), "fixture", "fixture")
    first = load_land_exclusion_index(app, domain, 1.0, lambda: land)
    original = LandExclusionIndex.from_land_parts.__func__
    monkeypatch.setattr(
        LandExclusionIndex,
        "from_land_parts",
        classmethod(lambda *_a: pytest.fail("repeated boundary preparation")),
    )
    reused = load_land_exclusion_index(app, domain, 1.0, lambda: land)
    assert [p.wkb for p in first.parts] == [p.wkb for p in reused.parts]
    assert [p.wkb for p in first.boundary_parts] == [p.wkb for p in reused.boundary_parts]
    monkeypatch.setattr(LandExclusionIndex, "from_land_parts", classmethod(original))
    path = next((app.paths.output_dir / "geometry_cache/land_exclusions").glob("*.parquet"))
    path.write_bytes(b"corrupt")
    repaired = load_land_exclusion_index(app, domain, 1.0, lambda: land)
    assert [p.wkb for p in first.boundary_parts] == [p.wkb for p in repaired.boundary_parts]


def test_projection_repair_is_valid_and_retains_source_bytes(tmp_path, monkeypatch):
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit import load_app_config
    from viewshed_toolkit.pipeline.prepare.area.domains import DomainGeometries
    from viewshed_toolkit.pipeline.weights.terrain import land_mask

    source = Polygon(
        [(-122.8, 48.1), (-122.7, 48.2), (-122.8, 48.2), (-122.7, 48.1), (-122.8, 48.1)]
    )
    before = source.wkb
    app = load_app_config(coastal_fixture(tmp_path))
    domain = DomainGeometries(
        box(-123, 48, -122, 49), source, box(-123, 48, -122, 49), "fixture", "fixture"
    )
    first = land_mask.load_projected_land_domain(app, domain)
    assert first.is_valid and source.wkb == before
    monkeypatch.setattr(
        land_mask.Transformer, "from_crs", lambda *_a, **_k: pytest.fail("repeated projection")
    )
    assert land_mask.load_projected_land_domain(app, domain).wkb == first.wkb


def test_concave_holes_match_independent_distance():
    angles = np.linspace(0, 2 * np.pi, 129)[:-1]
    radius = 1 + 0.3 * np.cos(angles * 7)
    holes = [
        np.column_stack([radius * np.cos(angles) + x * 4 + 3, radius * np.sin(angles) + y * 4 + 3])
        for x in range(9)
        for y in range(9)
    ]
    polygon = Polygon(box(0, 0, 40, 40).exterior, holes)
    rng = np.random.default_rng(24)
    rays = shapely.linestrings(rng.uniform(-3, 43, (1000, 2, 2)))
    for clearance in (0.01, 0.3, 0.8, 1.5):
        np.testing.assert_array_equal(
            LandExclusionIndex.from_land_parts([polygon], clearance).blocked(rays),
            shapely.distance(polygon, rays) <= clearance,
        )


@pytest.mark.parametrize("clearance", [-1.0, float("nan"), float("inf")])
def test_invalid_clearance_fails_before_preparation(clearance):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        LandExclusionIndex.from_land_parts([box(0, 0, 10, 10)], clearance)


def test_zero_length_rays_use_valid_point_clearance():
    from shapely.geometry import LineString

    land = Polygon(box(0, 0, 100, 100).exterior, [box(20, 20, 80, 80).exterior])
    locations = [(21, 50), (50, 50), (5, 5), (101, 50), (111, 50)]
    rays = np.asarray([LineString([xy, xy]) for xy in locations], dtype=object)
    before = shapely.to_wkb(rays)
    actual = LandExclusionIndex.from_land_parts([land], 10).blocked(rays)
    expected = shapely.distance(land, shapely.points(locations)) <= 10
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(shapely.to_wkb(rays), before)
