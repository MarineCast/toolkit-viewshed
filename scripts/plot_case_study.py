"""Static scientific map plates from validated target aggregates."""

from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl
from shapely.geometry import box

from viewshed_toolkit._internal.geo.h3 import cell_to_polygon
from viewshed_toolkit.pipeline.config import AppConfig
from viewshed_toolkit.pipeline.config.paths import bbox_from_config
from viewshed_toolkit.pipeline.contracts.components import component_root


def plot_target_aggregates(app: AppConfig) -> dict[str, Path]:
    root = component_root(app)
    west, south, east, north = bbox_from_config(app.raw_config)
    domain = box(west, south, east, north)
    land = gpd.read_file(app.paths.land_polygon_path).to_crs(4326)
    land = land.loc[land.intersects(domain)].copy()
    land.geometry = land.geometry.intersection(domain)
    output_dir = root.parent / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for role in ("land", "water"):
        frame = pl.read_parquet(root / "aggregates" / f"{role}_target_weights.parquet")
        geography = gpd.GeoDataFrame(
            frame.to_pandas(),
            geometry=[cell_to_polygon(cell) for cell in frame["target_h3"]],
            crs=4326,
        )
        panels = [
            ("mean_weight_terrain", "Terrain + pixel distance"),
            ("mean_weight_distance", "Centroid distance diagnostic"),
            ("mean_weight_static_viewability", "Combined physical support"),
        ]
        if role == "land":
            panels.insert(1, ("mean_canopy_given_terrain_support", "Canopy | positive terrain"))
        with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10}):
            fig, axes = plt.subplots(2, 2, figsize=(11, 12), constrained_layout=True)
            for axis, (column, title) in zip(axes.flat, panels, strict=False):
                quantile = float(
                    app.raw_config.get("static_maps", {}).get("display_quantile", 0.98)
                )
                upper = geography[column].quantile(quantile)
                color_max = float(upper) if upper > 0 else 1.0
                axis.set_facecolor("#edf3f5")
                land.plot(ax=axis, facecolor="#e0dfd9", edgecolor="#939995", linewidth=0.3)
                geography.plot(
                    column=column,
                    ax=axis,
                    cmap="viridis",
                    vmin=0,
                    vmax=color_max,
                    edgecolor="none",
                    legend=True,
                    legend_kwds={"shrink": 0.7, "label": "Mean factor", "extend": "max"},
                    missing_kwds={"color": "#999999"},
                )
                for label, lon, lat in (
                    ("Columbia R.", -124.05, 46.25),
                    ("Victoria", -123.3656, 48.4284),
                    ("Vancouver", -123.1207, 49.2827),
                    ("N. Vancouver I.", -128.0, 50.8),
                ):
                    axis.plot(lon, lat, ".", color="#152f36", markersize=3)
                    axis.annotate(
                        label, (lon, lat), xytext=(3, 3), textcoords="offset points", fontsize=7
                    )
                axis.set(
                    xlim=(west, east),
                    ylim=(south, north),
                    title=f"{title}\nColor cap: {quantile:.0%} quantile",
                    xlabel="Longitude",
                    ylabel="Latitude",
                )
                axis.grid(alpha=0.15, linewidth=0.5)
            if len(panels) == 3:
                axes.flat[3].axis("off")
                axes.flat[3].text(
                    0.05,
                    0.8,
                    "Water sources\n\nCanopy is not applicable.\n\nMeans include every candidate pair.\n\nZero means computed zero support.\nThese weights describe physical\nviewability, not detection probability.",
                    va="top",
                    fontsize=12,
                    linespacing=1.6,
                )
            fig.suptitle(f"Salish Sea · {role.title()}-source target summaries", fontsize=18)
            output = output_dir / f"{role}_target_factors.png"
            fig.savefig(output, dpi=160, facecolor="white")
            report_image = output.with_suffix(".jpg")
            fig.savefig(
                report_image,
                dpi=160,
                facecolor="white",
                pil_kwargs={"quality": 90, "optimize": True},
            )
            plt.close(fig)
            outputs[role] = report_image
    return outputs
