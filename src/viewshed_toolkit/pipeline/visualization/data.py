"""Data models, H3 conversion, aggregation, and map contexts for viewshed visualization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import polars as pl
from shapely.geometry import Polygon

from ..config import resolve_path
from ..config.loader import AppConfig
from ..contracts.artifacts import final_artifact_paths_from_raw


@dataclass(frozen=True)
class ViewshedMapConfig:
    """Display and smoothing parameters for one viewshed map export suite."""

    projected_crs: str = "EPSG:32610"
    output_crs: str = "EPSG:3857"
    colormap_name: str = "visibility_heat"
    display_quantile: float = 0.98
    analysis_pixel_size_m: float = 250.0
    output_pixel_size_m: float = 100.0
    gaussian_sigma_km: float = 1.5
    support_boundary_smoothing_km: float = 0.45
    raster_padding_km: float = 3.0
    h3_fill_opacity: float = 0.78
    smoothed_fill_opacity: float = 0.82
    selected_source_metric: str = "net_static_weight"
    visibility_class_count: int = 10
    externalize_map_assets: bool = True


@dataclass(frozen=True)
class _MapContext:
    target_gdf: gpd.GeoDataFrame
    source_gdf: gpd.GeoDataFrame
    target_footprint: Any
    source_footprint: Any
    analysis_footprint: Any
    center_lat: float
    center_lon: float
    source_selection_radius_km: float
    viewshed_radius_km: float


@dataclass(frozen=True)
class _SmoothingGrid:
    projected_crs: str
    transform: Any
    width: int
    height: int
    valid_mask: np.ndarray
    target_shapes: dict[str, Any]
    target_geometries: dict[str, Any]
    output_crs: str
    output_transform: Any
    output_width: int
    output_height: int
    output_valid_mask: np.ndarray
    bounds_wgs84: tuple[float, float, float, float]


@dataclass(frozen=True)
class _SmoothedMetricRender:
    """Display scale and generalized support produced for one smooth surface."""

    vmax: float
    support_geometry_wgs84: Any
    positive_cell_count: int


FACTOR_MAP_SPECS: dict[str, tuple[str, str]] = {
    "distance": ("distance_weight_sum", "Centroid-distance diagnostic sum"),
    "vegetation": ("vegetation_weight_sum", "Vegetation weight sum"),
    "terrain": ("terrain_weight_sum", "Terrain weight sum"),
    "combined": ("target_static_kernel_sum", "Static viewshed kernel sum"),
}


def _h3_polygon(cell: str) -> Polygon:
    return Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(str(cell))])


def _h3_geodataframe(frame: pd.DataFrame, h3_column: str) -> gpd.GeoDataFrame:
    out = frame.copy()
    out[h3_column] = out[h3_column].astype(str)
    out["geometry"] = out[h3_column].map(_h3_polygon)
    return gpd.GeoDataFrame(out, geometry="geometry", crs="EPSG:4326")


EdgeTable = pd.DataFrame | pl.DataFrame | pl.LazyFrame | str | Path


def _edge_lazy_frame(edges: EdgeTable) -> pl.LazyFrame:
    if isinstance(edges, pl.LazyFrame):
        return edges
    if isinstance(edges, pl.DataFrame):
        return edges.lazy()
    if isinstance(edges, pd.DataFrame):
        return pl.from_pandas(edges, include_index=False).lazy()
    path = Path(edges)
    if not path.is_file():
        raise FileNotFoundError(f"Viewshed edge table does not exist: {path}")
    return pl.scan_parquet(str(path))


def _selected_source_edges(edges: EdgeTable, source_h3: str) -> pd.DataFrame:
    """Collect only one source's candidate rows at the mapping boundary."""

    return (
        _edge_lazy_frame(edges)
        .filter(pl.col("source_h3").cast(pl.Utf8) == str(source_h3))
        .collect(engine="streaming")
        .to_pandas()
    )


