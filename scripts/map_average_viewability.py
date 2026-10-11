"""Additional offline target maps for the saved WorldCover/CHM trial.

Usage: PYTHONPATH=src python scripts/map_average_viewability.py CONFIG.json
Reads qualified caches only. No acquisition, canonical pipeline or promotion.
"""

import hashlib
import inspect
import io
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import h3
import matplotlib.pyplot as plt
import numpy as np
import polars as pl
from affine import Affine
from benchmark_cell_scores import digest, target_design
from compare_classified_canopy import calculate, load_worldcover
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Patch
from matplotlib.patches import Polygon as PlotPolygon
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import transform as project
from shapely.ops import unary_union

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

CONTRACT = "bounded_in_range_target_maps_v1"
RANGE_M = 5000.0
PARTS_SQL = "\nUNION ALL\n".join(
    f"SELECT site, '{label}' AS component, SUM(({expression}) * target_water_pixel_count) "
    "/ SUM(target_water_pixel_count) AS fraction FROM target_scores GROUP BY site"
    for label, expression in [
        ("Modeled visible support", '"lower"'),
        ("Reduced or blocked support", '1 - "upper"'),
        ("Missing vegetation or roof inputs", "input_unknown_fraction - uncovered_fraction"),
        ("Outside retained obstacle coverage", "uncovered_fraction"),
    ]
)


def target_population(water, transform, grid_shape, observers):
    """All 30m water centres within range of at least one source observer.

    The lattice extends past the retained obstacle cache. Those targets remain
    explicit missing coverage rather than being dropped from the pair universe.
    """
    coarse = transform * Affine.scale(3)
    lo = observers[:, :2].min(0) - RANGE_M
    hi = observers[:, :2].max(0) + RANGE_M
    c0 = math.floor((lo[0] - coarse.c) / 30)
    c1 = math.ceil((hi[0] - coarse.c) / 30)
    r0 = math.floor((coarse.f - hi[1]) / 30)
    r1 = math.ceil((coarse.f - lo[1]) / 30)
    rr, cc = np.mgrid[r0:r1, c0:c1]
    x, y = coarse * (cc + 0.5, rr + 0.5)
    selected = contains_xy(water, x, y)
    within = np.zeros(selected.shape, bool)
    for ox, oy, _ in observers:
        within |= np.hypot(x - ox, y - oy) <= RANGE_M
    selected &= within
    targets = np.column_stack((x[selected], y[selected]))
    inside = (
        (targets[:, 0] >= transform.c)
        & (targets[:, 0] < transform.c + grid_shape[1] * 10)
        & (targets[:, 1] <= transform.f)
        & (targets[:, 1] > transform.f - grid_shape[0] * 10)
    )
    return targets, rr[selected], cc[selected], inside


def merge_coverage(inside_result, inside_codes, outside_states, outside_codes, weights, cells):
    """Combine modeled coverage with uncovered comparisons on the full denominator."""
    count = len(cells)
    inside_weights, outside_weights = weights
    inside_n = np.bincount(inside_codes, weights=inside_weights, minlength=count)
    outside_n = np.bincount(outside_codes, weights=outside_weights, minlength=count)
    total = inside_n + outside_n
    if (total <= 0).any():
        raise ValueError("Every plotted cell needs positive target support")
    unknown = np.bincount(
        outside_codes,
        weights=(outside_states == -1).mean(0) * outside_weights,
        minlength=count,
    )
    excluded = np.bincount(
        outside_codes,
        weights=(outside_states == -2).mean(0) * outside_weights,
        minlength=count,
    )
    lower, upper, missing, beyond = (np.zeros(count) for _ in range(4))
    for code, result in inside_result.items():
        lower[code] = result["lower"] * inside_n[code]
        upper[code] = result["upper"] * inside_n[code]
        missing[code] = result["unknown"] * inside_n[code]
        beyond[code] = result["out_of_range"] * inside_n[code]
    return dict(
        lower=lower / total,
        upper=(upper + unknown) / total,
        unknown=(missing + unknown) / total,
        uncovered=unknown / total,
        out_of_range=(beyond + excluded) / total,
    )


