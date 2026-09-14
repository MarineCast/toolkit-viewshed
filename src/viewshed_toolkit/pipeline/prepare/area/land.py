from __future__ import annotations

import argparse
import logging
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import geopandas as gpd
from shapely.geometry import box

from viewshed_toolkit._internal.geo.geometry import (
    safe_polygonal_difference,
    safe_polygonal_intersection,
    safe_polygonal_union,
)
from viewshed_toolkit._internal.geo.h3 import (
    bbox_h3_cells,
)

from ...config import (
    DEFAULT_CONFIG,
    VIEWSHED_DATA_ROOT,
    bbox_from_config,
    get_run_version,
    land_h3_path_from_config,
    load_yaml,
    resolve_existing_or_relative_path,
    seascape_water_polygon_path,
    stable_config_hash,
    validate_land_h3_resolution,
)
from ...config.distance import load_distance_runtime
from .geometry import ensure_h3_geometry_artifact, load_h3_geometry_lookup

CRS_WGS84 = "EPSG:4326"
DEFAULT_PROJECTED_CRS = "EPSG:32610"
NATURAL_EARTH_LAND_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LandCellsResult:
    land_polygon_path: Path
    land_h3_path: Path
    natural_earth_path: Path
    n_land_features: int
    n_h3_cells: int
    h3_resolution: int


def bbox_candidate_h3_cells(
    bbox_wgs84: tuple[float, float, float, float],
    resolution: int,
    neighbor_distance: int = 1,
) -> set[str]:
    return set(
        bbox_h3_cells(
            bbox_wgs84,
            resolution,
            buffer_rings=neighbor_distance,
            strict_intersection=False,
        )
    )


def _land_domain_geometry(
    bbox_polygon,
    land_union,
    water_union,
):
    # The high-resolution water polygon is the best shoreline source. Its
    # complement captures islands/spits that Natural Earth misses. Because
    # territorial water stops offshore, use Natural Earth only to identify
    # open-water components connected to the bbox edge and add those to water.
    if water_union is None or water_union.is_empty:
        return land_union

    natural_earth_water = safe_polygonal_difference(
        bbox_polygon, land_union, label="land-cell bbox minus Natural Earth land"
    )
    offshore_water = safe_polygonal_difference(
        natural_earth_water,
        water_union,
        label="land-cell open water minus canonical water",
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
        label="land-cell filled water clipped to bbox",
    )
    return safe_polygonal_difference(
        bbox_polygon,
        filled_water,
        label="land-cell domain as bbox minus filled water",
    )


