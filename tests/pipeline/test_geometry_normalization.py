from __future__ import annotations

import gc
import weakref

import geopandas as gpd
import numpy as np
import pytest
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

from viewshed_toolkit._internal.geo import geometry as geometry_module

from viewshed_toolkit._internal.geo.geometry import (
    safe_polygonal_difference,
    safe_polygonal_intersection,
    safe_polygonal_union,
)


def test_safe_union_repairs_invalid_and_mixed_geometry() -> None:
    bow_tie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    mixed = GeometryCollection([box(3, 0, 4, 1), LineString([(0, 0), (4, 1)])])
    thin_sliver = Polygon([(4.0, 0.0), (4.000001, 0.0), (4.000001, 1.0), (4.0, 0.0)])
    frame = gpd.GeoDataFrame(
        geometry=[bow_tie, mixed, thin_sliver],
        crs="EPSG:4326",
    )

    result = safe_polygonal_union(frame, clip_geometry=box(-1, -1, 5, 3))

    assert result.geom_type in {"Polygon", "MultiPolygon"}
    assert result.is_valid
    assert not result.is_empty


def test_safe_boolean_operations_repair_self_touching_inputs() -> None:
    invalid = Polygon([(0, 0), (3, 3), (0, 3), (3, 0), (0, 0)])
    clip = box(0, 0, 1.5, 3)

    intersection = safe_polygonal_intersection(
        invalid,
        clip,
        label="invalid fixture intersection",
    )
    difference = safe_polygonal_difference(
        invalid,
        clip,
        label="invalid fixture difference",
    )

    assert intersection.is_valid
    assert difference.is_valid
    assert intersection.geom_type in {"Polygon", "MultiPolygon"}
    assert difference.geom_type in {"Polygon", "MultiPolygon"}


@pytest.mark.parametrize(
    "geometry",
    [
        box(0, 0, 1, 1),
        MultiPolygon([box(0, 0, 1, 1), box(2, 0, 3, 1)]),
    ],
)
def test_valid_single_feature_preserves_coordinates_without_repair_or_dissolve(
    monkeypatch: pytest.MonkeyPatch, geometry: Polygon | MultiPolygon
) -> None:
    def unexpected_operation(*args: object, **kwargs: object) -> None:
        raise AssertionError("Valid single-feature input requires no repair or dissolve")

    monkeypatch.setattr(geometry_module, "_make_valid", unexpected_operation)
    monkeypatch.setattr(geometry_module, "unary_union", unexpected_operation)
    result = safe_polygonal_union(gpd.GeoSeries([geometry], crs="EPSG:4326"))

    assert result.is_valid
    assert result.wkb == geometry.wkb


def test_multiple_overlapping_features_still_dissolve() -> None:
    result = safe_polygonal_union(
        gpd.GeoSeries([box(0, 0, 2, 1), box(1, 0, 3, 1)], crs="EPSG:4326")
    )

    assert result.is_valid
    assert result.equals(box(0, 0, 3, 1))


@pytest.mark.parametrize(
    "clip",
    [
        box(-10, -10, 300, 150),
        box(15, -10, 150, 90),
        Polygon([(0, -10), (210, -10), (210, 90), (100, 90), (100, 20), (0, 20)]),
    ],
)
def test_component_clip_matches_direct_geometry_and_30m_mask(clip: Polygon) -> None:
    water = MultiPolygon(
        [
            Polygon(box(0, 0, 90, 90).exterior, [box(30, 30, 60, 60).exterior]),
            box(150, 0, 240, 90),
        ]
    )
    expected = geometry_module.polygons_from_any(water.intersection(clip))
    actual = safe_polygonal_union(gpd.GeoSeries([water], crs="EPSG:32610"), clip_geometry=clip)

    assert expected is not None
    assert actual.is_valid
    assert actual.equals(expected)
    options = dict(out_shape=(5, 10), transform=from_origin(-30, 120, 30, 30), all_touched=True)
    np.testing.assert_array_equal(rasterize([actual], **options), rasterize([expected], **options))


def test_geometry_validity_reuses_identity_without_retaining_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    water = MultiPolygon([box(0, 0, 1, 1), box(3, 0, 4, 1)])
    original = BaseGeometry.is_valid.fget
    assert original is not None
    checks = []

    def tracked(geometry: BaseGeometry) -> bool:
        checks.append(id(geometry))
        return original(geometry)

    monkeypatch.setattr(BaseGeometry, "is_valid", property(tracked))
    for _ in range(2):
        result = safe_polygonal_union(gpd.GeoSeries([water], crs="EPSG:4326"))
        assert result.wkb == water.wkb
    assert checks.count(id(water)) == 1

    reference = weakref.ref(water)
    key = id(water)
    del result, water
    gc.collect()
    assert reference() is None
    assert key not in geometry_module._VALID_GEOMETRIES


@pytest.mark.parametrize("use_optimized_union", [True, False])
def test_disconnected_union_matches_reference_with_nested_water_and_touching_parts(
    monkeypatch: pytest.MonkeyPatch, use_optimized_union: bool
) -> None:
    water = MultiPolygon(
        [
            Polygon(box(0, 0, 10, 10).exterior, [box(2, 2, 8, 8).exterior]),
            box(3, 3, 4, 4),
            box(20, 0, 21, 1),
        ]
    )
    additions = MultiPolygon([box(8, 0, 15, 10), box(40, 0, 41, 1)])
    expected = water.union(additions)
    if not use_optimized_union:
        monkeypatch.delattr(geometry_module.shapely, "disjoint_subset_union_all", raising=False)
    actual = safe_polygonal_union(gpd.GeoSeries([water, additions], crs="EPSG:4326"))

    assert actual.is_valid
    assert actual.equals(expected)
    assert actual.area == expected.area