def profile(data, classes, observers, targets, codes, cells, selected, weights, inside, policy):
    points, selected_codes = targets[selected], codes[selected]
    supported = inside[selected]
    retained_codes = np.unique(selected_codes[supported])
    results, seconds = {}, 0.0
    if len(retained_codes):
        local_codes = np.searchsorted(retained_codes, selected_codes[supported])
        computed, seconds = calculate(
            data,
            classes,
            observers,
            points[supported],
            local_codes,
            weights[supported],
            cells[retained_codes],
            [0.002],
            policy,
        )
        results = {
            code: {
                key: computed[0.002][key][i]
                for key in ("lower", "upper", "unknown", "out_of_range")
            }
            for i, code in enumerate(retained_codes)
        }
    outside = points[~supported]
    states = np.full((len(observers), len(outside)), -2, dtype="int8")
    for i, (x, y, _) in enumerate(observers):
        states[i, np.hypot(outside[:, 0] - x, outside[:, 1] - y) <= RANGE_M] = -1
    return (
        merge_coverage(
            results,
            selected_codes[supported],
            states,
            selected_codes[~supported],
            (weights[supported], weights[~supported]),
            cells,
        ),
        seconds,
    )


def polygons(ax, geometry, centre, **kwargs):
    """Display-only simplification; numerical geometry is never simplified."""
    g = geometry.simplify(20, preserve_topology=True)
    parts = [g] if g.geom_type == "Polygon" else getattr(g, "geoms", [])
    for p in parts:
        if p.geom_type == "Polygon":
            xy = (np.asarray(p.exterior.coords) - centre) / 1000
            ax.add_patch(PlotPolygon(xy, **kwargs))
            for hole in p.interiors:
                ax.add_patch(
                    PlotPolygon(
                        (np.asarray(hole.coords) - centre) / 1000,
                        facecolor="#f4f4f1",
                        edgecolor="none",
                    )
                )


def plot_site(site, rows, water, envelope, out):
    plt.rcParams.update({"svg.fonttype": "none", "font.size": 10})
    centre = np.asarray(site["observers"])[:, :2].mean(0)
    cmap = LinearSegmentedColormap.from_list("viewability", ["#f5f7f9", "#144e7b"])
    fig, axes = plt.subplots(1, 2, figsize=(13.8, 7.4), layout="constrained")
    project_hex = Transformer.from_crs(4326, 32610, always_xy=True)
    land = shape(site["metadata"]["source_land"])
    box_bounds = envelope.bounds
    lim = (np.asarray(box_bounds).reshape(2, 2) - centre) / 1000
    for ax, field, title in zip(
        axes, ["lower", "upper"], ["Lower estimate", "Upper estimate"], strict=True
    ):
        ax.set_facecolor("#f4f4f1")
        polygons(ax, water.intersection(envelope), centre, facecolor="white", edgecolor="none")
        polygons(ax, envelope, centre, fill=False, edgecolor="#666666", linestyle="--", linewidth=1)
        for row in rows:
            cell = project(
                project_hex.transform,
                Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(row["target_h3"])]),
            )
            area = cell.intersection(water).intersection(envelope)
            polygons(
                ax, area, centre, facecolor=cmap(row[field]), edgecolor="#8b969e", linewidth=0.8
            )
            polygons(
                ax,
                cell.intersection(envelope),
                centre,
                fill=False,
                edgecolor="#89939a",
                linewidth=0.7,
            )
            label = area.representative_point()
            lx, ly = (np.array([label.x, label.y]) - centre) / 1000
            if area.area > 100000:
                ax.text(
                    lx,
                    ly,
                    f"{100*row[field]:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=9,
                    color="white" if row[field] > 0.58 else "#25313a",
                    bbox={"facecolor": cmap(row[field]), "edgecolor": "none", "pad": 0.7},
                )
        polygons(ax, land, centre, facecolor="#e7bd68", edgecolor="#614912", linewidth=1.7)
        points = (np.asarray(site["observers"])[:, :2] - centre) / 1000
        ax.scatter(points[:, 0], points[:, 1], s=20, marker="+", color="#332713", zorder=5)
        ax.set_xlim(lim[0, 0] - 0.2, lim[1, 0] + 0.2)
        ax.set_ylim(lim[0, 1] - 0.2, lim[1, 1] + 0.2)
        ax.set_aspect("equal")
        ax.set_title(title + " of average viewability", fontsize=13, pad=10)
        ax.set_xlabel("East of source observer centre (km)")
        ax.set_ylabel("North (km)")
    fig.suptitle(
        site["name"] + " · average viewability of target water cells\n"
        "15 source observers · H3 resolution 7 · all water targets within 5 km of any observer",
        fontsize=15,
    )
    fig.colorbar(
        plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, 1)),
        ax=axes,
        orientation="horizontal",
        fraction=0.045,
        pad=0.035,
        label="Average modeled viewability · 0% to 100%",
        ticks=[0, 0.2, 0.4, 0.6, 0.8, 1],
        format=plt.FuncFormatter(lambda x, _: f"{100*x:.0f}%"),
    )
    fig.legend(
        handles=[
            Patch(
                facecolor="#e7bd68",
                edgecolor="#614912",
                label="Source land cell; + marks observers",
            ),
            Patch(
                facecolor="white",
                edgecolor="#666666",
                linestyle="--",
                label="5 km reach from the observer locations",
            ),
        ],
        loc="outside lower center",
        ncol=2,
        frameon=False,
    )
    fig.savefig(out / (site["name"] + ".png"), dpi=140)
    stream = io.StringIO()
    fig.savefig(stream, format="svg")
    plt.close(fig)
    return "<svg" + stream.getvalue().split("<svg", 1)[1]


