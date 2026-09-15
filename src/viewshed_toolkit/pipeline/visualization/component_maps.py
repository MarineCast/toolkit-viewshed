"""Static target-support map for an explicitly selected source domain."""

import hashlib
import html
import uuid
from pathlib import Path
from typing import Any, cast

import folium
import polars as pl
from branca.colormap import LinearColormap
from branca.element import Figure
from shapely.geometry import mapping

from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

from ..config import AppConfig
from ..config.paths import bbox_from_config
from ..contracts.components import component_path, component_root, provenance, record_product


def _stable_element_ids(element: Any, prefix: str) -> None:
    """Folium's default random IDs must not change an unchanged map checksum."""
    element._id = hashlib.sha256(prefix.encode()).hexdigest()[:32]
    for index, child in enumerate(element._children.values()):
        _stable_element_ids(child, f"{prefix}/{index}")


def export_component_map(app: AppConfig, *, source_type: str, factor: str = "static") -> Path:
    columns = {
        "static": "weight_static_viewability",
        "terrain": "weight_terrain",
        "canopy": "weight_vegetation",
        "distance": "weight_distance",
    }
    if factor not in columns or (factor == "canopy" and source_type == "water"):
        raise ValueError("Requested map factor is unavailable for this source role")
    values = pl.col(columns[factor])
    if factor == "canopy":
        values = values.filter(pl.col("weight_terrain") > 0)
    path = component_path(app, "static", source_type)
    frame = (
        pl.read_parquet(path)
        .group_by("target_h3")
        .agg(
            values.mean().alias("mean_static_support"),
            pl.col("source_h3").n_unique().alias("source_cells"),
            (pl.col("weight_terrain") > 0).sum().alias("terrain_supported_sources"),
        )
        .sort("target_h3")
    )
    west, south, east, north = bbox_from_config(app.raw_config)
    map_ = folium.Map(
        location=[(south + north) / 2, (west + east) / 2], tiles=None, prefer_canvas=True
    )
    quantile = float(app.raw_config.get("static_maps", {}).get("display_quantile", 0.98))
    upper = frame["mean_static_support"].quantile(quantile)
    color_max = float(upper) if upper is not None and upper > 0 else 1.0
    colors = LinearColormap(["#ffffd9", "#41b6c4", "#225ea8", "#081d58"], vmin=0, vmax=color_max)
    colors.caption = (
        f"Mean {factor} support across candidate {source_type} sources (not detection probability)"
        if factor != "canopy"
        else "Mean conditional canopy among terrain-supported land pairs; gray = unavailable"
    )
    features = [
        {
            "type": "Feature",
            "geometry": mapping(cell_to_polygon(row["target_h3"])),
            "properties": row,
        }
        for row in frame.to_dicts()
    ]
    folium.GeoJson(
        {"type": "FeatureCollection", "features": features},
        smooth_factor=0,
        style_function=lambda feature: {
            "fillColor": (
                colors(feature["properties"]["mean_static_support"])
                if feature["properties"]["mean_static_support"] is not None
                else "#b7b7b7"
            ),
            "fillOpacity": 1.0,
            "stroke": False,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["target_h3", "mean_static_support", "source_cells"]
            + (["terrain_supported_sources"] if factor == "canopy" else []),
            aliases=["Target H3 cell", f"Mean {factor} support", "Candidate source cells"]
            + (["Terrain-supported source cells"] if factor == "canopy" else []),
        ),
    ).add_to(map_)
    caption = html.escape(colors.caption)
    caption += f". Color cap: {color_max:.3g} ({quantile:.0%} quantile); values remain unchanged."
    ticks = "".join(
        f"<span>{color_max * fraction:.3g}</span>" for fraction in (0, 0.25, 0.5, 0.75, 1)
    )
    cast(Figure, map_.get_root()).html.add_child(
        folium.Element(
            '<div class="component-legend" role="group" aria-label="Weight legend" '
            'style="position:absolute;bottom:28px;left:12px;z-index:1000;'
            "box-sizing:border-box;width:min(360px,calc(100vw - 24px));padding:10px 12px;"
            "background:rgba(255,255,255,0.96);border:1px solid #ced8dc;border-radius:6px;"
            'font:12px/1.4 sans-serif;color:#172f38;">'
            f'<div style="margin-bottom:7px;overflow-wrap:anywhere">{caption}</div>'
            '<div style="height:10px;background:linear-gradient(to right,'
            '#ffffd9,#41b6c4,#225ea8,#081d58)"></div>'
            '<div style="display:flex;justify-content:space-between;margin-top:3px">'
            f"{ticks}"
            "</div></div>"
        )
    )
    target_bounds = [cell_to_polygon(cell).bounds for cell in frame["target_h3"]]
    if target_bounds:
        west = min(west, *(bounds[0] for bounds in target_bounds))
        south = min(south, *(bounds[1] for bounds in target_bounds))
        east = max(east, *(bounds[2] for bounds in target_bounds))
        north = max(north, *(bounds[3] for bounds in target_bounds))
    map_.fit_bounds([[south, west], [north, east]])
    output = component_root(app) / "maps" / f"{source_type}_{factor}_support.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        _stable_element_ids(
            map_.get_root(), f"{app.config_hash}/{source_type}/{factor}/component-map-v4"
        )
        map_.save(str(temporary))
        temporary.replace(output)
        record_product(
            output,
            provenance(app, "component_target_map_v4", {"static": path}),
            factor=factor,
            display_quantile=quantile,
            color_min=0.0,
            color_max=color_max,
        )
    finally:
        temporary.unlink(missing_ok=True)
    return output
