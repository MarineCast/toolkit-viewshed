"""Geometry helpers."""

from __future__ import annotations

import weakref
from typing import Any

import geopandas as gpd
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

CRS_WGS84 = "EPSG:4326"
_VALID_GEOMETRIES: dict[int, weakref.ReferenceType[Any]] = {}


def _geometry_is_valid(geom: Any) -> bool:
    """Reuse validity only for the same live, immutable Shapely 2 geometry."""
    key = id(geom)
    previous = _VALID_GEOMETRIES.get(key)
    if previous is not None and previous() is geom:
        return True
    if not geom.is_valid:
        return False
    remember_validated_geometry(geom)
    return True


def remember_validated_geometry(geom: Any) -> None:
    """Memoize topology proved by validation or a verified validated-cache checksum.

    Callers must already have that proof. Geometry objects are immutable and
    weak references prevent object-ID reuse from certifying a different value.
    """
    key = id(geom)

    def discard(reference: weakref.ReferenceType[Any]) -> None:
        if _VALID_GEOMETRIES.get(key) is reference:
            _VALID_GEOMETRIES.pop(key, None)

    if len(_VALID_GEOMETRIES) >= 4096:
        _VALID_GEOMETRIES.clear()
    _VALID_GEOMETRIES[key] = weakref.ref(geom, discard)


def _componentwise_intersection(left: Any, right: Any) -> Any:
    """Distribute a clip over disjoint polygon components without a global dissolve."""
    if not isinstance(left, MultiPolygon):
        return left.intersection(right)
    parts = shapely.get_parts(left)
    shapely.prepare(right)
    contained = shapely.covers(right, parts)
    if contained.all():
        return left
    selected = shapely.intersects(right, parts)
    output = list(parts[contained])
    for part in parts[selected & ~contained]:
        clipped = polygons_from_any(part.intersection(right))
        if isinstance(clipped, Polygon):
            output.append(clipped)
        elif isinstance(clipped, MultiPolygon):
            output.extend(clipped.geoms)
    if not output:
        return Polygon()
    return output[0] if len(output) == 1 else MultiPolygon(output)


try:
    from shapely import make_valid as _make_valid  # type: ignore
except Exception:  # pragma: no cover
    try:
        from shapely.validation import make_valid as _make_valid  # type: ignore
    except Exception:  # pragma: no cover
        _make_valid = None


def safe_make_valid(geom: Any) -> Any:
    """Repair a Shapely geometry using the best available Shapely API."""
    if geom is None or geom.is_empty:
        return geom
    if _geometry_is_valid(geom):
        return geom

    if _make_valid is not None:
        try:
            fixed = _make_valid(geom)
            if fixed is not None and not fixed.is_empty:
                return fixed
        except Exception:
            pass

    try:
        fixed = geom.buffer(0)
        if fixed is not None and not fixed.is_empty:
            return fixed
    except Exception:
        pass
    return geom


def safe_polygonal_union(
    frame: gpd.GeoDataFrame | gpd.GeoSeries,
    *,
    clip_geometry: Any | None = None,
    target_crs: Any | None = None,
) -> Polygon | MultiPolygon:
    """Return a valid polygonal union, optionally clipped and reprojected.

    Geometry repair and clipping happen in the source CRS before reprojection.
    This is important for large coastal datasets: projecting out-of-domain
    polygons into a local CRS can introduce self-intersections even when the
    original WGS84 geometry is valid.

    ``clip_geometry`` must use the same CRS as ``frame``. Empty and
    non-polygonal fragments produced by repair or clipping are discarded.
    """

    geometry = frame.geometry if isinstance(frame, gpd.GeoDataFrame) else frame
    if geometry.crs is None:
        raise ValueError("Cannot build a polygonal union without source CRS metadata.")

    clip_polygon = None
    if clip_geometry is not None:
        clip_polygon = polygons_from_any(safe_make_valid(clip_geometry))
        if clip_polygon is None or clip_polygon.is_empty or not _geometry_is_valid(clip_polygon):
            raise ValueError("Clip geometry could not be repaired to a valid polygon.")

    parts: list[Polygon | MultiPolygon] = []
    for value in geometry:
        polygonal = polygons_from_any(safe_make_valid(value))
        if polygonal is None or polygonal.is_empty:
            continue
        if not _geometry_is_valid(polygonal):
            raise ValueError("Input geometry could not be repaired before union.")
        if clip_polygon is not None:
            if not polygonal.intersects(clip_polygon):
                continue
            polygonal = polygons_from_any(
                safe_make_valid(_componentwise_intersection(polygonal, clip_polygon))
            )
            if polygonal is None or polygonal.is_empty:
                continue
            if not _geometry_is_valid(polygonal):
                raise ValueError("Clipped geometry could not be repaired before union.")
        parts.append(polygonal)

    if not parts:
        raise ValueError("No polygonal geometry remains after repair and clipping.")

    if target_crs is not None:
        projected = gpd.GeoSeries(parts, crs=geometry.crs).to_crs(target_crs)
        parts = []
        for value in projected:
            polygonal = polygons_from_any(safe_make_valid(value))
            if polygonal is None or polygonal.is_empty:
                continue
            if not _geometry_is_valid(polygonal):
                raise ValueError("Projected geometry could not be repaired before union.")
            parts.append(polygonal)
        if not parts:
            raise ValueError("No polygonal geometry remains after reprojection.")

    try:
        # A repaired single feature already represents its complete union.
        # Dissolving a regional MultiPolygon again can dominate preparation.
        if len(parts) == 1:
            merged = parts[0]
        elif hasattr(shapely, "disjoint_subset_union_all") and shapely.geos_version >= (3, 12, 0):
            merged = shapely.disjoint_subset_union_all(shapely.get_parts(parts))
        else:
            merged = unary_union(parts)
    except Exception as exc:
        raise ValueError(
            "Polygonal union failed after geometry repair, clipping, and reprojection."
        ) from exc

    merged = polygons_from_any(safe_make_valid(merged))
    if merged is None or merged.is_empty or not _geometry_is_valid(merged):
        raise ValueError("Polygonal union did not produce a valid polygon geometry.")
    return merged


