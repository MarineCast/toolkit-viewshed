"""Land/water domain and H3 helper utilities for viewshed preparation.

This module owns prep/QA geometry concerns used to build the canonical
source-target lookup and water-source terrain inputs. The distance-weight stage
must not import these helpers for pair generation.
"""

from __future__ import annotations

import logging
import math
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, box

from viewshed_toolkit._internal.geo.geometry import (
    safe_polygonal_difference,
    safe_polygonal_intersection,
    safe_polygonal_union,
)
from viewshed_toolkit._internal.geo.h3 import bbox_h3_cells as core_bbox_h3_cells
from viewshed_toolkit._internal.geo.h3 import (
    cell_to_latlng,
    grid_disk_set,
)

from ...config import (
    VIEWSHED_DATA_ROOT,
    resolve_existing_or_relative_path,
    seascape_water_polygon_path,
)

LOGGER = logging.getLogger(__name__)
CRS_WGS84 = "EPSG:4326"
NATURAL_EARTH_LAND_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip"

AVG_H3_EDGE_LENGTH_KM: dict[int, float] = {
    0: 1281.256011,
    1: 483.056839,
    2: 182.512956,
    3: 68.979222,
    4: 26.071760,
    5: 9.854091,
    6: 3.724533,
    7: 1.406476,
    8: 0.531414,
    9: 0.200786,
    10: 0.075864,
    11: 0.028663,
    12: 0.010830,
    13: 0.004092,
    14: 0.001546,
    15: 0.000584,
}


@dataclass(frozen=True)
class DomainGeometries:
    bbox_polygon: Polygon
    land_domain: Any
    water_domain: Any
    land_source: str
    water_source: str


def h3_grid_disk(cell: str, k: int) -> set[str]:
    return grid_disk_set(cell, int(k))


def h3_cell_latlon(cell: str) -> tuple[float, float]:
    return cell_to_latlng(cell)


def bbox_h3_cells(
    bbox_wgs84: tuple[float, float, float, float],
    resolution: int,
    *,
    buffer_rings: int = 1,
    strict_intersection: bool = True,
) -> list[str]:
    return core_bbox_h3_cells(
        bbox_wgs84,
        resolution,
        buffer_rings=buffer_rings,
        strict_intersection=strict_intersection,
    )


def estimate_grid_disk_k(
    resolution: int,
    max_distance_km: float,
    *,
    override_k: int | None = None,
) -> int:
    if override_k is not None:
        return int(override_k)

    edge_km = AVG_H3_EDGE_LENGTH_KM.get(int(resolution))
    if edge_km is None:
        raise ValueError(f"Unsupported H3 resolution for ring estimate: {resolution}")
    return int(math.ceil(float(max_distance_km) / edge_km)) + 2


def _read_vector_any(path: Path) -> gpd.GeoDataFrame:
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith(".parquet") or suffixes.endswith(".geoparquet"):
        return gpd.read_parquet(path)
    return gpd.read_file(path)