def aggregate_target_weight_sums(edges: EdgeTable) -> pd.DataFrame:
    """Sum each pair factor across sources for every target H3 cell.

    Pair-level combined weight is the distance-integrated terrain kernel times
    conditional canopy transmission. Vegetation is summed only where the
    bare-earth terrain factor is positive; terrain-blocked pairs carry a neutral
    vegetation factor for multiplication but provide no vegetation opportunity
    to map. Only the source-to-target aggregation is a sum.
    """

    required = {
        "source_h3",
        "target_h3",
        "weight_distance",
        "weight_vegetation",
        "weight_terrain",
        "net_static_weight",
    }
    frame = _edge_lazy_frame(edges)
    missing = required - set(frame.collect_schema().names())
    if missing:
        raise ValueError(f"Viewshed edge table is missing map fields: {sorted(missing)}")
    return (
        frame.with_columns(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
            *[
                pl.col(column)
                .cast(pl.Float64, strict=False)
                .fill_nan(0.0)
                .fill_null(0.0)
                .alias(column)
                for column in (
                    "weight_distance",
                    "weight_vegetation",
                    "weight_terrain",
                    "net_static_weight",
                )
            ],
        )
        .with_columns(
            (pl.col("weight_terrain") > 0.0).alias("terrain_visible"),
            (pl.col("net_static_weight") > 0.0).alias("combined_visible"),
            pl.when(pl.col("weight_terrain") > 0.0)
            .then(pl.col("weight_vegetation"))
            .otherwise(0.0)
            .alias("vegetation_weight_with_terrain_opportunity"),
        )
        .group_by("target_h3")
        .agg(
            pl.col("weight_distance").sum().alias("distance_weight_sum"),
            pl.col("vegetation_weight_with_terrain_opportunity")
            .sum()
            .alias("vegetation_weight_sum"),
            pl.col("weight_terrain").sum().alias("terrain_weight_sum"),
            pl.col("net_static_weight").sum().alias("target_static_kernel_sum"),
            pl.col("source_h3").n_unique().alias("candidate_source_count"),
            pl.col("terrain_visible").sum().alias("terrain_visible_source_count"),
            pl.col("combined_visible").sum().alias("combined_visible_source_count"),
        )
        .sort("target_h3")
        .collect(engine="streaming")
        .to_pandas()
    )


def select_source_for_viewshed_map(
    edges: EdgeTable,
    source_cells: pd.DataFrame,
    requested_source_h3: str | None = None,
) -> str:
    """Choose an explicit source or the source with most final visible targets."""

    available = source_cells["h3_cell"].astype(str).tolist()
    if not available:
        raise ValueError("No selected source H3 cells are available for mapping.")
    if requested_source_h3 is not None:
        requested_source_h3 = str(requested_source_h3)
        if requested_source_h3 not in set(available):
            raise ValueError(
                f"Requested map source {requested_source_h3} is not selected; "
                f"available={available}"
            )
        return requested_source_h3

    frame = _edge_lazy_frame(edges)
    columns = set(frame.collect_schema().names())
    visibility_column = "weight_canopy_los" if "weight_canopy_los" in columns else "weight_terrain"
    counts = (
        frame.filter(
            pl.col(visibility_column).cast(pl.Float64, strict=False).fill_nan(0.0).fill_null(0.0)
            > 0.0
        )
        .group_by(pl.col("source_h3").cast(pl.Utf8).alias("source_h3"))
        .agg(pl.col("target_h3").n_unique().alias("visible_target_count"))
        .sort(
            ["visible_target_count", "source_h3"],
            descending=[True, False],
        )
        .limit(1)
        .collect(engine="streaming")
    )
    if counts.height == 0:
        return available[0]
    return str(counts.item(0, "source_h3"))


def _build_map_context(
    aggregate: pd.DataFrame,
    source_cells: gpd.GeoDataFrame,
    *,
    center_lat: float,
    center_lon: float,
    source_selection_radius_km: float,
    viewshed_radius_km: float,
    projected_crs: str,
) -> _MapContext:
    target_gdf = _h3_geodataframe(aggregate, "target_h3")
    source_gdf = source_cells.copy().to_crs("EPSG:4326")
    if "h3_cell" not in source_gdf.columns:
        raise ValueError("Source-cell GeoDataFrame must contain h3_cell.")
    source_gdf["h3_cell"] = source_gdf["h3_cell"].astype(str)
    target_footprint = target_gdf.geometry.union_all()
    source_footprint = source_gdf.geometry.union_all()

    projected_centroids = source_gdf.to_crs(projected_crs).geometry.centroid
    projected_buffers = projected_centroids.buffer(float(viewshed_radius_km) * 1_000.0)
    analysis_footprint = gpd.GeoSeries(projected_buffers, crs=projected_crs).union_all()
    analysis_footprint = (
        gpd.GeoSeries([analysis_footprint], crs=projected_crs).to_crs("EPSG:4326").iloc[0]
    )
    return _MapContext(
        target_gdf=target_gdf,
        source_gdf=source_gdf,
        target_footprint=target_footprint,
        source_footprint=source_footprint,
        analysis_footprint=analysis_footprint,
        center_lat=float(center_lat),
        center_lon=float(center_lon),
        source_selection_radius_km=float(source_selection_radius_km),
        viewshed_radius_km=float(viewshed_radius_km),
    )