def safe_polygonal_intersection(
    left: Any,
    right: Any,
    *,
    label: str = "polygonal intersection",
) -> Polygon | MultiPolygon:
    """Return a repaired polygon-only intersection with a precise diagnostic."""

    return _safe_polygonal_binary(left, right, operation="intersection", label=label)


def safe_polygonal_difference(
    left: Any,
    right: Any,
    *,
    label: str = "polygonal difference",
) -> Polygon | MultiPolygon:
    """Return a repaired polygon-only difference with a precise diagnostic."""

    return _safe_polygonal_binary(left, right, operation="difference", label=label)


def _safe_polygonal_binary(
    left: Any,
    right: Any,
    *,
    operation: str,
    label: str,
) -> Polygon | MultiPolygon:
    left_polygon = polygons_from_any(safe_make_valid(left))
    right_polygon = polygons_from_any(safe_make_valid(right))
    if left_polygon is None or left_polygon.is_empty:
        raise ValueError(f"{label}: left geometry has no valid polygonal component.")
    if right_polygon is None or right_polygon.is_empty:
        raise ValueError(f"{label}: right geometry has no valid polygonal component.")
    if not _geometry_is_valid(left_polygon) or not _geometry_is_valid(right_polygon):
        raise ValueError(f"{label}: an input could not be repaired to valid polygonal geometry.")
    try:
        if operation == "intersection":
            if isinstance(right_polygon, MultiPolygon) and not isinstance(
                left_polygon, MultiPolygon
            ):
                result = _componentwise_intersection(right_polygon, left_polygon)
            else:
                result = _componentwise_intersection(left_polygon, right_polygon)
        else:
            result = getattr(left_polygon, operation)(right_polygon)
    except Exception as exc:
        raise ValueError(f"{label}: GEOS {operation} failed after repairing both inputs.") from exc
    if result is None or result.is_empty:
        return Polygon()
    polygonal = polygons_from_any(safe_make_valid(result))
    if polygonal is None or polygonal.is_empty:
        return Polygon()
    if not _geometry_is_valid(polygonal):
        raise ValueError(f"{label}: result could not be repaired to valid polygonal geometry.")
    return polygonal


def polygons_from_any(geom: Any) -> Polygon | MultiPolygon | None:
    """Extract polygonal geometry from arbitrary Shapely geometry."""
    if geom is None or geom.is_empty:
        return None
    gt = geom.geom_type
    if gt == "Polygon":
        return geom
    if gt == "MultiPolygon":
        parts = [g for g in geom.geoms if g is not None and not g.is_empty]
        if not parts:
            return None
        if len(parts) > 1 and len(parts) == len(geom.geoms):
            return geom
        return MultiPolygon(parts) if len(parts) > 1 else parts[0]
    if gt == "GeometryCollection":
        polys = []
        for g in geom.geoms:
            pg = polygons_from_any(g)
            if pg is None:
                continue
            if pg.geom_type == "Polygon":
                polys.append(pg)
            elif pg.geom_type == "MultiPolygon":
                polys.extend(list(pg.geoms))
        if not polys:
            return None
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]
    return None