def _natural_earth_land_path(cache_dir: Path | None = None, overwrite: bool = False) -> Path:
    cache_dir = cache_dir or (VIEWSHED_DATA_ROOT / "natural_earth")
    cache_dir.mkdir(parents=True, exist_ok=True)
    shp_path = cache_dir / "ne_10m_land.shp"
    if shp_path.exists() and not overwrite:
        return shp_path

    zip_path = cache_dir / "ne_10m_land.zip"
    if overwrite or not zip_path.exists():
        LOGGER.info("Downloading Natural Earth land polygons to %s", zip_path)
        urllib.request.urlretrieve(NATURAL_EARTH_LAND_URL, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(cache_dir)

    if not shp_path.exists():
        raise FileNotFoundError(f"Natural Earth land shapefile was not extracted: {shp_path}")
    return shp_path


def _load_optional_clipped_union(
    path: Path | None,
    *,
    bbox_gdf: gpd.GeoDataFrame,
    label: str,
) -> tuple[Any | None, str]:
    if path is None:
        return None, "none"
    if not path.exists():
        return None, f"missing:{path}"

    gdf = _read_vector_any(path)
    if gdf.empty:
        return None, f"empty:{path}"
    if gdf.crs is None:
        raise ValueError(f"{label} polygon has no CRS metadata: {path}")

    source = gdf.to_crs(CRS_WGS84)
    try:
        union = safe_polygonal_union(source, clip_geometry=bbox_gdf.geometry.iloc[0])
    except ValueError as exc:
        if "No polygonal geometry remains" in str(exc):
            return None, f"empty_after_clip:{path}"
        raise ValueError(f"Could not normalize {label} polygon source {path}: {exc}") from exc
    return union, str(path)


def _land_domain_geometry(
    bbox_polygon: Polygon,
    land_union: Any,
    water_union: Any | None,
) -> Any:
    """Mirror prepare_land shoreline logic for consistent mixed-cell treatment."""

    if water_union is None or water_union.is_empty:
        return land_union

    natural_earth_water = safe_polygonal_difference(
        bbox_polygon,
        land_union,
        label="bbox minus Natural Earth land",
    )
    offshore_water = safe_polygonal_difference(
        natural_earth_water,
        water_union,
        label="Natural Earth water minus canonical water",
    )
    boundary_zone = bbox_polygon.boundary.buffer(1e-9)
    water_parts = [water_union]
    if not offshore_water.is_empty:
        for geom in getattr(offshore_water, "geoms", [offshore_water]):
            if not geom.is_empty and geom.intersects(boundary_zone):
                water_parts.append(geom)

    filled_water_union = safe_polygonal_union(gpd.GeoSeries(water_parts, crs=CRS_WGS84))
    filled_water = safe_polygonal_intersection(
        filled_water_union,
        bbox_polygon,
        label="filled water clipped to bbox",
    )
    return safe_polygonal_difference(
        bbox_polygon,
        filled_water,
        label="land domain as bbox minus filled water",
    )


def target_domain_polygon(runtime: Any) -> Any:
    """Return the configured target domain around the observer-source bbox.

    Source locations remain restricted to ``runtime.bbox_wgs84``. Targets may
    fall outside that bbox by the maximum LOS distance plus the configured AOI
    margin. Buffering is performed in the configured projected CRS so degrees
    are never treated as metres.
    """

    source_polygon = box(*runtime.bbox_wgs84)
    viewshed = runtime.raw_config.get("viewshed", {}) or {}
    max_distance_m = float(viewshed.get("max_distance_m", 30_000.0))
    aoi_margin_m = float(viewshed.get("aoi_margin_m", 1_000.0))
    buffer_m = max_distance_m + aoi_margin_m
    if not math.isfinite(buffer_m) or buffer_m < 0:
        raise ValueError("viewshed.max_distance_m + aoi_margin_m must be finite and >= 0")
    if buffer_m == 0:
        return source_polygon

    projected = gpd.GeoSeries([source_polygon], crs=CRS_WGS84).to_crs(runtime.projected_crs)
    return (
        gpd.GeoSeries(projected.buffer(buffer_m), crs=runtime.projected_crs)
        .to_crs(CRS_WGS84)
        .iloc[0]
    )


def load_land_water_domains(
    runtime: Any,
    *,
    extent: str = "source",
) -> DomainGeometries:
    if extent not in {"source", "target"}:
        raise ValueError("extent must be 'source' or 'target'")
    paths_cfg = runtime.raw_config.get("paths", {}) or {}
    bbox_polygon = (
        box(*runtime.bbox_wgs84) if extent == "source" else target_domain_polygon(runtime)
    )
    bbox_gdf = gpd.GeoDataFrame({"name": ["bbox"]}, geometry=[bbox_polygon], crs=CRS_WGS84)

    water_path_value = paths_cfg.get("water_polygon_path")
    water_path = (
        resolve_existing_or_relative_path(water_path_value, runtime.config_dir)
        if water_path_value
        else seascape_water_polygon_path()
    )
    water_union, water_source = _load_optional_clipped_union(
        water_path,
        bbox_gdf=bbox_gdf,
        label="Water",
    )

    land_path_value = paths_cfg.get("land_polygon_path")
    land_source_path: Path | None = None
    if land_path_value:
        land_source_path = resolve_existing_or_relative_path(land_path_value, runtime.config_dir)
    else:
        try:
            land_source_path = _natural_earth_land_path(overwrite=False)
        except Exception as exc:
            LOGGER.warning("Could not load Natural Earth land polygons: %s", exc)
            land_source_path = None

    land_union, land_source = _load_optional_clipped_union(
        land_source_path,
        bbox_gdf=bbox_gdf,
        label="Land",
    )

    if water_union is None and land_union is None:
        raise FileNotFoundError(
            "Could not build land/water domains. Provide paths.water_polygon_path, "
            "paths.land_polygon_path, or allow Natural Earth land cache/download."
        )

    if water_union is None:
        water_domain = safe_polygonal_difference(
            bbox_polygon,
            land_union,
            label="water domain as bbox minus land",
        )
        water_source = "bbox_minus_land"
    else:
        water_domain = safe_polygonal_intersection(
            water_union,
            bbox_polygon,
            label="canonical water clipped to bbox",
        )

    if land_union is None:
        land_domain = safe_polygonal_difference(
            bbox_polygon,
            water_domain,
            label="land domain as bbox minus water",
        )
        land_source = "bbox_minus_water"
    else:
        land_domain = _land_domain_geometry(bbox_polygon, land_union, water_union)

    if land_domain.is_empty:
        raise ValueError("Land domain is empty after clipping/intersection.")
    if water_domain.is_empty:
        raise ValueError("Water domain is empty after clipping/intersection.")

    return DomainGeometries(
        bbox_polygon=bbox_polygon,
        land_domain=land_domain,
        water_domain=water_domain,
        land_source=land_source,
        water_source=water_source,
    )


def _build_centroid_lookup(cells: Sequence[str]) -> pd.DataFrame:
    rows = []
    for cell in cells:
        lat, lon = h3_cell_latlon(cell)
        rows.append((cell, lat, lon))
    return pd.DataFrame(rows, columns=["h3", "lat", "lon"]).set_index("h3")