@dataclass(frozen=True)
class SelectedLocation:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class StaticMapSettings:
    enabled: bool
    selected_location: SelectedLocation
    output_dir: Path
    map_config: ViewshedMapConfig


@dataclass(frozen=True)
class StaticMapOutputPaths:
    output_dir: Path
    selected_html: Path
    land_aggregate_html: Path
    water_aggregate_html: Path
    manifest: Path
    selected_values: Path
    land_target_aggregate_values: Path
    land_source_aggregate_values: Path
    water_target_aggregate_values: Path
    water_source_aggregate_values: Path


@dataclass(frozen=True)
class _FactorSpec:
    slug: str
    selected_metric: str
    target_metric: str
    source_metric: str
    caption: str


FACTOR_SPECS = (
    _FactorSpec(
        "distance",
        "weight_distance",
        "distance_weight_sum",
        "distance_weight_sum",
        "Distance diagnostic",
    ),
    _FactorSpec(
        "vegetation",
        "vegetation_supported_weight",
        "vegetation_weight_sum",
        "vegetation_weight_sum",
        "Conditional vegetation support",
    ),
    _FactorSpec(
        "terrain",
        "weight_terrain",
        "terrain_weight_sum",
        "terrain_weight_sum",
        "Terrain support",
    ),
    _FactorSpec(
        "combined",
        "net_static_weight",
        "target_static_kernel_sum",
        "source_static_kernel_sum",
        "Combined static weight",
    ),
)


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping.")
    return value


def static_map_settings(app: AppConfig) -> StaticMapSettings:
    """Parse and validate the ``static_maps`` section of the canonical config."""

    section = _mapping(app.raw_config.get("static_maps", {}), label="static_maps")
    selected = _mapping(
        section.get("selected_location", {}),
        label="static_maps.selected_location",
    )
    missing = [key for key in ("latitude", "longitude") if key not in selected]
    if missing:
        raise KeyError("static_maps.selected_location is missing: " + ", ".join(missing))
    latitude = float(selected["latitude"])
    longitude = float(selected["longitude"])
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("static_maps.selected_location.latitude must be in [-90, 90].")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("static_maps.selected_location.longitude must be in [-180, 180].")

    output_value = section.get("output_dir")
    if output_value is not None:
        output_dir = resolve_path(output_value, app.config_path.parent)
    else:
        resolution_segment = f"h3r{int(app.h3.source_resolution)}"
        map_root = app.paths.map_dir
        output_dir = (
            map_root
            if map_root.name == "static" and map_root.parent.name == resolution_segment
            else map_root / resolution_segment / "static"
        )
    map_config = ViewshedMapConfig(
        projected_crs=str(section.get("projected_crs", app.viewshed.crs_projected)),
        output_crs=str(section.get("output_crs", "EPSG:3857")),
        colormap_name=str(section.get("colormap_name", "visibility_heat")),
        display_quantile=float(section.get("display_quantile", 0.98)),
        analysis_pixel_size_m=float(section.get("analysis_pixel_size_m", 250.0)),
        output_pixel_size_m=float(section.get("output_pixel_size_m", 100.0)),
        gaussian_sigma_km=float(section.get("gaussian_sigma_km", 1.5)),
        support_boundary_smoothing_km=float(section.get("support_boundary_smoothing_km", 0.45)),
        raster_padding_km=float(section.get("raster_padding_km", 3.0)),
        h3_fill_opacity=float(section.get("h3_fill_opacity", 0.78)),
        smoothed_fill_opacity=float(section.get("smoothed_fill_opacity", 0.82)),
        externalize_map_assets=bool(section.get("externalize_map_assets", True)),
    )
    if not 0.0 < map_config.display_quantile <= 1.0:
        raise ValueError("static_maps.display_quantile must be in (0, 1].")
    if map_config.analysis_pixel_size_m <= 0 or map_config.output_pixel_size_m <= 0:
        raise ValueError("Static-map pixel sizes must be positive.")
    if map_config.gaussian_sigma_km < 0:
        raise ValueError("static_maps.gaussian_sigma_km must be non-negative.")
    return StaticMapSettings(
        enabled=bool(section.get("enabled", True)),
        selected_location=SelectedLocation(latitude, longitude),
        output_dir=output_dir,
        map_config=map_config,
    )