def run(config):
    if set(config) != {"trial_report", "output_dir"}:
        raise ValueError("Exactly trial_report and output_dir required")
    report_path = Path(config["trial_report"])
    trial = json.loads(report_path.read_text())
    if trial.get("contract") != "bounded_worldcover_canopy_trial_v1":
        raise ValueError("Qualified WorldCover trial required")
    signature = {
        key: trial[key]
        for key in (
            "model",
            "worldcover",
            "benchmark_sha256",
            "prepared_checksums",
            "code_checksums",
        )
    }
    if (
        hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        != trial["scientific_signature"]
    ):
        raise ValueError("Original scientific signature mismatch")
    producer_checksums = {
        "compare_classified_canopy.py": digest(inspect.getfile(calculate)),
        "classified.py": digest(inspect.getfile(calculate.__globals__["classified_ray_support"])),
    }
    if any(trial["code_checksums"][name] != value for name, value in producer_checksums.items()):
        raise ValueError("Original classified-canopy calculation changed; regenerate the trial")
    out = Path(config["output_dir"])
    if out.resolve() == report_path.parent.resolve():
        raise ValueError("Preserve the original report; use a new output directory")
    out.mkdir(parents=True, exist_ok=True)
    benchmark_path = Path(trial["config"]["benchmark_report"])
    if digest(benchmark_path) != trial["benchmark_sha256"]:
        raise ValueError("Original benchmark checksum mismatch")
    benchmark = json.loads(benchmark_path.read_text())
    inputs = Path(benchmark["config"]["input_dir"])
    for asset in trial["worldcover"]["assets"]:
        if digest(asset["path"]) != asset["sha256"]:
            raise ValueError("WorldCover checksum mismatch")
    all_rows, site_results, svgs, decomposition = [], [], {}, []
    for site in trial["sites"]:
        name = site["name"]
        original_site = next(s for s in benchmark["sites"] if s["name"] == name)
        if any(site[key] != original_site[key] for key in ("source_h3", "observers", "metadata")):
            raise ValueError("Saved source observers or metadata differ from the benchmark")
        for ext in ("npz", "json"):
            if digest(inputs / f"{name}.{ext}") != trial["prepared_checksums"][name][ext]:
                raise ValueError("Prepared cache checksum mismatch")
        with np.load(inputs / f"{name}.npz", allow_pickle=False) as cached:
            data = {
                key: cached[key].copy()
                for key in (
                    "ground",
                    "canopy",
                    "canopy_unknown",
                    "building",
                    "building_unknown",
                    "building_present",
                    "qualified_water",
                )
            }
            transform = Affine(*cached["transform"])
            data["transform"] = transform
        if transform.a != 10 or transform.e != -10 or transform.b or transform.d:
            raise ValueError("Retained north-up nested 10m grid required")
        water_input = site["metadata"]["inputs"]["water"]
        if digest(water_input["path"]) != water_input["sha256"]:
            raise ValueError("Target-water geometry checksum mismatch")
        water = gpd.read_parquet(water_input["path"]).to_crs(32610).geometry.union_all()
        observers = np.asarray(site["observers"])
        if (
            observers.shape != (15, 3)
            or not np.isfinite(observers).all()
            or h3.get_resolution(site["source_h3"]) != 7
        ):
            raise ValueError("Original fifteen finite source observers and H3R7 required")
        envelope = unary_union(
            [Point(x, y).buffer(RANGE_M, quad_segs=128) for x, y, _ in observers]
        )
        targets, rr, cc, inside = target_population(
            water, transform, data["ground"].shape, observers
        )
        back = Transformer.from_crs(32610, 4326, always_xy=True)
        lon, lat = back.transform(targets[:, 0], targets[:, 1])
        h3s = np.asarray([h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)])
        cells, codes = np.unique(h3s, return_inverse=True)
        classes = load_worldcover(trial["worldcover"]["assets"], data["ground"].shape, transform)
        selected, weights = target_design(rr, cc, codes, 2, census_threshold=1000)
        policy = trial["model"]["water_class_policy"]
        reference, reference_s = profile(
            data,
            classes,
            observers,
            targets,
            codes,
            cells,
            np.arange(len(targets)),
            np.ones(len(targets)),
            inside,
            policy,
        )
        fast, fast_s = profile(
            data, classes, observers, targets, codes, cells, selected, weights, inside, policy
        )
        error = np.concatenate(
            [np.abs(reference[k] - fast[k]) for k in ("lower", "upper", "unknown")]
        )
        accepted = error.max() <= 0.10 and np.quantile(error, 0.95) <= 0.05
        result = fast if accepted else reference
        counts = np.bincount(codes, minlength=len(cells))
        inside_counts = np.bincount(codes[inside], minlength=len(cells))
        rows = [
            dict(
                site=name,
                source_type="land",
                source_h3=site["source_h3"],
                target_h3=str(cell),
                lower=float(result["lower"][i]),
                upper=float(result["upper"][i]),
                average_viewability=None if result["unknown"][i] else float(result["lower"][i]),
                input_unknown_fraction=float(result["unknown"][i]),
                uncovered_fraction=float(result["uncovered"][i]),
                reference_lower=float(reference["lower"][i]),
                reference_upper=float(reference["upper"][i]),
                reference_unknown_fraction=float(reference["unknown"][i]),
                out_of_range_fraction=float(result["out_of_range"][i]),
                target_water_pixel_count=int(counts[i]),
                obstacle_cache_coverage=float(inside_counts[i] / counts[i]),
                observer_count=len(observers),
                target_scope="30m water centres within5km of any observer; partial H3 water portions",
                model_contract=trial["model"]["contract"],
                beta_per_m=0.002,
                water_class_policy=policy,
                sampling="fast" if accepted else "full",
            )
            for i, cell in enumerate(cells)
        ]
        all_rows.extend(rows)
        total_pixels = len(targets)

        def mean(key, site_rows=rows, total=total_pixels):
            return float(sum(r[key] * r["target_water_pixel_count"] for r in site_rows) / total)

        site_results.append(
            dict(
                site=name,
                target_cells=len(cells),
                target_water_pixels=len(targets),
                cache_missing_pixels=int((~inside).sum()),
                full_seconds=reference_s,
                fast_seconds=fast_s,
                maximum_sampling_error=float(error.max()),
                mean_lower=mean("lower"),
                mean_upper=mean("upper"),
                mean_unknown=mean("input_unknown_fraction"),
                mean_uncovered=mean("uncovered_fraction"),
                reach=mapping(envelope),
            )
        )
        for label, value in [
            ("Modeled visible support", mean("lower")),
            ("Reduced or blocked support", 1 - mean("upper")),
            (
                "Missing vegetation or roof inputs",
                mean("input_unknown_fraction") - mean("uncovered_fraction"),
            ),
            ("Outside retained obstacle coverage", mean("uncovered_fraction")),
        ]:
            decomposition.append(dict(site=name, component=label, fraction=value))
        svgs[name] = plot_site(site, rows, water, envelope, out)
        print(json.dumps(site_results[-1] | {"reach": "recorded in JSON"}), flush=True)
    table = pl.DataFrame(all_rows).sort(["source_type", "source_h3", "target_h3"])
    if table.unique(["source_type", "source_h3", "target_h3"]).height != table.height:
        raise ValueError("Duplicate target pair identity")
    atomic_sink_parquet(table.lazy(), out / "average_viewability.parquet", overwrite=True)
    context = pl.SQLContext(target_scores=pl.read_parquet(out / "average_viewability.parquet"))
    reviewed = context.execute("SELECT * FROM target_scores").collect().to_dicts()
    parts = context.execute(PARTS_SQL).collect().to_dicts()
    # The canonical chart source is the actual query executed over saved scores.
    for expected in decomposition:
        actual = next(
            r
            for r in parts
            if r["site"] == expected["site"] and r["component"] == expected["component"]
        )
        np.testing.assert_allclose(actual["fraction"], expected["fraction"], atol=1e-12)
    summary = dict(
        contract=CONTRACT,
        config=config,
        original_report_sha256=digest(report_path),
        sites=site_results,
        pairs=table.to_dicts(),
        prepared_checksums=trial["prepared_checksums"],
        worldcover=trial["worldcover"],
        scope="All retained regional water-pixel centres within5km of any fixed source observer, including edge cells and uncovered targets; all observer-target pairs in denominator; beyond-range pairs zero",
        code_sha256=digest(__file__),
        producer_checksums=producer_checksums,
    )
    summary["scientific_signature"] = hashlib.sha256(
        json.dumps(summary, sort_keys=True).encode()
    ).hexdigest()
    atomic_write_json(out / "average_viewability.json", summary, overwrite=True)
    artifact = make_artifact(reviewed, site_results, svgs, parts)
    atomic_write_json(out / "artifact.json", artifact, overwrite=True)
    original_html = (report_path.parent / "report.html").read_text()
    nav = '<nav style="padding:16px 28px;background:#e7ede3"><a href="average-viewability.html">Average viewability maps →</a></nav>'
    (out / "report.html").write_text(original_html.replace("<body>", "<body>" + nav, 1))
    for name in (
        "report.json",
        "worldcover_view_scores.parquet",
        "worldcover_canopy_scores.parquet",
    ):
        (out / name).write_bytes((report_path.parent / name).read_bytes())
    return summary