def natural_earth_land_path(cache_dir: Path | None = None, overwrite: bool = False) -> Path:
    cache_dir = cache_dir or (VIEWSHED_DATA_ROOT / "natural_earth")
    cache_dir.mkdir(parents=True, exist_ok=True)
    shp_path = cache_dir / "ne_10m_land.shp"
    if shp_path.exists() and not overwrite:
        return shp_path

    zip_path = cache_dir / "ne_10m_land.zip"
    if overwrite or not zip_path.exists():
        urllib.request.urlretrieve(NATURAL_EARTH_LAND_URL, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(cache_dir)

    if not shp_path.exists():
        raise FileNotFoundError(f"Natural Earth land shapefile was not extracted: {shp_path}")
    return shp_path


def build_land_cells_for_config(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    h3_resolution: int | None = None,
) -> LandCellsResult:
    raw, config_dir = load_yaml(config_path)
    bbox_wgs84 = bbox_from_config(raw)
    resolution = int(h3_resolution or raw.get("h3", {}).get("source_resolution", 6))
    region_cfg = raw.get("region", {})
    paths_cfg = raw.get("paths", {})
    projected_crs = str(
        raw.get("viewshed", {}).get("crs_projected")
        or region_cfg.get("crs_projected")
        or DEFAULT_PROJECTED_CRS
    )
    coastal_buffer_m = float(region_cfg.get("coastal_buffer_m", 6_000))
    min_land_fraction = float(region_cfg.get("min_source_cell_land_fraction", 0.01))
    max_water_fraction_raw = region_cfg.get("max_source_cell_water_fraction", 0.95)
    max_water_fraction = None if max_water_fraction_raw is None else float(max_water_fraction_raw)
    land_h3_path = land_h3_path_from_config(raw, config_dir)
    land_polygon_path = land_h3_path.with_name("_LAND_CLIP_NOT_WRITTEN.parquet")
    configured_land_path = paths_cfg.get("land_polygon_path")
    ne_path = (
        resolve_existing_or_relative_path(configured_land_path, config_dir)
        if configured_land_path
        else natural_earth_land_path(overwrite=False)
    )
    if not ne_path.exists():
        raise FileNotFoundError(f"Configured land polygon does not exist: {ne_path}")

    if land_h3_path.exists() and not overwrite:
        h3_gdf = gpd.read_parquet(land_h3_path)
        validate_existing_land_h3_file(
            h3_gdf,
            requested_resolution=resolution,
            path=land_h3_path,
        )
        return LandCellsResult(
            land_polygon_path=land_polygon_path,
            land_h3_path=land_h3_path,
            natural_earth_path=ne_path,
            n_land_features=0,
            n_h3_cells=len(h3_gdf),
            h3_resolution=resolution,
        )

    land = gpd.read_file(ne_path).to_crs(CRS_WGS84)
    bbox_gdf = gpd.GeoDataFrame(
        {"name": ["config_bbox"]}, geometry=[box(*bbox_wgs84)], crs=CRS_WGS84
    )
    bbox_polygon = bbox_gdf.geometry.iloc[0]
    try:
        land_union = safe_polygonal_union(land, clip_geometry=bbox_polygon)
    except ValueError as exc:
        if "No polygonal geometry remains" not in str(exc):
            raise
        land_union = None
    land_clip = gpd.GeoDataFrame(
        geometry=[] if land_union is None else [land_union],
        crs=CRS_WGS84,
    )

    if land_union is None:
        h3_gdf = gpd.GeoDataFrame(
            columns=[
                "h3_cell",
                "h3_resolution",
                "cell_area_m2",
                "land_area_m2",
                "land_fraction",
                "centroid_lon",
                "centroid_lat",
                "geometry",
            ],
            geometry="geometry",
            crs=CRS_WGS84,
        )
    else:
        water_union = None
        water_path_value = paths_cfg.get("water_polygon_path")
        water_path = (
            resolve_existing_or_relative_path(water_path_value, config_dir)
            if water_path_value
            else seascape_water_polygon_path(config_path)
        )
        if water_path.exists():
            water = gpd.read_parquet(water_path)
            if water.crs is None:
                raise ValueError(f"Water polygon has no CRS metadata: {water_path}")
            try:
                water_union = safe_polygonal_union(
                    water.to_crs(CRS_WGS84),
                    clip_geometry=bbox_polygon,
                )
            except ValueError as exc:
                if "No polygonal geometry remains" not in str(exc):
                    raise

        # Land is all available land with the high-resolution water polygon
        # carved out; water is everything else inside the bbox.
        land_domain = _land_domain_geometry(bbox_polygon, land_union, water_union)
        water_domain = safe_polygonal_difference(
            bbox_polygon,
            land_domain,
            label="land-cell water domain as bbox minus land",
        )

        candidate_cells = sorted(bbox_candidate_h3_cells(bbox_wgs84, resolution))
        LOGGER.info("Candidate H3 source cells before land clipping: %d", len(candidate_cells))
        runtime = replace(
            load_distance_runtime(config_path),
            source_resolution=resolution,
            target_resolution=resolution,
        )
        geometry_path = ensure_h3_geometry_artifact(
            runtime,
            candidate_cells,
            projected_crs=projected_crs,
            overwrite=False,
        )
        geometry_wgs84 = load_h3_geometry_lookup(geometry_path, "geometry_wgs84")
        geometry_projected = load_h3_geometry_lookup(geometry_path, "geometry_projected")
        h3_gdf = gpd.GeoDataFrame(
            {
                "h3_cell": candidate_cells,
                "h3_resolution": [resolution] * len(candidate_cells),
            },
            geometry=[geometry_wgs84[cell] for cell in candidate_cells],
            crs=CRS_WGS84,
        )
        h3_gdf = h3_gdf[h3_gdf.intersects(land_domain) & h3_gdf.intersects(bbox_polygon)].copy()
        h3_gdf["full_h3_geometry"] = h3_gdf.geometry
        h3_gdf["land_geometry"] = h3_gdf.geometry.intersection(land_domain)
        h3_gdf = h3_gdf[h3_gdf["land_geometry"].notna() & ~h3_gdf["land_geometry"].is_empty].copy()

        full_projected = gpd.GeoSeries(
            [geometry_projected[cell] for cell in h3_gdf["h3_cell"].astype(str)],
            crs=projected_crs,
        )
        land_projected = gpd.GeoSeries(h3_gdf["land_geometry"], crs=CRS_WGS84).to_crs(projected_crs)
        h3_gdf["cell_area_m2"] = full_projected.area.to_numpy()
        h3_gdf["land_area_m2"] = land_projected.area.to_numpy()
        h3_gdf["land_fraction"] = (h3_gdf["land_area_m2"] / h3_gdf["cell_area_m2"]).fillna(0.0)

        if not water_domain.is_empty:
            h3_gdf["water_geometry"] = h3_gdf["full_h3_geometry"].intersection(water_domain)
            water_projected = gpd.GeoSeries(h3_gdf["water_geometry"], crs=CRS_WGS84).to_crs(
                projected_crs
            )
            h3_gdf["water_area_m2"] = water_projected.area.to_numpy()
            h3_gdf["water_fraction"] = (h3_gdf["water_area_m2"] / h3_gdf["cell_area_m2"]).fillna(
                0.0
            )

            water_domain_projected = (
                gpd.GeoSeries([water_domain], crs=CRS_WGS84).to_crs(projected_crs).iloc[0]
            )
            h3_gdf["distance_to_water_m"] = land_projected.distance(
                water_domain_projected
            ).to_numpy()

        h3_gdf["touches_or_intersects_land"] = True
        centroids = land_projected.representative_point()
        centroids_wgs84 = gpd.GeoSeries(centroids, crs=projected_crs).to_crs(CRS_WGS84)
        h3_gdf["centroid_lon"] = centroids_wgs84.x.to_numpy()
        h3_gdf["centroid_lat"] = centroids_wgs84.y.to_numpy()
        n_before_filters = len(h3_gdf)
        land_mask = h3_gdf["land_fraction"] >= min_land_fraction
        n_removed_land = int((~land_mask).sum())
        h3_gdf = h3_gdf[land_mask].copy()
        n_removed_water = 0
        if max_water_fraction is not None and "water_fraction" in h3_gdf.columns:
            water_mask = h3_gdf["water_fraction"] <= max_water_fraction
            n_removed_water = int((~water_mask).sum())
            h3_gdf = h3_gdf[water_mask].copy()
        n_removed_distance = 0
        if "distance_to_water_m" in h3_gdf.columns:
            distance_mask = h3_gdf["distance_to_water_m"] <= coastal_buffer_m
            n_removed_distance = int((~distance_mask).sum())
            h3_gdf = h3_gdf[distance_mask].copy()
        LOGGER.info(
            "Land source filter counts: candidates=%d removed_land_fraction=%d removed_water_fraction=%d removed_coastal_distance=%d final=%d",
            n_before_filters,
            n_removed_land,
            n_removed_water,
            n_removed_distance,
            len(h3_gdf),
        )

        h3_gdf = h3_gdf.drop(columns=["geometry"]).set_geometry("land_geometry")
        h3_gdf = h3_gdf.rename_geometry("geometry")
        if "water_geometry" in h3_gdf.columns:
            h3_gdf = h3_gdf.drop(columns=["water_geometry"])

    h3_gdf["h3_resolution"] = int(resolution)
    h3_gdf["run_version"] = get_run_version(raw)
    h3_gdf["config_hash"] = stable_config_hash(raw)
    h3_gdf.to_parquet(land_h3_path, index=False)
    return LandCellsResult(
        land_polygon_path=land_polygon_path,
        land_h3_path=land_h3_path,
        natural_earth_path=ne_path,
        n_land_features=len(land_clip),
        n_h3_cells=len(h3_gdf),
        h3_resolution=resolution,
    )


def validate_existing_land_h3_file(
    land_df,
    *,
    requested_resolution: int,
    path: Path | None = None,
) -> None:
    validate_land_h3_resolution(land_df, requested_resolution=requested_resolution, path=path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build land-intersecting H3 source cells for viewshed modeling."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--h3-resolution", type=int, default=None)
    args = parser.parse_args(argv)
    result = build_land_cells_for_config(
        args.config,
        overwrite=args.overwrite,
        h3_resolution=args.h3_resolution,
    )
    print(f"Wrote {result.n_h3_cells:,} land H3 cells -> {result.land_h3_path}")


if __name__ == "__main__":
    main()
