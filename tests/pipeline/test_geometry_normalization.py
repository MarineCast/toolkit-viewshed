from __future__ import annotations

import geopandas as gpd
from shapely.geometry import GeometryCollection, LineString, Polygon, box

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