def make_artifact(rows, sites, svgs, decomposition):
    title = "Average viewability from our source cells"
    source = dict(
        id="target_scores",
        label="Dungeness and Sequim target-cell calculation",
        path="average_viewability.parquet",
        query={
            "engine": "Polars SQL",
            "language": "SQL",
            "sql": "SELECT * FROM target_scores",
            "description": "WorldCover + canopy transmission, central coefficient, 15 outdoor observers per source cell",
            "tables_used": ["target_scores (loaded from average_viewability.parquet)"],
            "metric_definitions": {
                "viewability": "Average modeled transmission over15 observers and all30m water centres within5km of any observer, per H3R7 water portion; outside5km pairs zero; missing inputs retain bounds"
            },
        },
    )
    parts_source = source | {"id": "support_parts", "query": source["query"] | {"sql": PARTS_SQL}}
    blocks = [
        dict(id="title", type="markdown", body="# " + title),
        dict(
            id="summary",
            type="markdown",
            body="## Executive Summary\n\n**Each water cell has an average viewability from the highlighted source land cell.** Darker blue means a clearer modeled view. The maps include all target water pixels within 5 km of at least one of the 15 source observer locations, including cells crossing the distance boundary.\n\n**Read the two maps as a range.** Differences show uncertainty from missing vegetation, roof heights or obstacle coverage. Labels are rounded; the table gives precise bounds and identifies remaining missing comparisons.",
        ),
        dict(
            id="reading",
            type="markdown",
            body="## How to read the maps\n\nGold outlines the source land cell; crosses mark its 15 observers. Percentages average their modeled views of the water portion in each target hexagon. For example, 60% means average modeled transmission of 0.60 across those observer and water-point comparisons. Views beyond 5 km from an individual observer contribute zero. The dashed outline shows the combined 5 km reach.\n\nBoundary-cell percentages describe only the water portion inside that reach. Missing inputs contribute zero to the lower estimate and one to the upper estimate; neither map silently turns uncertainty into a complete score.",
        ),
    ]
    for site in sites:
        name = site["site"]
        blocks.append(
            dict(
                id=name.lower() + "-text",
                type="markdown",
                sourceId="target_scores",
                body=f"## {name}\n\nThere are **{site['target_cells']} target water cells** in range. Their water-area-weighted average modeled viewability spans **{100*site['mean_lower']:.1f}% to {100*site['mean_upper']:.1f}%**. Compare the same target cell across the two maps: a large gap indicates missing information. These are averages across the source land cell, so an individual shoreline viewpoint can be clearer.",
            )
        )
        blocks.append(
            dict(
                id=name.lower() + "-map",
                type="html",
                body='<figure style="margin:0;background:white">'
                + svgs[name].replace("<svg ", '<svg style="width:100%;height:auto" ', 1)
                + f'<figcaption style="padding:12px">{name}: lower and upper average viewability, central vegetation scenario.</figcaption></figure>',
            )
        )
    blocks.extend(
        [
            dict(
                id="uncertainty-text",
                type="markdown",
                body="## Where the uncertainty comes from\n\nThe blue share is modeled visible support. The remaining shares distinguish reduced or blocked support from missing vegetation or roof inputs and missing obstacle coverage. The upper map adds both unknown shares to the blue share. This helps identify which targets need better input data.",
            ),
            dict(id="uncertainty-chart", type="chart", chartId="support_parts"),
            dict(
                id="lookup-text",
                type="markdown",
                body="## Exact target-cell values\n\nUse this table to match a target hexagon to its H3 identifier. The lower and upper columns give the viewability range; coverage reports how much target water lies inside the retained obstacle raster.",
            ),
            dict(id="lookup", type="table", tableId="target_values"),
            dict(
                id="assumptions",
                type="markdown",
                body="## What to improve next\n\nPrioritize missing canopy heights along the widest-range views and extend obstacle rasters where target coverage is incomplete. The model uses 2020 canopy and WorldCover inputs and assumes a complete building inventory. It assumes zero vegetation where WorldCover reports water and canopy height is missing. Transmission coefficients remain assumptions awaiting field calibration.\n\nThe maps use 10 m obstacle grids, retained 30 m terrain and water targets, and H3 resolution 7 reporting cells.\n\nWorldCover attribution: © ESA WorldCover project 2020 / Contains modified Copernicus Sentinel data (2020) processed by ESA WorldCover consortium. CC BY 4.0.",
            ),
            dict(
                id="back",
                type="markdown",
                body="Use your browser's Back button to return to the detailed WorldCover and canopy trial.",
            ),
        ]
    )
    chart = dict(
        id="support_parts",
        type="bar",
        title="Average support and missing comparisons",
        dataset="parts",
        sourceId="support_parts",
        valueFormat="percent",
        encodings={
            "x": {"field": "site", "type": "nominal"},
            "y": {"field": "fraction", "type": "quantitative"},
            "color": {"field": "component", "type": "nominal"},
        },
        settings={"groupMode": "stacked100", "orientation": "horizontal", "showValues": True},
        palette={"kind": "identity"},
    )
    columns = [
        {"field": "site", "label": "Source", "type": "text"},
        {"field": "target_h3", "label": "Target H3", "type": "text"},
    ]
    columns.extend(
        {"field": f, "label": label, "format": "percent"}
        for f, label in [
            ("lower", "Lower"),
            ("upper", "Upper"),
            ("input_unknown_fraction", "Missing comparisons"),
            ("obstacle_cache_coverage", "Raster coverage"),
        ]
    )
    table = dict(
        id="target_values",
        title="Average viewability per target water cell",
        dataset="targets",
        sourceId="target_scores",
        defaultSort={"field": "lower", "direction": "desc"},
        columns=columns,
    )
    return dict(
        surface="report",
        manifest=dict(
            version=1,
            surface="report",
            title=title,
            blocks=blocks,
            sources=[source, parts_source],
            charts=[chart],
            tables=[table],
        ),
        snapshot=dict(
            version=1, status="ready", datasets={"targets": rows, "parts": decomposition}
        ),
        sources=[source, parts_source],
    )


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
