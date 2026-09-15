"""Build reviewed regional summaries and a portable-report artifact from validated outputs."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from plot_case_study import plot_target_aggregates

from viewshed_toolkit import load_app_config
from viewshed_toolkit.pipeline.config.case_study import CaseStudyConfig
from viewshed_toolkit.pipeline.config.paths import bbox_from_config, resolve_path
from viewshed_toolkit.pipeline.contracts.components import component_root, write_json
from viewshed_toolkit.pipeline.finalize.aggregate import FACTORS, aggregate_components
from viewshed_toolkit.pipeline.finalize.composition import validate_composed

LABELS = {
    "weight_terrain": "Terrain with pixel-distance attenuation",
    "weight_vegetation": "Conditional canopy",
    "weight_distance": "Centroid distance diagnostic",
    "weight_static_viewability": "Combined physical support",
}


def build_report(config: Path) -> Path:
    app = load_app_config(config)
    study = CaseStudyConfig.model_validate(app.raw_config["case_study"])
    analysis = resolve_path(study.analysis_directory, app.config_path.parent)
    analysis.mkdir(parents=True, exist_ok=True)
    root = component_root(app)
    aggregate_components(app)
    map_plates = plot_target_aggregates(app)
    summary, factors, distance, distribution = [], [], [], []
    for role in ("land", "water"):
        path = validate_composed(app, role)
        frame = pl.read_parquet(path)
        summary.append(
            {
                "role": role,
                "pairs": frame.height,
                "sources": frame["source_h3"].n_unique(),
                "targets": frame["target_h3"].n_unique(),
                "mean_static": frame["weight_static_viewability"].mean(),
                "positive_fraction": (frame["weight_static_viewability"] > 0).mean(),
                "mean_terrain": frame["weight_terrain"].mean(),
                "canopy_reduction": (
                    1
                    - frame["weight_static_viewability"].cast(pl.Float64).sum()
                    / frame["weight_terrain"].cast(pl.Float64).sum()
                    if role == "land" and frame["weight_terrain"].sum() > 0
                    else None
                ),
            }
        )
        for factor in FACTORS:
            if role == "water" and factor == "weight_vegetation":
                continue
            series = frame[factor]
            factors.append(
                {
                    "role": role,
                    "component": LABELS[factor],
                    "mean": series.mean(),
                    "p50": series.median(),
                    "p95": series.quantile(0.95),
                    "zero_fraction": (series == 0).mean(),
                    "pairs": len(series),
                }
            )
        bins = frame.with_columns(
            (pl.col("distance_km").floor().cast(pl.Int32)).alias("distance_bin_km")
        )
        distance.extend(
            bins.group_by("distance_bin_km")
            .agg(
                pl.len().alias("pairs"),
                pl.col("weight_terrain").mean().alias("terrain"),
                pl.col("weight_static_viewability").mean().alias("combined"),
            )
            .sort("distance_bin_km")
            .with_columns(pl.lit(role).alias("role"))
            .to_dicts()
        )
        histogram = (
            frame.with_columns(
                (pl.col("weight_static_viewability") * 20)
                .floor()
                .clip(0, 19)
                .cast(pl.Int32)
                .alias("bin")
            )
            .group_by("bin")
            .agg(pl.len().alias("pairs"))
            .sort("bin")
        )
        distribution.extend(
            histogram.with_columns(
                (pl.col("bin") / 20).alias("weight_lower"),
                (pl.col("pairs") / frame.height).alias("fraction"),
                pl.lit(role).alias("role"),
            ).to_dicts()
        )
    coverage_path = root / "analysis" / "input-coverage.json"
    coverage = json.loads(coverage_path.read_text())
    coverage_rows = [
        {
            "dataset": name.upper(),
            "missing_fraction": values["missing_land_fraction"],
            "missing_pixels": values["missing_land_pixels"],
            "land_pixels": values["land_pixels"],
        }
        for name, values in coverage["rasters"].items()
    ]
    # Materialize the bounded reader snapshot through explicit, auditable SQL views.
    snapshot_tables = {
        "summary": summary,
        "factors": factors,
        "distance": distance,
        "distribution": distribution,
    }
    sql_queries = {name: f"SELECT * FROM report_{name}" for name in snapshot_tables}
    context = pl.SQLContext(
        **{f"report_{name}": pl.DataFrame(rows) for name, rows in snapshot_tables.items()}
    )
    snapshot_tables = {
        name: context.execute(query).collect().to_dicts() for name, query in sql_queries.items()
    }
    now = datetime.now(UTC).isoformat()
    evidence = {
        "generated_at": now,
        "config_hash": app.config_hash,
        "bbox": bbox_from_config(app.raw_config),
        "summary": summary,
        "factors": factors,
        "distance": distance,
        "distribution": distribution,
    }
    write_json(root / "analysis" / "summary.json", evidence)
    land, water = summary
    title = "Salish Sea physical viewability"
    source = {
        "id": "weights",
        "label": "Validated regional source-to-target weights",
        "path": f"data/{study.name}/outputs/components/analysis/summary.json",
        "query": {
            "engine": "polars_sql",
            "sql": ";\n".join(sql_queries.values()) + ";",
            "language": "sql",
            "description": "Bounded SQL views of Python-derived full-pair summaries, candidate-pair denominators, one-kilometre distance bins, and 0.05-wide combined-weight bins; transformations are implemented in scripts/report_case_study.py.",
            "tables_used": [
                f"data/{study.name}/outputs/components/weights/{role}/static_weights.parquet"
                for role in ("land", "water")
            ],
            "executed_at": now,
            "metric_definitions": {
                "mean_static": "Arithmetic mean over all candidate source-target pairs, including computed zeros. No observer-effort weighting.",
                "canopy_reduction": "1 - sum(combined physical support)/sum(terrain support), within the same source role.",
            },
        },
    }
    coverage_source = {
        "id": "coverage",
        "label": "Raster land-coverage counts",
        "path": f"data/{study.name}/outputs/components/analysis/input-coverage.json",
        "query": {
            "engine": "rasterio",
            "sql": Path(__file__).with_name("check_case_study_inputs.py").read_text(),
            "language": "python",
            "description": "Exact pixel-center counts over Natural Earth mapped land across the buffered prepared raster extent.",
            "tables_used": [
                f"data/{study.name}/prepared/dem_30m.tif",
                f"data/{study.name}/prepared/chm_30m.tif",
            ],
            "executed_at": now,
        },
    }
    charts = [
        {
            "id": "components",
            "title": "Mean component weights",
            "type": "bar",
            "dataset": "factors",
            "sourceId": "weights",
            "layout": "full",
            "encodings": {
                "x": {"field": "component", "type": "nominal"},
                "y": {"field": "mean", "type": "quantitative", "label": "Mean weight"},
                "color": {"field": "role", "type": "nominal", "label": "Source role"},
            },
        },
        {
            "id": "distribution",
            "title": "Combined-weight distribution",
            "type": "bar",
            "dataset": "distribution",
            "sourceId": "weights",
            "layout": "full",
            "encodings": {
                "x": {
                    "field": "weight_lower",
                    "type": "ordinal",
                    "label": "Bin lower edge (width 0.05)",
                },
                "y": {
                    "field": "fraction",
                    "type": "quantitative",
                    "label": "Fraction of candidate pairs",
                    "format": "percent",
                },
                "color": {"field": "role", "type": "nominal", "label": "Source role"},
            },
        },
        {
            "id": "distance",
            "title": "Combined support over distance",
            "type": "line",
            "dataset": "distance",
            "sourceId": "weights",
            "layout": "full",
            "encodings": {
                "x": {
                    "field": "distance_bin_km",
                    "type": "quantitative",
                    "label": "Distance-bin lower edge (km)",
                },
                "y": {
                    "field": "combined",
                    "type": "quantitative",
                    "label": "Mean combined physical support",
                },
                "color": {"field": "role", "type": "nominal", "label": "Source role"},
            },
        },
    ]
    blocks = []

    def prose(id_: str, text: str, sourced: bool = False):
        block = {"id": id_, "type": "markdown", "body": text}
        if sourced:
            block["sourceId"] = "weights"
        blocks.append(block)

    prose("title", f"# {title}")
    prose(
        "summary",
        f"## Technical summary\n\nThe complete regional calculation covers **{land['sources']:,} land source cells** and **{water['sources']:,} water source cells**, with **{land['pairs']+water['pairs']:,} candidate pairs** across the two roles. Positive combined support occurs in **{land['positive_fraction']:.1%}** of land pairs and **{water['positive_fraction']:.1%}** of water pairs. These are static physical-viewability weights, not detection probabilities, access, observer effort, or animal occurrence.\n\nCanopy reduces total modeled land terrain support by **{land['canopy_reduction']:.1%}**, under the configured canopy and observer-clearance assumptions.",
        True,
    )
    prose(
        "definitions",
        "## What the components measure\n\nThe domain runs from the Columbia River mouth through northern Vancouver Island: 128.6°W–121.6°W, 46.0°N–51.1°N. Rasters and target coverage include the configured 31 km buffer. Sources and targets use H3 resolution 7; visibility uses a 30 m projected raster and a 30 km range.\n\n**Terrain** is the existing distance-integrated LOS kernel. **Canopy** is its conditional obstruction ratio. **Distance** is a separately inspectable centroid-distance diagnostic. **Combined support** is terrain × canopy; multiplying centroid distance again would count attenuation twice. Water uses opaque-land LOS and has no applicable canopy factor.\n\nEvery mean below uses the full candidate-pair denominator, including computed zeros. Land and water roles stay separate. Their mixed coastal cells can share H3 identifiers and must not be interpreted as distinct people or visits.",
    )
    blocks.append(
        {
            "id": "coverage_notice",
            "type": "markdown",
            "sourceId": "coverage",
            "body": f"## Missing canopy limits interpretation\n\nAcross the full buffered input raster, **{coverage['rasters']['dem']['missing_land_fraction']:.3%}** of mapped land pixels lack DEM values and **{coverage['rasters']['chm']['missing_land_fraction']:.2%}** lack canopy values. These are input-domain coverage fractions, not fractions of affected source-target pairs.\n\nThe configured model treats missing canopy as zero obstruction and missing DEM as an opaque barrier. Combined support is therefore conditional on those explicit assumptions. Missing canopy is not an observed absence of trees; generalized land geometry also includes some inland water bodies. Inspect the coverage map and finer local data before relying on individual shoreline cells.",
        }
    )
    prose(
        "component_findings",
        "## Components remain separately inspectable\n\nThe comparison shows each component on its dimensionless 0–1 scale. Conditional canopy has a different meaning from terrain support: terrain-blocked pairs receive a neutral canopy factor of one by contract. The source/target aggregate tables also expose canopy means restricted to terrain-supported pairs, avoiding that neutral-factor effect. Water canopy is not shown because it is not applicable.",
    )
    blocks.append(
        {"id": "component_chart", "type": "chart", "chartId": "components", "layout": "full"}
    )
    prose(
        "geographic_patterns",
        "## Where physical support occurs\n\nEach hexagon summarizes candidate source pairs reaching that target cell. Each panel uses its labeled color range, capped at the configured 98th percentile for contrast; the underlying weights remain in [0,1], including computed zeros. Canopy is averaged only where terrain support is positive; gray canopy cells have no such pairs. Map plates are clipped to the case-study bounds, while the numerical tables retain the configured buffered target domain. Outer boundary bands reflect bounded source coverage and the range cutoff, not environmental boundaries. The plates are static; detailed interactive maps are saved under data/salish_sea/outputs/components/maps.",
        True,
    )
    for role, plate in map_plates.items():
        encoded = base64.b64encode(plate.read_bytes()).decode("ascii")
        blocks.append(
            {
                "id": f"{role}_geographic_plate",
                "type": "html",
                "sourceId": "weights",
                "body": f'<figure><img src="data:image/jpeg;base64,{encoded}" alt="{role.title()} source terrain, distance, canopy where applicable, and combined target support maps" style="display:block;width:100%;height:auto" /><figcaption>Static geographic comparison from validated {role} target aggregates. Candidate-pair means are not observer-effort weights.</figcaption></figure>',
            }
        )
    prose(
        "sparsity",
        "## Positive support occupies only part of the candidate universe\n\nThe distribution includes every candidate pair. The first bin includes both exact zeros and small positive values below 0.05; the summary table reports the exact positive fraction separately. A low score describes limited modeled physical support, not low whale presence or low observer activity.",
    )
    blocks.append(
        {"id": "distribution_chart", "type": "chart", "chartId": "distribution", "layout": "full"}
    )
    prose(
        "distance_findings",
        "## Distance and obstruction jointly shape support\n\nEach point averages pairs in a one-kilometre distance bin. These are spatial comparisons, not a time series or causal effect: the sources, coastline, and target composition differ between bins. The decay function attenuates support, while the observed curve also reflects those changing geometries.",
    )
    blocks.append(
        {"id": "distance_chart", "type": "chart", "chartId": "distance", "layout": "full"}
    )
    prose(
        "inventory",
        "## Regional pair inventory\n\nThe table records exact denominators for interpreting the figures. Source and target summaries are saved independently for each role, with candidate counts, mean, maximum, sum, and positive-support fractions. Sums are unnormalized accumulated support, never probabilities.",
    )
    blocks.append(
        {"id": "inventory_table", "type": "table", "tableId": "summary", "layout": "full"}
    )
    prose(
        "method",
        "## Deterministic calculation and validation\n\nUSGS elevation and ETH 2020 canopy tiles are acquired with asset checksums and manifests, then mosaicked to the same projected grid. Natural Earth land defines the coast; the buffered complement defines water without implying public access or territorial jurisdiction. Land observers use the configured active-fraction sampling and 30 m canopy clearance. Water uses deterministic source/target samples and opaque-land obstruction.\n\nComponent validation checks unique non-null H3 pairs, finite weights in [0,1], exact pair coverage, source fingerprints, and the composition identity. Regional maps summarize support over candidate sources; they retain a count denominator to distinguish coverage from support.",
    )
    prose(
        "limits",
        "## Limits and uncertainty\n\nNatural Earth is a generalized coastline and can miss small islands, narrow channels, piers, and fine shoreline structure. A 30 m raster and sampled viewpoints cannot resolve every local obstruction. USGS service coverage includes cross-border elevation samples but is not a guarantee of uniform source quality; source lineage and coverage diagnostics accompany this run. ETH canopy represents 2020 conditions, not current forest cover.\n\nThe inherited canopy policy treats missing canopy as zero obstruction, and DEM gaps are opaque barriers. Those explicit policies can respectively overstate and understate support. Review the saved raster-coverage diagnostics before using shoreline-scale values. The report does not establish field accuracy or detection calibration. Land and water averages use different candidate universes and are descriptive comparisons only.",
    )
    prose(
        "next",
        "## How to use these outputs\n\nUse the pair tables for downstream modeling and the per-source/per-target aggregates for regional inspection. Keep terrain, canopy, and distance diagnostics available alongside combined support. Validate candidate observation sites against finer shoreline and elevation data before making site-level decisions.\n\nObserver activity, access, weather, reporting, and detection require separate data and models.",
    )
    prose(
        "questions",
        "## Questions for the next validation pass\n\nHow sensitive are near-shore values to a finer shoreline and canopy resolution? Which high-support cells remain strong with alternative sampled viewpoints? Where do canopy gaps or older elevation sources materially affect conclusions? These questions need sensitivity runs or field evidence beyond this deterministic case study.",
    )
    prose(
        "source_attribution",
        "## Source attribution\n\nSources accessed September 15, 2026.\n\n- [USGS 3DEP](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services): unrestricted elevation products; the dynamic service combines available source resolutions.\n- [ETH Global Canopy Height 2020](https://langnico.github.io/globalcanopyheight/): CC BY 4.0. Lang, N., Jetz, W., Schindler, K., and Wegner, J. D. (2023), *A high-resolution canopy height model of the Earth*, Nature Ecology & Evolution. This is an estimated canopy-height product, derived from Sentinel-2 and GEDI.\n- [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/): public-domain generalized land geometry.\n\nAcquisition manifests preserve exact asset URLs and checksums. Processing clips land before projection and resamples elevation and canopy to EPSG:32610 at 30 m, with maximum resampling for canopy.",
    )
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "generatedAt": now,
            "description": "Columbia River to northern Vancouver Island: terrain, canopy, distance, and combined physical viewability.",
            "sources": [source, coverage_source],
            "cards": [],
            "charts": charts,
            "tables": [
                {
                    "id": "summary",
                    "title": "Candidate pairs and physical support",
                    "dataset": "summary",
                    "sourceId": "weights",
                    "defaultSort": {"field": "pairs", "direction": "desc"},
                    "columns": [
                        {"field": "role", "label": "Source role", "type": "text"},
                        {"field": "sources", "label": "Source cells", "format": "number"},
                        {"field": "targets", "label": "Target cells", "format": "number"},
                        {"field": "pairs", "label": "Candidate pairs", "format": "number"},
                        {
                            "field": "positive_fraction",
                            "label": "Positive support",
                            "format": "percent",
                        },
                        {"field": "mean_static", "label": "Mean support", "format": "number"},
                    ],
                }
            ],
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": now,
            "status": "ready",
            "datasets": {**snapshot_tables, "coverage": coverage_rows},
        },
        "sources": [source, coverage_source],
    }
    output = root / "analysis" / "artifact.json"
    write_json(output, artifact)
    (analysis / "README.md").write_text(
        f"# Salish Sea case study\n\nConfig: `{config.as_posix()}`.\n\nData and generated outputs: `data/{study.name}/`.\n\nThe HTML report is `report.html`. Regenerate evidence with:\n\n```bash\nPYTHONPATH=src python scripts/report_case_study.py --config {config.as_posix()}\n```\n"
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    print(build_report(parser.parse_args().config))
