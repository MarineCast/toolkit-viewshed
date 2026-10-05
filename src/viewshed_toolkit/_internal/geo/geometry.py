"""Geometry helpers."""

from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

LOGGER = logging.getLogger(__name__)
CRS_WGS84 = "EPSG:4326"

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
        if clip_polygon is None or clip_polygon.is_empty or not clip_polygon.is_valid:
            raise ValueError("Clip geometry could not be repaired to a valid polygon.")

    parts: list[Polygon | MultiPolygon] = []
    for value in geometry:
        polygonal = polygons_from_any(safe_make_valid(value))
        if polygonal is None or polygonal.is_empty:
            continue
        if not polygonal.is_valid:
            raise ValueError("Input geometry could not be repaired before union.")
        if clip_polygon is not None:
            if not polygonal.intersects(clip_polygon):
                continue
            polygonal = polygons_from_any(safe_make_valid(polygonal.intersection(clip_polygon)))
            if polygonal is None or polygonal.is_empty:
                continue
            if not polygonal.is_valid:
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
            if not polygonal.is_valid:
                raise ValueError("Projected geometry could not be repaired before union.")
            parts.append(polygonal)
        if not parts:
            raise ValueError("No polygonal geometry remains after reprojection.")

    try:
        merged = unary_union(parts)
    except Exception as exc:
        raise ValueError(
            "Polygonal union failed after geometry repair, clipping, and reprojection."
        ) from exc

    merged = polygons_from_any(safe_make_valid(merged))
    if merged is None or merged.is_empty or not merged.is_valid:
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
    if not left_polygon.is_valid or not right_polygon.is_valid:
        raise ValueError(f"{label}: an input could not be repaired to valid polygonal geometry.")
    try:
        result = getattr(left_polygon, operation)(right_polygon)
    except Exception as exc:
        raise ValueError(f"{label}: GEOS {operation} failed after repairing both inputs.") from exc
    if result is None or result.is_empty:
        return Polygon()
    polygonal = polygons_from_any(safe_make_valid(result))
    if polygonal is None or polygonal.is_empty:
        return Polygon()
    if not polygonal.is_valid:
        raise ValueError(f"{label}: result could not be repaired to valid polygonal geometry.")
    return polygonal


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _clean_ring(coords: Any, tol: float = 1e-9, min_unique: int = 3) -> list | None:
    if coords is None:
        return None
    pts = list(coords)
    if len(pts) < 4:
        return None

    tol2 = tol * tol
    out = [pts[0]]
    for p in pts[1:]:
        if _dist2(p, out[-1]) > tol2:
            out.append(p)

    if _dist2(out[0], out[-1]) > tol2:
        out.append(out[0])

    changed = True
    while changed and len(out) >= 4:
        changed = False
        new_out = [out[0]]
        for i in range(1, len(out) - 1):
            prev_pt = new_out[-1]
            cur_pt = out[i]
            next_pt = out[i + 1]
            if _dist2(prev_pt, next_pt) <= tol2:
                changed = True
                continue
            new_out.append(cur_pt)
        new_out.append(out[-1])
        if _dist2(new_out[0], new_out[-1]) > tol2:
            new_out.append(new_out[0])
        out = new_out

    unique = []
    for p in out[:-1]:
        if not unique or _dist2(p, unique[-1]) > tol2:
            unique.append(p)
    if len(unique) < min_unique:
        return None
    return out


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


def _clean_polygon(poly: Polygon, tol: float = 1e-9) -> Polygon | None:
    if poly is None or poly.is_empty:
        return None
    ext = _clean_ring(list(poly.exterior.coords), tol=tol)
    if ext is None:
        return None

    new_holes = []
    for ring in poly.interiors:
        cleaned = _clean_ring(list(ring.coords), tol=tol)
        if cleaned is None:
            continue
        try:
            hole_poly = Polygon(cleaned)
            if hole_poly.is_empty or hole_poly.area <= 0:
                continue
        except Exception:
            continue
        new_holes.append(cleaned)

    rebuilt = Polygon(ext, new_holes)
    if not rebuilt.is_valid:
        rebuilt2 = polygons_from_any(safe_make_valid(rebuilt))
        if rebuilt2 is None:
            return None
        if rebuilt2.geom_type == "MultiPolygon":
            rebuilt2 = max(rebuilt2.geoms, key=lambda g: g.area)
        rebuilt = rebuilt2
    return rebuilt if rebuilt is not None and not rebuilt.is_empty else None


def clean_geometry(geom: Any, tol: float = 1e-9) -> Polygon | MultiPolygon | None:
    """Clean degenerate rings and return polygonal geometry only."""
    if geom is None or geom.is_empty:
        return None
    gt = geom.geom_type
    if gt == "Polygon":
        return _clean_polygon(geom, tol=tol)
    if gt == "MultiPolygon":
        parts = []
        for p in geom.geoms:
            cp = _clean_polygon(p, tol=tol)
            if cp is not None and not cp.is_empty:
                parts.append(cp)
        if not parts:
            return None
        mp = MultiPolygon(parts) if len(parts) > 1 else parts[0]
        if not mp.is_valid:
            return polygons_from_any(safe_make_valid(mp))
        return mp

    fixed = polygons_from_any(safe_make_valid(geom))
    if fixed is None:
        return None
    return clean_geometry(fixed, tol=tol)