def static_map_output_paths(app: AppConfig) -> StaticMapOutputPaths:
    output_dir = static_map_settings(app).output_dir
    return StaticMapOutputPaths(
        output_dir=output_dir,
        selected_html=output_dir / "land_source_selected_location_static_weights.html",
        land_aggregate_html=output_dir / "land_source_aggregate_static_weights.html",
        water_aggregate_html=output_dir / "water_source_aggregate_static_weights.html",
        manifest=output_dir / "static_weight_map_manifest.json",
        selected_values=output_dir / "land_source_selected_location_weights.parquet",
        land_target_aggregate_values=output_dir / "land_source_aggregate_target_weights.parquet",
        land_source_aggregate_values=output_dir / "land_source_aggregate_source_weights.parquet",
        water_target_aggregate_values=output_dir / "water_source_aggregate_target_weights.parquet",
        water_source_aggregate_values=output_dir / "water_source_aggregate_source_weights.parquet",
    )


def _static_edges(app: AppConfig, *, source_type: str) -> pl.LazyFrame:
    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be 'land' or 'water'.")
    artifacts = final_artifact_paths_from_raw(
        app.raw_config,
        app.config_path.parent,
    )
    path = (
        artifacts.land_static_weights if source_type == "land" else artifacts.water_static_weights
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"Finalized {source_type} static weights are required for map export: "
            f"{path}. Run finalize-viewshed-lookups first."
        )
    frame = pl.scan_parquet(str(path))
    required = {
        "source_h3",
        "target_h3",
        "weight_distance",
        "weight_vegetation",
        "weight_terrain",
        "weight_static_viewability",
    }
    missing = sorted(required - set(frame.collect_schema().names()))
    if missing:
        raise ValueError(f"Static viewshed artifact is missing map column(s): {missing}: {path}")
    return (
        frame.select(sorted(required))
        .with_columns(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
            *[
                pl.col(column)
                .cast(pl.Float64, strict=False)
                .fill_nan(0.0)
                .fill_null(0.0)
                .clip(0.0, 1.0)
                for column in (
                    "weight_distance",
                    "weight_vegetation",
                    "weight_terrain",
                    "weight_static_viewability",
                )
            ],
        )
        .with_columns(
            pl.when(pl.col("weight_terrain") > 0.0)
            .then(pl.col("weight_vegetation"))
            .otherwise(0.0)
            .alias("vegetation_supported_weight"),
            pl.col("weight_static_viewability").alias("net_static_weight"),
        )
    )


def _aggregate_edges(edges: pl.LazyFrame, *, by: str) -> pl.DataFrame:
    if by not in {"source_h3", "target_h3"}:
        raise ValueError("Static map aggregation must use source_h3 or target_h3.")
    combined_name = "source_static_kernel_sum" if by == "source_h3" else "target_static_kernel_sum"
    return (
        edges.group_by(by)
        .agg(
            pl.col("weight_distance").sum().alias("distance_weight_sum"),
            pl.col("vegetation_supported_weight").sum().alias("vegetation_weight_sum"),
            pl.col("weight_terrain").sum().alias("terrain_weight_sum"),
            pl.col("net_static_weight").sum().alias(combined_name),
            pl.len().alias("pair_count"),
            (pl.col("weight_terrain") > 0.0).sum().alias("terrain_supported_pair_count"),
        )
        .sort(by)
        .collect(engine="streaming")
    )


def _source_cells(edges: pl.LazyFrame) -> gpd.GeoDataFrame:
    frame = (
        edges.select(pl.col("source_h3").unique().sort())
        .collect(engine="streaming")
        .rename({"source_h3": "h3_cell"})
        .to_pandas()
    )
    return _h3_geodataframe(frame, "h3_cell")


def _build_context(
    frame: pd.DataFrame,
    source_cells: gpd.GeoDataFrame,
    *,
    center: SelectedLocation,
    app: AppConfig,
    config: ViewshedMapConfig,
) -> Any:
    return _build_map_context(
        frame,
        source_cells,
        center_lat=center.latitude,
        center_lon=center.longitude,
        source_selection_radius_km=0.0,
        viewshed_radius_km=float(app.viewshed.max_distance_m) / 1_000.0,
        projected_crs=config.projected_crs,
    )
