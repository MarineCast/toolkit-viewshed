"""Documentation map: one land source, matched target pairs, fixed factor scales.

Chart contract: compare bare terrain, conditional canopy and their composed
physical kernel for the same source/targets. Four static map panels include a
separate water source, with a fixed [0,1] scale. Missing pairs are uncolored,
computed zero is pale blue, mapped land is gray, source is an outlined star.
Renderer: Matplotlib. Outputs: documentation PNG and SVG; inspect both at export size.
"""

from pathlib import Path

import geopandas as gpd
import h3
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
import polars as pl
from shapely.geometry import box

from viewshed_toolkit._internal.geo.h3 import cell_to_polygon
from viewshed_toolkit.pipeline.config.paths import bbox_from_config, resolve_path
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths_from_raw
from viewshed_toolkit.pipeline.finalize.final_artifacts import validate_static_artifact_metadata


def plot_demo(app) -> tuple[Path, dict]:
    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    bounds = bbox_from_config(app.raw_config)
    frame_by_role = {}
    summary = {"bbox_wgs84": list(bounds), "roles": {}}
    for role in ("land", "water"):
        path = paths.land_static_weights if role == "land" else paths.water_static_weights
        validate_static_artifact_metadata(path, raw=app.raw_config, source_type=role)
        frame = pl.read_parquet(path)
        # Select the source nearest Friday Harbor with positive modeled support.
        candidates = (
            frame.filter(pl.col("weight_static_viewability") > 0)["source_h3"].unique().to_list()
        )
        if not candidates:
            raise ValueError(f"No positive {role} support to demonstrate")
        source = min(
            candidates,
            key=lambda c: (h3.cell_to_latlng(c)[0] - 48.535) ** 2
            + ((h3.cell_to_latlng(c)[1] + 123.015) * 0.66) ** 2,
        )
        selected = frame.filter(pl.col("source_h3") == source).sort("target_h3")
        frame_by_role[role] = gpd.GeoDataFrame(
            selected.to_pandas(),
            geometry=[cell_to_polygon(c) for c in selected["target_h3"]],
            crs=4326,
        )
        summary["roles"][role] = {
            "pairs": frame.height,
            "sources": frame["source_h3"].n_unique(),
            "targets": frame["target_h3"].n_unique(),
            "selected_source": source,
            "selected_pairs": selected.height,
            "positive_pairs": frame.filter(pl.col("weight_static_viewability") > 0).height,
        }
    west, south, east, north = bounds
    land = gpd.read_file(
        app.paths.land_polygon_path, bbox=(west - 0.1, south - 0.1, east + 0.1, north + 0.1)
    ).to_crs(4326)
    land.geometry = land.geometry.make_valid().intersection(
        box(west - 0.1, south - 0.1, east + 0.1, north + 0.1)
    )
    cmap = LinearSegmentedColormap.from_list("support", ["#e8f1f8", "#73a9d0", "#174e79"])
    panels = [
        ("land", "weight_terrain", "Bare terrain + distance"),
        ("land", "weight_vegetation", "Conditional canopy factor"),
        ("land", "weight_static_viewability", "Combined land viewability"),
        ("water", "weight_static_viewability", "Water-source viewability"),
    ]
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10}):
        fig, axes = plt.subplots(2, 2, figsize=(11.5, 10.5), layout="constrained")
        for ax, (role, column, title) in zip(axes.flat, panels, strict=True):
            land.plot(ax=ax, color="#e4e3df", edgecolor="#9b9d9b", linewidth=0.45)
            geography = frame_by_role[role]
            geography.plot(
                ax=ax, column=column, cmap=cmap, vmin=0, vmax=1, edgecolor="#ffffff", linewidth=0.3
            )
            if column == "weight_vegetation":
                blocked = geography[geography["weight_terrain"] == 0]
                if not blocked.empty:
                    blocked.plot(
                        ax=ax, facecolor="#eeeeee", edgecolor="#aaaaaa", hatch="///", linewidth=0.3
                    )
            lat, lon = h3.cell_to_latlng(summary["roles"][role]["selected_source"])
            ax.plot(
                lon,
                lat,
                marker="*",
                markersize=14,
                markerfacecolor="#f5bf4f",
                markeredgecolor="#282b2e",
            )
            for name, x, y in [
                ("San Juan", -123.10, 48.54),
                ("Shaw", -122.975, 48.59),
                ("Lopez", -122.93, 48.50),
            ]:
                ax.text(
                    x,
                    y,
                    name,
                    fontsize=9,
                    color="#303539",
                    ha="center",
                    bbox={"facecolor": "white", "alpha": 0.65, "edgecolor": "none", "pad": 1},
                )
            ax.set(
                xlim=(west, east),
                ylim=(south, north),
                title=title,
                xlabel="Longitude",
                ylabel="Latitude",
            )
            ax.tick_params(labelsize=9)
            ax.grid(alpha=0.15, linewidth=0.5)
        fig.colorbar(
            plt.cm.ScalarMappable(norm=Normalize(0, 1), cmap=cmap),
            ax=axes,
            shrink=0.65,
            label="Physical support / conditional factor (0–1)",
        )
        fig.suptitle(
            "Central San Juan Islands · static physical viewability\nH3 resolution 7 · 100 m terrain · 5 km radius · 1–3 land observers per source",
            fontsize=16,
        )
        fig.supxlabel(
            "Stars: source cells. Pale blue: zero. Uncolored water: no candidate pair. Hatched: no baseline support; canopy factor is not interpretable.\nMissing canopy uses a zero-height fallback; see the coverage audit. These are not detection probabilities.\nSources: USGS 3DEP; ETH 2020 canopy (Lang et al., CC BY 4.0); Natural Earth (public domain).",
            fontsize=9,
        )
        output = resolve_path("docs/assets/san-juan-demo.png")
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, dpi=160, facecolor="white")
        svg = output.with_suffix(".svg")
        fig.savefig(svg, facecolor="white")
        svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
        plt.close(fig)
    return output, summary
