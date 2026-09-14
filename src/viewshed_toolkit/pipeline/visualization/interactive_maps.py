"""Folium layers and interactive HTML writers for viewshed visualization."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import branca.colormap as bcm
import folium
import geopandas as gpd
import h3
import pandas as pd
from shapely.geometry import mapping

from ..config.loader import AppConfig
from .data import (
    FACTOR_SPECS,
    StaticMapOutputPaths,
    StaticMapSettings,
    ViewshedMapConfig,
    _build_context,
    _h3_geodataframe,
    _MapContext,
    _SmoothingGrid,
)
from .static_maps import _build_smoothing_grid, _render_smoothed_metric
from .styling import (
    _add_map_controls,
    _display_max,
    _generalized_visibility_legend,
    _map_colors,
    _map_note,
    add_click_to_copy_coordinates,
)


@dataclass(frozen=True)
class _GeoJsonAsset:
    """One serialized geometry payload shared by multiple map pages."""

    path: Path
    data: dict[str, Any]


@dataclass(frozen=True)
class _MapGeometryAssets:
    """Named shared GeoJSON assets emitted beside a map export suite."""

    directory: Path
    by_name: dict[str, _GeoJsonAsset]

    def get(self, name: str) -> _GeoJsonAsset | None:
        return self.by_name.get(name)


def _add_h3_value_hover(
    map_: folium.Map,
    frame: gpd.GeoDataFrame,
    *,
    metric: str,
    caption: str,
    layer_name: str = "H3 grid values (hover)",
    output_path: Path,
    asset: _GeoJsonAsset | None = None,
) -> None:
    """Add an invisible interactive H3 layer exposing the mapped grid value."""

    if metric not in frame.columns or "target_h3" not in frame.columns:
        raise KeyError(f"H3 hover layer requires target_h3 and {metric}.")
    hover_frame = frame[["target_h3", metric, "geometry"]].copy()
    hover_frame[metric] = pd.to_numeric(hover_frame[metric], errors="coerce").fillna(0.0)
    _geojson_layer(
        hover_frame,
        output_path=output_path,
        asset=asset,
        name=layer_name,
        show=True,
        style_function=lambda _: {
            "color": "transparent",
            "weight": 0,
            "fillColor": "transparent",
            "fillOpacity": 0.001,
        },
        highlight_function=lambda _: {"color": "#111111", "weight": 1.5},
        tooltip=folium.GeoJsonTooltip(
            fields=["target_h3", metric],
            aliases=["Target H3", f"Grid value: {caption}"],
            localize=True,
            sticky=True,
        ),
    ).add_to(map_)


def _geojson_feature_collection(geometry: Any) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": mapping(geometry),
            }
        ],
    }


def _geojson_data(value: gpd.GeoDataFrame | Any) -> dict[str, Any]:
    if isinstance(value, gpd.GeoDataFrame):
        return json.loads(value.to_json(drop_id=True))
    return _geojson_feature_collection(value)


def _write_geojson_asset(
    value: gpd.GeoDataFrame | Any,
    path: Path,
) -> _GeoJsonAsset:
    data = _geojson_data(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    return _GeoJsonAsset(path=path.resolve(), data=data)


def _asset_url(asset_path: Path, output_path: Path) -> str:
    """Return a portable URL from one HTML page to its adjacent asset."""

    return Path(
        os.path.relpath(
            Path(asset_path).resolve(),
            Path(output_path).resolve().parent,
        )
    ).as_posix()


def _geojson_layer(
    data: gpd.GeoDataFrame | dict[str, Any] | Any,
    *,
    output_path: Path,
    asset: _GeoJsonAsset | None = None,
    **kwargs: Any,
) -> folium.GeoJson:
    """Create an embedded layer or reference a shared external GeoJSON asset."""

    layer_data = asset.data if asset is not None else data
    layer = folium.GeoJson(layer_data, **kwargs)
    if asset is not None:
        # Folium needs the in-memory features above to compile Python style
        # functions, but the rendered page can load the geometry once from the
        # shared file instead of embedding coordinates into every HTML page.
        layer.embed = False
        layer.embed_link = _asset_url(asset.path, output_path)
    return layer


def _image_overlay(
    image_path: Path,
    *,
    output_path: Path,
    externalize: bool,
    **kwargs: Any,
) -> folium.raster_layers.ImageOverlay:
    """Create an image overlay without base64-duplicating an exported PNG."""

    if not externalize:
        return folium.raster_layers.ImageOverlay(image=str(image_path), **kwargs)
    overlay = folium.raster_layers.ImageOverlay(
        image="https://invalid.local/orcacast-map-asset.png",
        **kwargs,
    )
    asset_path = Path(image_path)
    asset_url = _asset_url(asset_path, output_path)
    # Map exports deliberately reuse stable filenames. Version the reference so
    # an already-open browser cannot keep displaying a PNG from an earlier run.
    if asset_path.exists():
        asset_url = f"{asset_url}?v={asset_path.stat().st_mtime_ns}"
    overlay.url = asset_url
    return overlay


def _shared_map_geometry_assets(
    context: _MapContext,
    selected_edges: pd.DataFrame,
    *,
    source_h3: str,
    output_dir: Path,
) -> _MapGeometryAssets:
    """Serialize geometry reused across the factor and selected-source maps."""

    asset_dir = Path(output_dir) / "assets"
    source_columns = ["h3_cell", "geometry"]
    selected_columns = [
        column
        for column in (
            "source_h3",
            "target_h3",
            "weight_terrain",
            "weight_canopy_los",
            "weight_distance",
            "weight_vegetation",
            "net_static_weight",
        )
        if column in selected_edges.columns
    ]
    selected_values = _h3_geodataframe(
        selected_edges[selected_columns].copy(),
        "target_h3",
    )
    selected_source = context.source_gdf.loc[
        context.source_gdf["h3_cell"].astype(str).eq(str(source_h3)),
        source_columns,
    ].copy()
    assets = {
        "target_h3_values": _write_geojson_asset(
            context.target_gdf,
            asset_dir / "target_h3_values.geojson",
        ),
        "target_footprint": _write_geojson_asset(
            context.target_footprint,
            asset_dir / "target_footprint.geojson",
        ),
        "analysis_footprint": _write_geojson_asset(
            context.analysis_footprint,
            asset_dir / "analysis_footprint.geojson",
        ),
        "source_h3_cells": _write_geojson_asset(
            context.source_gdf[source_columns].copy(),
            asset_dir / "source_h3_cells.geojson",
        ),
        "selected_candidate_values": _write_geojson_asset(
            selected_values,
            asset_dir / f"selected_{source_h3}_candidate_values.geojson",
        ),
        "selected_candidate_footprint": _write_geojson_asset(
            selected_values.geometry.union_all(),
            asset_dir / f"selected_{source_h3}_candidate_footprint.geojson",
        ),
        "selected_source_h3": _write_geojson_asset(
            selected_source,
            asset_dir / f"selected_{source_h3}_source.geojson",
        ),
    }
    terrain_values = pd.to_numeric(
        selected_values.get("weight_terrain"),
        errors="coerce",
    ).fillna(0.0)
    terrain_visible = selected_values.loc[terrain_values > 0.0].copy()
    if not terrain_visible.empty:
        assets["selected_terrain_visible_values"] = _write_geojson_asset(
            terrain_visible,
            asset_dir / f"selected_{source_h3}_terrain_visible_values.geojson",
        )
        assets["selected_terrain_footprint"] = _write_geojson_asset(
            terrain_visible.geometry.union_all(),
            asset_dir / f"selected_{source_h3}_terrain_footprint.geojson",
        )
    if "weight_canopy_los" in selected_values.columns:
        canopy_values = pd.to_numeric(
            selected_values["weight_canopy_los"],
            errors="coerce",
        ).fillna(0.0)
        canopy_visible = selected_values.loc[canopy_values > 0.0].copy()
        if not canopy_visible.empty:
            assets["selected_canopy_visible_values"] = _write_geojson_asset(
                canopy_visible,
                asset_dir / f"selected_{source_h3}_canopy_visible_values.geojson",
            )
            assets["selected_canopy_footprint"] = _write_geojson_asset(
                canopy_visible.geometry.union_all(),
                asset_dir / f"selected_{source_h3}_canopy_footprint.geojson",
            )
    return _MapGeometryAssets(directory=asset_dir.resolve(), by_name=assets)


def _add_context_layers(
    map_: folium.Map,
    context: _MapContext,
    *,
    output_path: Path,
    assets: _MapGeometryAssets | None = None,
) -> None:
    _geojson_layer(
        mapping(context.target_footprint),
        output_path=output_path,
        asset=None if assets is None else assets.get("target_footprint"),
        name="Modeled H3 target footprint",
        show=True,
        style_function=lambda _: {
            "color": "#222222",
            "weight": 1.4,
            "fillOpacity": 0.0,
            "dashArray": "5 4",
        },
    ).add_to(map_)
    _geojson_layer(
        mapping(context.analysis_footprint),
        output_path=output_path,
        asset=None if assets is None else assets.get("analysis_footprint"),
        name="Union of source viewshed radii",
        show=False,
        style_function=lambda _: {
            "color": "#6A3D9A",
            "weight": 1.8,
            "fillColor": "#6A3D9A",
            "fillOpacity": 0.04,
            "dashArray": "8 5",
        },
    ).add_to(map_)
    _geojson_layer(
        context.source_gdf,
        output_path=output_path,
        asset=None if assets is None else assets.get("source_h3_cells"),
        name="Selected source H3 cells",
        show=True,
        style_function=lambda _: {
            "color": "#111111",
            "weight": 1.6,
            "fillColor": "#FFFFFF",
            "fillOpacity": 0.04,
        },
        tooltip=folium.GeoJsonTooltip(fields=["h3_cell"], aliases=["Source H3"]),
    ).add_to(map_)

    parameter_layer = folium.FeatureGroup(name="Selected point and parameter radii", show=True)
    folium.CircleMarker(
        [context.center_lat, context.center_lon],
        radius=5,
        color="#111111",
        fill=True,
        fill_color="#FFFFFF",
        fill_opacity=1.0,
        weight=2,
        tooltip="Experiment center",
    ).add_to(parameter_layer)
    folium.Circle(
        [context.center_lat, context.center_lon],
        radius=context.source_selection_radius_km * 1_000.0,
        color="#E31A1C",
        weight=2,
        fill=False,
        tooltip=f"Source-selection radius: {context.source_selection_radius_km:g} km",
    ).add_to(parameter_layer)
    folium.Circle(
        [context.center_lat, context.center_lon],
        radius=context.viewshed_radius_km * 1_000.0,
        color="#6A3D9A",
        weight=2,
        fill=False,
        dash_array="8 5",
    ).add_to(parameter_layer)
    parameter_layer.add_to(map_)


def _add_selected_source_context_layers(
    map_: folium.Map,
    edges: pd.DataFrame,
    context: _MapContext,
    *,
    source_h3: str,
    visibility_metric: str,
    modeled_footprint_show: bool = True,
    output_path: Path,
    assets: _MapGeometryAssets | None = None,
) -> gpd.GeoDataFrame:
    """Add the selected H3, candidate/visible footprints, point, and radius."""

    source_h3 = str(source_h3)
    selected = edges.loc[edges["source_h3"].astype(str).eq(source_h3)].copy()
    if selected.empty:
        raise ValueError(f"No candidate pairs exist for selected source {source_h3}.")
    candidate_gdf = _h3_geodataframe(selected, "target_h3")
    _geojson_layer(
        mapping(candidate_gdf.geometry.union_all()),
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_candidate_footprint"),
        name="Selected-source candidate footprint",
        show=False,
        style_function=lambda _: {
            "color": "#666666",
            "weight": 1.2,
            "fillOpacity": 0.0,
            "dashArray": "5 4",
        },
    ).add_to(map_)

    visibility = pd.to_numeric(selected[visibility_metric], errors="coerce").fillna(0.0)
    visible = selected.loc[visibility > 0.0].copy()
    if not visible.empty:
        visible_gdf = _h3_geodataframe(visible, "target_h3")
        _geojson_layer(
            mapping(visible_gdf.geometry.union_all()),
            output_path=output_path,
            asset=(
                None
                if assets is None
                else (
                    assets.get("selected_canopy_footprint")
                    if visibility_metric == "weight_canopy_los"
                    else assets.get("selected_terrain_footprint")
                )
            ),
            name="Selected-source modeled viewshed footprint",
            show=modeled_footprint_show,
            style_function=lambda _: {
                "color": "#111111",
                "weight": 1.8,
                "fillOpacity": 0.0,
            },
        ).add_to(map_)

    selected_source = context.source_gdf.loc[
        context.source_gdf["h3_cell"].astype(str).eq(source_h3)
    ]
    _geojson_layer(
        selected_source,
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_source_h3"),
        name="Selected source H3",
        show=True,
        style_function=lambda _: {
            "color": "#111111",
            "weight": 2.4,
            "fillColor": "#FFFFFF",
            "fillOpacity": 0.08,
        },
        tooltip=folium.GeoJsonTooltip(fields=["h3_cell"], aliases=["Source H3"]),
    ).add_to(map_)
    source_lat, source_lon = h3.cell_to_latlng(source_h3)
    selected_layer = folium.FeatureGroup(
        name="Selected source point and viewshed radius", show=True
    )
    folium.CircleMarker(
        [source_lat, source_lon],
        radius=6,
        color="#111111",
        fill=True,
        fill_color="#FFFFFF",
        fill_opacity=1.0,
        weight=2,
        tooltip=f"Selected source: {source_h3}",
    ).add_to(selected_layer)
    folium.Circle(
        [source_lat, source_lon],
        radius=context.viewshed_radius_km * 1_000.0,
        color="#6A3D9A",
        weight=2,
        fill=False,
        dash_array="8 5",
    ).add_to(selected_layer)
    _add_h3_value_hover(
        map_,
        candidate_gdf,
        metric=visibility_metric,
        caption=visibility_metric.replace("_", " "),
        layer_name="Selected-source H3 values (hover)",
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_candidate_values"),
    )
    selected_layer.add_to(map_)
    return candidate_gdf


def _add_generalized_support_layer(
    map_: folium.Map,
    geometry_wgs84: Any,
    *,
    name: str,
) -> None:
    """Draw the non-expanding generalized boundary used to mask a smooth surface."""

    folium.GeoJson(
        mapping(geometry_wgs84),
        name=name,
        show=True,
        style_function=lambda _: {
            "color": "#111111",
            "weight": 1.8,
            "fillOpacity": 0.0,
        },
    ).add_to(map_)


def _fit_target_bounds(map_: folium.Map, target_gdf: gpd.GeoDataFrame) -> None:
    min_lon, min_lat, max_lon, max_lat = target_gdf.total_bounds
    map_.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(18, 18))


def write_target_h3_weight_map(
    context: _MapContext,
    *,
    metric: str,
    caption: str,
    output_path: Path,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> Path:
    """Write one native-H3 target factor map with shared footprint context."""

    frame = context.target_gdf.copy()
    values = pd.to_numeric(frame[metric], errors="coerce").fillna(0.0)
    vmax = _display_max(values, config.display_quantile)
    colors = bcm.LinearColormap(_map_colors(config), vmin=0.0, vmax=vmax, caption=caption)
    properties = ["target_h3", metric]

    def style(feature: dict) -> dict:
        value = float(feature["properties"].get(metric) or 0.0)
        return {
            "fillColor": colors(min(max(value, 0.0), vmax)),
            "color": "#4A4A4A",
            "weight": 0.35,
            "fillOpacity": config.h3_fill_opacity if value > 0.0 else 0.0,
        }

    map_ = folium.Map(
        location=[context.center_lat, context.center_lon],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    _geojson_layer(
        frame,
        output_path=output_path,
        asset=None if assets is None else assets.get("target_h3_values"),
        name=caption,
        style_function=style,
        highlight_function=lambda _: {"color": "#111111", "weight": 2.0},
        tooltip=folium.GeoJsonTooltip(
            fields=properties,
            aliases=["Target H3", f"Grid value: {caption}"],
            localize=True,
        ),
    ).add_to(map_)
    _add_context_layers(
        map_,
        context,
        output_path=output_path,
        assets=assets,
    )
    colors.add_to(map_)
    _add_map_controls(map_)
    _fit_target_bounds(map_, frame)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_.save(output_path)
    return output_path


def write_selected_h3_viewshed_map(
    edges: pd.DataFrame,
    context: _MapContext,
    *,
    source_h3: str,
    output_path: Path,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> Path:
    """Write a selected-source map with actual terrain-visible H3 cells."""

    source_h3 = str(source_h3)
    candidates = edges.loc[edges["source_h3"].astype(str).eq(source_h3)].copy()
    if candidates.empty:
        raise ValueError(f"No candidate pairs exist for selected source {source_h3}.")
    candidates["weight_terrain"] = pd.to_numeric(
        candidates["weight_terrain"], errors="coerce"
    ).fillna(0.0)
    visible = candidates.loc[candidates["weight_terrain"] > 0.0].copy()
    canopy_visible = None
    if "weight_canopy_los" in candidates.columns:
        candidates["weight_canopy_los"] = pd.to_numeric(
            candidates["weight_canopy_los"], errors="coerce"
        ).fillna(0.0)
        canopy_visible = candidates.loc[candidates["weight_canopy_los"] > 0.0].copy()
    candidate_gdf = _h3_geodataframe(candidates, "target_h3")
    visible_gdf = _h3_geodataframe(visible, "target_h3") if not visible.empty else None

    values = (
        pd.to_numeric(visible_gdf["weight_terrain"], errors="coerce").fillna(0.0)
        if visible_gdf is not None
        else pd.Series(dtype=float)
    )
    vmax = _display_max(values, config.display_quantile)
    colors = bcm.LinearColormap(
        _map_colors(config), vmin=0.0, vmax=vmax, caption="Selected-source terrain weight"
    )
    source_lat, source_lon = h3.cell_to_latlng(source_h3)
    map_ = folium.Map(
        location=[source_lat, source_lon],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    _geojson_layer(
        mapping(candidate_gdf.geometry.union_all()),
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_candidate_footprint"),
        name="Selected-source candidate footprint",
        style_function=lambda _: {
            "color": "#666666",
            "weight": 1.2,
            "fillOpacity": 0.0,
            "dashArray": "5 4",
        },
    ).add_to(map_)
    if visible_gdf is not None:

        def visible_style(feature: dict) -> dict:
            value = float(feature["properties"].get("weight_terrain") or 0.0)
            return {
                "fillColor": colors(min(max(value, 0.0), vmax)),
                "color": "#444444",
                "weight": 0.4,
                "fillOpacity": config.h3_fill_opacity,
            }

        _geojson_layer(
            visible_gdf,
            output_path=output_path,
            asset=None if assets is None else assets.get("selected_terrain_visible_values"),
            name="Bare-earth terrain-visible H3 cells",
            show=canopy_visible is None,
            style_function=visible_style,
            tooltip=folium.GeoJsonTooltip(
                fields=[
                    "target_h3",
                    "weight_terrain",
                    "weight_distance",
                    "weight_vegetation",
                    "net_static_weight",
                ],
                aliases=[
                    "Target H3",
                    "Terrain weight",
                    "Distance weight",
                    "Vegetation weight",
                    "Combined weight",
                ],
                localize=True,
            ),
        ).add_to(map_)
        _geojson_layer(
            mapping(visible_gdf.geometry.union_all()),
            output_path=output_path,
            asset=None if assets is None else assets.get("selected_terrain_footprint"),
            name="Bare-earth terrain footprint",
            show=canopy_visible is None,
            style_function=lambda _: {
                "color": "#111111",
                "weight": 1.8,
                "fillOpacity": 0.0,
            },
        ).add_to(map_)

    if canopy_visible is not None and not canopy_visible.empty:
        canopy_gdf = _h3_geodataframe(canopy_visible, "target_h3")

        def canopy_style(feature: dict) -> dict:
            value = float(feature["properties"].get("weight_canopy_los") or 0.0)
            return {
                "fillColor": colors(min(max(value, 0.0), vmax)),
                "color": "#222222",
                "weight": 0.5,
                "fillOpacity": config.h3_fill_opacity,
            }

        _geojson_layer(
            canopy_gdf,
            output_path=output_path,
            asset=None if assets is None else assets.get("selected_canopy_visible_values"),
            name="Canopy-constrained visible H3 cells",
            show=True,
            style_function=canopy_style,
            tooltip=folium.GeoJsonTooltip(
                fields=[
                    "target_h3",
                    "weight_terrain",
                    "weight_canopy_los",
                    "weight_vegetation",
                    "weight_distance",
                    "net_static_weight",
                ],
                aliases=[
                    "Target H3",
                    "Bare-earth terrain",
                    "Canopy LOS",
                    "Conditional canopy factor",
                    "Distance weight",
                    "Combined weight",
                ],
                localize=True,
            ),
        ).add_to(map_)
        _geojson_layer(
            mapping(canopy_gdf.geometry.union_all()),
            output_path=output_path,
            asset=None if assets is None else assets.get("selected_canopy_footprint"),
            name="Actual canopy-constrained viewshed footprint",
            show=True,
            style_function=lambda _: {
                "color": "#111111",
                "weight": 1.8,
                "fillOpacity": 0.0,
            },
        ).add_to(map_)

    hover_metric = (
        "weight_canopy_los"
        if canopy_visible is not None and "weight_canopy_los" in candidate_gdf.columns
        else "weight_terrain"
    )
    _add_h3_value_hover(
        map_,
        candidate_gdf,
        metric=hover_metric,
        caption=hover_metric.replace("_", " "),
        layer_name="Selected-source H3 values (hover)",
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_candidate_values"),
    )

    selected_layer = folium.FeatureGroup(name="Selected source point and radius", show=True)
    folium.CircleMarker(
        [source_lat, source_lon],
        radius=6,
        color="#111111",
        fill=True,
        fill_color="#FFFFFF",
        fill_opacity=1.0,
        weight=2,
        tooltip=f"Selected source: {source_h3}",
    ).add_to(selected_layer)
    folium.Circle(
        [source_lat, source_lon],
        radius=context.viewshed_radius_km * 1_000.0,
        color="#6A3D9A",
        weight=2,
        fill=False,
        dash_array="8 5",
    ).add_to(selected_layer)
    selected_layer.add_to(map_)
    _geojson_layer(
        context.source_gdf.loc[context.source_gdf["h3_cell"].eq(source_h3)],
        output_path=output_path,
        asset=None if assets is None else assets.get("selected_source_h3"),
        name="Selected source H3",
        style_function=lambda _: {
            "color": "#111111",
            "weight": 2.4,
            "fillColor": "#FFFFFF",
            "fillOpacity": 0.08,
        },
    ).add_to(map_)
    colors.add_to(map_)
    _add_map_controls(map_)
    _fit_target_bounds(map_, candidate_gdf)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_.save(output_path)
    return output_path


def write_smoothed_weight_map(
    context: _MapContext,
    grid: _SmoothingGrid,
    *,
    png_path: Path,
    tif_path: Path,
    output_path: Path,
    metric: str,
    caption: str,
    vmax: float,
    support_geometry_wgs84: Any,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> Path:
    """Write a Folium ImageOverlay map for one smoothed factor surface."""

    west, south, east, north = grid.bounds_wgs84
    colors = bcm.LinearColormap(_map_colors(config), vmin=0.0, vmax=vmax, caption=caption)
    map_ = folium.Map(
        location=[context.center_lat, context.center_lon],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    _image_overlay(
        png_path,
        output_path=output_path,
        externalize=bool(config.externalize_map_assets),
        bounds=[[south, west], [north, east]],
        name=f"Smoothed {caption}",
        opacity=1.0,
        interactive=True,
        cross_origin=False,
        zindex=2,
        show=True,
    ).add_to(map_)
    _add_generalized_support_layer(
        map_,
        support_geometry_wgs84,
        name="Generalized positive-weight extent",
    )
    _add_context_layers(
        map_,
        context,
        output_path=output_path,
        assets=assets,
    )
    _add_h3_value_hover(
        map_,
        context.target_gdf,
        metric=metric,
        caption=caption,
        output_path=output_path,
        asset=None if assets is None else assets.get("target_h3_values"),
    )
    colors.add_to(map_)
    _add_map_controls(map_)
    _fit_target_bounds(map_, context.target_gdf)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_.save(output_path)
    return output_path


def write_selected_source_smoothed_map(
    edges: pd.DataFrame,
    context: _MapContext,
    grid: _SmoothingGrid,
    *,
    source_h3: str,
    visibility_metric: str,
    png_path: Path,
    output_path: Path,
    caption: str,
    vmax: float,
    support_geometry_wgs84: Any,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> Path:
    """Write the continuous smoothed final viewshed for one source H3."""

    west, south, east, north = grid.bounds_wgs84
    source_lat, source_lon = h3.cell_to_latlng(str(source_h3))
    colors = bcm.LinearColormap(_map_colors(config), vmin=0.0, vmax=vmax, caption=caption)
    map_ = folium.Map(
        location=[source_lat, source_lon],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    _image_overlay(
        png_path,
        output_path=output_path,
        externalize=bool(config.externalize_map_assets),
        bounds=[[south, west], [north, east]],
        name=caption,
        opacity=1.0,
        interactive=True,
        cross_origin=False,
        zindex=2,
        show=True,
    ).add_to(map_)
    _add_generalized_support_layer(
        map_,
        support_geometry_wgs84,
        name="Generalized smoothed viewshed extent",
    )
    candidate_gdf = _add_selected_source_context_layers(
        map_,
        edges,
        context,
        source_h3=source_h3,
        visibility_metric=visibility_metric,
        modeled_footprint_show=False,
        output_path=output_path,
        assets=assets,
    )
    colors.add_to(map_)
    _add_map_controls(map_)
    _fit_target_bounds(map_, candidate_gdf)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_.save(output_path)
    return output_path


def write_selected_source_generalized_map(
    edges: pd.DataFrame,
    context: _MapContext,
    grid: _SmoothingGrid,
    *,
    source_h3: str,
    visibility_metric: str,
    class_png_path: Path,
    output_path: Path,
    support_geometry_wgs84: Any,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> Path:
    """Write the generalized poor-to-high visibility class map."""

    west, south, east, north = grid.bounds_wgs84
    source_lat, source_lon = h3.cell_to_latlng(str(source_h3))
    map_ = folium.Map(
        location=[source_lat, source_lon],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    _image_overlay(
        class_png_path,
        output_path=output_path,
        externalize=bool(config.externalize_map_assets),
        bounds=[[south, west], [north, east]],
        name=f"Generalized visibility ({config.visibility_class_count} classes)",
        opacity=1.0,
        interactive=True,
        cross_origin=False,
        zindex=2,
        show=True,
    ).add_to(map_)
    _add_generalized_support_layer(
        map_,
        support_geometry_wgs84,
        name="Generalized viewshed extent",
    )
    candidate_gdf = _add_selected_source_context_layers(
        map_,
        edges,
        context,
        source_h3=source_h3,
        visibility_metric=visibility_metric,
        modeled_footprint_show=False,
        output_path=output_path,
        assets=assets,
    )
    map_.get_root().html.add_child(
        folium.Element(
            _generalized_visibility_legend(
                _map_colors(config, config.visibility_class_count),
                config.visibility_class_count,
            )
        )
    )
    _add_map_controls(map_)
    _fit_target_bounds(map_, candidate_gdf)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_.save(output_path)
    return output_path


def _add_h3_metric_layer(
    map_: folium.Map,
    frame: pd.DataFrame,
    *,
    h3_column: str,
    metric: str,
    name: str,
    output_path: Path,
    config: ViewshedMapConfig,
    show: bool,
) -> gpd.GeoDataFrame:
    gdf = _h3_geodataframe(frame[[h3_column, metric]].copy(), h3_column)
    gdf[metric] = pd.to_numeric(gdf[metric], errors="coerce").fillna(0.0)
    vmax = _display_max(gdf[metric], config.display_quantile)
    colors = bcm.LinearColormap(_map_colors(config), vmin=0.0, vmax=vmax)

    def style(feature: dict[str, Any]) -> dict[str, Any]:
        value = float(feature["properties"].get(metric) or 0.0)
        return {
            "fillColor": colors(min(max(value, 0.0), vmax)),
            "color": "#3f3f3f",
            "weight": 0.4,
            "fillOpacity": config.h3_fill_opacity if value > 0.0 else 0.0,
        }

    # These canonical pages are opened directly from disk. Folium implements an
    # external GeoJSON layer with a synchronous XMLHttpRequest, which browsers
    # reject for file:// pages and leaves a named but empty layer. H3 metric
    # tables are compact enough to embed; large smoothed PNGs remain external
    # when externalize_map_assets is enabled.
    asset = None
    _geojson_layer(
        gdf,
        output_path=output_path,
        asset=asset,
        name=name,
        show=show,
        style_function=style,
        highlight_function=lambda _: {"color": "#111111", "weight": 1.8},
        tooltip=folium.GeoJsonTooltip(
            fields=[h3_column, metric],
            aliases=["H3 cell", "Grid value"],
            localize=True,
            sticky=True,
        ),
    ).add_to(map_)
    return gdf


def _add_smoothed_metric_layer(
    map_: folium.Map,
    frame: pd.DataFrame,
    context: Any,
    grid: Any,
    *,
    metric: str,
    name: str,
    stem: str,
    output_dir: Path,
    output_path: Path,
    config: ViewshedMapConfig,
    aggregation: str,
    clip_domain: str,
    source_h3: str | None = None,
) -> dict[str, Any]:
    positive = pd.to_numeric(frame[metric], errors="coerce").fillna(0.0) > 0.0
    if not positive.any():
        folium.FeatureGroup(name=name, show=False).add_to(map_)
        return {"layer": name, "positive_h3_cell_count": 0, "rendered": False}

    smooth_dir = output_dir / "assets" / "smoothed"
    png_path = smooth_dir / f"{stem}.png"
    tif_path = smooth_dir / f"{stem}.tif"
    render = _render_smoothed_metric(
        frame,
        grid,
        metric=metric,
        caption=name,
        png_path=png_path,
        tif_path=tif_path,
        config=config,
        target_aggregation=aggregation,
        source_h3=source_h3,
        clip_domain=clip_domain,
    )
    west, south, east, north = grid.bounds_wgs84
    _image_overlay(
        png_path,
        output_path=output_path,
        externalize=config.externalize_map_assets,
        bounds=[[south, west], [north, east]],
        name=name,
        opacity=1.0,
        interactive=True,
        cross_origin=False,
        zindex=2,
        show=False,
    ).add_to(map_)
    return {
        "layer": name,
        "positive_h3_cell_count": render.positive_cell_count,
        "support_boundary_smoothing_km": config.support_boundary_smoothing_km,
        "vmax": render.vmax,
        "png": str(png_path),
        "tif": str(tif_path),
        "rendered": True,
    }


def _fit_bounds(map_: folium.Map, frames: list[gpd.GeoDataFrame]) -> None:
    bounds = [frame.total_bounds for frame in frames if not frame.empty]
    if not bounds:
        return
    min_lon = min(value[0] for value in bounds)
    min_lat = min(value[1] for value in bounds)
    max_lon = max(value[2] for value in bounds)
    max_lat = max(value[3] for value in bounds)
    map_.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=(18, 18))


def _write_selected_map(
    selected: pd.DataFrame,
    source_cells: gpd.GeoDataFrame,
    *,
    source_h3: str,
    app: AppConfig,
    settings: StaticMapSettings,
    paths: StaticMapOutputPaths,
) -> list[dict[str, Any]]:
    config = settings.map_config
    # A selected-source map is a diagnostic comparison against its original H3
    # cells. Preserve that exact positive-cell support; boundary generalization
    # remains useful for the full-AOI aggregate presentation maps.
    selected_config = replace(config, support_boundary_smoothing_km=0.0)
    context = _build_context(
        selected,
        source_cells,
        center=settings.selected_location,
        app=app,
        config=config,
    )
    grid = _build_smoothing_grid(context, app.paths.water_polygon_path, config)
    map_ = folium.Map(
        location=[
            settings.selected_location.latitude,
            settings.selected_location.longitude,
        ],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    rendered: list[dict[str, Any]] = []
    h3_frames: list[gpd.GeoDataFrame] = []
    for spec in FACTOR_SPECS:
        original_name = f"{spec.caption} — original H3"
        smooth_name = f"{spec.caption} — smooth"
        h3_frames.append(
            _add_h3_metric_layer(
                map_,
                selected,
                h3_column="target_h3",
                metric=spec.selected_metric,
                name=original_name,
                output_path=paths.selected_html,
                config=config,
                show=spec.slug == "combined",
            )
        )
        rendered.append(
            _add_smoothed_metric_layer(
                map_,
                selected,
                context,
                grid,
                metric=spec.selected_metric,
                name=smooth_name,
                stem=f"land_source_selected_{source_h3}_{spec.slug}",
                output_dir=paths.output_dir,
                output_path=paths.selected_html,
                config=selected_config,
                aggregation="single_configured_source",
                clip_domain="modeled_water",
                source_h3=source_h3,
            )
        )

    source_layer = folium.FeatureGroup(
        name="Configured point, source H3, and viewshed radius",
        show=True,
    )
    folium.CircleMarker(
        [settings.selected_location.latitude, settings.selected_location.longitude],
        radius=7,
        color="#111111",
        weight=2,
        fill=True,
        fill_color="#ffffff",
        fill_opacity=1.0,
        tooltip=(
            f"Configured source: {settings.selected_location.latitude:.6f}, "
            f"{settings.selected_location.longitude:.6f} ({source_h3})"
        ),
    ).add_to(source_layer)
    folium.Circle(
        [settings.selected_location.latitude, settings.selected_location.longitude],
        radius=float(app.viewshed.max_distance_m),
        color="#6A3D9A",
        weight=2,
        fill=False,
        dash_array="8 5",
    ).add_to(source_layer)
    selected_source = source_cells.loc[source_cells["h3_cell"].astype(str).eq(source_h3)]
    folium.GeoJson(
        selected_source,
        style_function=lambda _: {
            "color": "#111111",
            "weight": 2.5,
            "fillColor": "#ffffff",
            "fillOpacity": 0.05,
        },
        tooltip=folium.GeoJsonTooltip(fields=["h3_cell"], aliases=["Source H3"]),
    ).add_to(source_layer)
    source_layer.add_to(map_)

    folium.GeoJson(
        mapping(context.target_footprint),
        name="Selected-source target footprint",
        show=False,
        style_function=lambda _: {
            "color": "#555555",
            "weight": 1.2,
            "fillOpacity": 0.0,
            "dashArray": "5 4",
        },
    ).add_to(map_)
    _map_note(
        map_,
        title="Configured-location land-source static viewshed weights",
        config=config,
    )
    add_click_to_copy_coordinates(map_)
    folium.LayerControl(collapsed=False).add_to(map_)
    _fit_bounds(map_, h3_frames)
    paths.selected_html.parent.mkdir(parents=True, exist_ok=True)
    map_.save(paths.selected_html)
    return rendered


def _write_aggregate_map(
    target: pd.DataFrame,
    source: pd.DataFrame,
    source_cells: gpd.GeoDataFrame,
    *,
    app: AppConfig,
    settings: StaticMapSettings,
    paths: StaticMapOutputPaths,
    source_type: str,
    aggregate_html: Path,
) -> list[dict[str, Any]]:
    source_label = f"{source_type.capitalize()}-source"
    config = settings.map_config
    target_context = _build_context(
        target,
        source_cells,
        center=settings.selected_location,
        app=app,
        config=config,
    )
    source_for_context = source.rename(columns={"source_h3": "target_h3"})
    source_context = _build_context(
        source_for_context,
        source_cells,
        center=settings.selected_location,
        app=app,
        config=config,
    )
    target_grid = _build_smoothing_grid(
        target_context,
        app.paths.water_polygon_path,
        config,
    )
    source_grid = _build_smoothing_grid(source_context, None, config)
    map_ = folium.Map(
        location=[
            settings.selected_location.latitude,
            settings.selected_location.longitude,
        ],
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
    )
    rendered: list[dict[str, Any]] = []
    h3_frames: list[gpd.GeoDataFrame] = []
    for scope, original_frame, smooth_frame, h3_column, context, grid, clip_domain in (
        (
            f"{source_label} target aggregate",
            target,
            target,
            "target_h3",
            target_context,
            target_grid,
            "modeled_water",
        ),
        (
            f"{source_label} source aggregate",
            source,
            source_for_context,
            "source_h3",
            source_context,
            source_grid,
            "source_h3_footprint",
        ),
    ):
        is_target = "target aggregate" in scope
        for spec in FACTOR_SPECS:
            metric = spec.target_metric if is_target else spec.source_metric
            original_name = f"{scope}: {spec.caption} — original H3"
            smooth_name = f"{scope}: {spec.caption} — smooth"
            h3_frames.append(
                _add_h3_metric_layer(
                    map_,
                    original_frame,
                    h3_column=h3_column,
                    metric=metric,
                    name=original_name,
                    output_path=aggregate_html,
                    config=config,
                    show=is_target and spec.slug == "combined",
                )
            )
            rendered.append(
                _add_smoothed_metric_layer(
                    map_,
                    smooth_frame,
                    context,
                    grid,
                    metric=metric,
                    name=smooth_name,
                    stem=(
                        f"{source_type}_source_"
                        + ("target" if is_target else "source")
                        + f"_{spec.slug}"
                    ),
                    output_dir=paths.output_dir,
                    output_path=aggregate_html,
                    config=config,
                    aggregation=("sum_across_sources" if is_target else "sum_across_targets"),
                    clip_domain=clip_domain,
                )
            )

    folium.GeoJson(
        mapping(target_context.target_footprint),
        name="Modeled target H3 footprint",
        show=False,
        style_function=lambda _: {
            "color": "#555555",
            "weight": 1.1,
            "fillOpacity": 0.0,
            "dashArray": "5 4",
        },
    ).add_to(map_)
    folium.GeoJson(
        mapping(source_context.target_footprint),
        name="Modeled source H3 footprint",
        show=False,
        style_function=lambda _: {
            "color": "#111111",
            "weight": 1.2,
            "fillOpacity": 0.0,
        },
    ).add_to(map_)
    _map_note(
        map_,
        title=f"Whole-domain {source_type}-source static viewshed aggregates",
        config=config,
    )
    add_click_to_copy_coordinates(map_)
    folium.LayerControl(collapsed=False).add_to(map_)
    _fit_bounds(map_, h3_frames)
    aggregate_html.parent.mkdir(parents=True, exist_ok=True)
    map_.save(aggregate_html)
    return rendered
