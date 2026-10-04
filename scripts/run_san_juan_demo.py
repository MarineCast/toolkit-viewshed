"""Build the bounded, real-data San Juan documentation example through production APIs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from check_case_study_inputs import check_inputs

from viewshed_toolkit import STAGES, ViewshedRequest, load_app_config, process, run_component_stage
from viewshed_toolkit.pipeline.config.paths import resolve_path
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths_from_raw
from viewshed_toolkit.pipeline.contracts.components import (
    acquisition_request,
    cache_matches,
    component_root,
    record_product,
    write_json,
)
from viewshed_toolkit.pipeline.prepare.area.case_study import prepare_case_geometry
from viewshed_toolkit.pipeline.prepare.vegetation.sources import (
    eth_chm_filename,
    eth_download_canopy_url,
    eth_tile_bounds,
)
from viewshed_toolkit.pipeline.providers.base import validate_raster
from viewshed_toolkit.pipeline.visualization.data import static_map_output_paths


def prepare_canopy_windows(app) -> list[Path]:
    """Read native ETH COG windows; never synthesize missing canopy values.

    This demo-specific acquisition helper retains remote URL, bounds, version,
    access time and checksum. All reprojection, nodata policy and LOS remain
    owned by production stages. Each output explicitly declares 255 as nodata.
    """
    from osgeo import gdal

    gdal.UseExceptions()
    request = acquisition_request(app, "chm")
    bounds = request["acquisition_bbox"]
    outputs = []
    for value in request["dataset"]["assets"]:
        output = Path(value)
        tile = output.stem
        tile_bounds = eth_tile_bounds(tile)
        window = [
            max(bounds[0], tile_bounds[0]),
            max(bounds[1], tile_bounds[1]),
            min(bounds[2], tile_bounds[2]),
            min(bounds[3], tile_bounds[3]),
        ]
        if window[0] >= window[2] or window[1] >= window[3]:
            raise ValueError(f"Configured canopy tile does not intersect the demo: {tile}")
        url = eth_download_canopy_url(eth_chm_filename(tile))
        contract = {
            "algorithm": "native_eth_cog_window_v1",
            "url": url,
            "bbox_wgs84": window,
            "source_year": 2020,
            "nodata": 255,
            "license": "CC BY 4.0",
            "attribution": "Lang, Jetz, Schindler and Wegner, A high-resolution canopy height model of the Earth (2023)",
        }
        if not cache_matches(output, contract):
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp.tif")
            try:
                logging.info("Reading bounded canopy window %s %s", tile, window)
                with gdal.config_options(
                    {
                        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                        "CPL_VSIL_CURL_USE_HEAD": "NO",
                        "GDAL_HTTP_TIMEOUT": "90",
                    }
                ):
                    result = gdal.Translate(
                        str(temporary),
                        "/vsicurl/" + url,
                        projWin=[window[0], window[3], window[2], window[1]],
                        projWinSRS="EPSG:4326",
                        noData=255,
                        creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
                    )
                    if result is None:
                        raise ValueError(f"Canopy window acquisition failed: {tile}")
                    result.FlushCache()
                    result = None
                validate_raster(temporary)
                temporary.replace(output)
                record_product(output, contract, downloaded_at=datetime.now(UTC).isoformat())
            finally:
                temporary.unlink(missing_ok=True)
        outputs.append(output)
    return outputs


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/san_juan_demo.yaml"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-only", action="store_true", help="Acquire and prepare real inputs only"
    )
    mode.add_argument(
        "--render-only",
        action="store_true",
        help="Retired mode: use the checked bundle exporter",
    )
    mode.add_argument(
        "--model-only",
        action="store_true",
        help="Build validated scientific outputs without regenerating legacy presentation",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild derived products from validated real input caches",
    )
    mode.add_argument(
        "--legacy-render",
        action="store_true",
        help="Explicitly render existing validated outputs to an ignored legacy directory",
    )
    parser.add_argument(
        "--legacy-output",
        type=Path,
        help="Legacy output directory; tracked documentation is forbidden",
    )
    args = parser.parse_args(argv)
    if args.render_only:
        parser.error(
            "--render-only is retired. Use scripts/build_documentation_examples.py to export the checked instructional bundle, or --legacy-render for an ignored legacy output."
        )
    if args.legacy_output and not args.legacy_render:
        parser.error("--legacy-output requires --legacy-render")
    if args.prepare_only and args.rebuild:
        parser.error("--rebuild rebuilds the model; combine it with --model-only")
    if args.legacy_render and args.rebuild:
        parser.error("--legacy-render uses existing outputs; run --rebuild --model-only first")
    legacy_output = None
    if args.legacy_render:
        from plot_san_juan_demo import legacy_output_directory

        try:
            legacy_output = legacy_output_directory(args.legacy_output)
        except ValueError as exc:
            parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start = time.monotonic()
    app = load_app_config(args.config)
    data_root = resolve_path("data/san_juan_demo", app.config_path.parent)
    os.environ.setdefault("HYRIVER_CACHE_NAME", str(data_root / "cache/http.sqlite"))
    (data_root / "cache").mkdir(parents=True, exist_ok=True)
    if not args.legacy_render:
        prepare_case_geometry(app)
        prepare_canopy_windows(app)
        for stage in ("download-dem", "prepare-dem", "download-chm", "prepare-chm"):
            logging.info("Stage %s", stage)
            run_component_stage(app, stage)
        check_inputs(args.config)
        if args.prepare_only:
            return
        # Inputs were prepared independently above. Use the production stage
        # registry from land-cell preparation through finalization and maps.
        # A selected-stage request retains intermediates for documentation inspection.
        if args.rebuild:
            from dataclasses import replace

            from viewshed_toolkit.pipeline.api.registry import invocations_for_stage
            from viewshed_toolkit.pipeline.api.stages import run_stage

            for stage in STAGES[1:]:
                for invocation in invocations_for_stage(stage):
                    logging.info("Rebuilding %s role=%s", stage, invocation.source_type)
                    run_stage(app, replace(invocation, overwrite=True))
        else:
            process(
                ViewshedRequest(
                    config=args.config, stages=STAGES[1:], run_id=f"san-juan-demo-{app.config_hash}"
                )
            )
    if not args.legacy_render:
        print(component_root(app).parent / "viewshed-generation.json")
        print(
            f"Next: PYTHONPATH=src python scripts/build_documentation_examples.py --config {args.config} --output docs/assets/examples/san-juan"
        )
        return
    from plot_san_juan_demo import plot_demo

    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    figure, summary = plot_demo(app, output_dir=legacy_output)
    maps = static_map_output_paths(app)
    summary.update(
        generated_at_utc=datetime.now(UTC).isoformat(),
        command_mode="explicit-legacy-render",
        elapsed_seconds=round(time.monotonic() - start, 2),
        config_hash=app.config_hash,
        source_data="real; no synthetic terrain or canopy",
        input_coverage=json.loads(
            (component_root(app) / "analysis/input-coverage.json").read_text()
        ),
        software={name: version(name) for name in ("GDAL", "rasterio", "numpy", "polars", "h3")},
        canopy_windows=[
            json.loads(path.with_suffix(".tif.json").read_text())
            for path in [
                resolve_path(v, app.config_path.parent)
                for v in app.raw_config["datasets"]["chm"]["assets"]
            ]
        ],
        final_artifacts={
            k: str(v.relative_to(data_root))
            for k, v in paths.all_final_paths().items()
            if v.exists() and v.is_relative_to(data_root)
        },
        maps={
            k: str(v.relative_to(data_root))
            for k, v in asdict(maps).items()
            if isinstance(v, Path) and v.exists() and v.is_relative_to(data_root)
        },
    )
    # Runtime receipt contains source paths/checksums and stays with ignored data.
    write_json(data_root / "demo-receipt.json", summary)
    # Small, attributed derivatives are the documentation deliverables. Raw
    # rasters, source manifests and intermediate tables stay in ignored data/.
    docs_assets = figure.parent
    write_json(docs_assets / "san-juan-demo-summary.json", summary)
    note = """<style>body {display:flex;flex-direction:column;}
        .folium-map {flex:1;min-height:400px;height:auto!important;}</style>
        <header style="background:white;padding:10px 14px;border-bottom:1px solid #bbb;
        font:12px/1.4 sans-serif;flex:none;">
        <strong>San Juan Islands documentation demo · 100 m terrain · H3 R7 · 5 km</strong><br>
        Static physical viewability, not detection probability or public access.
        Missing canopy uses zero height (CANOPY_MISSING_PERCENT of mapped land in this input grid).
        Generalized shoreline; coarse exploratory example.<br>
        Sources: USGS 3DEP (public domain);
        <a href="https://langnico.github.io/globalcanopyheight/">ETH 2020 canopy,
        Lang et al.</a> (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>);
        Natural Earth (public domain). Derived by terrain/canopy LOS and distance attenuation.
        </header>"""
    # Derive the coverage percentage instead of retaining this run's number.
    missing = summary["input_coverage"]["rasters"]["chm"]["missing_land_fraction"]
    note = note.replace("CANOPY_MISSING_PERCENT", f"{100 * missing:.1f}%")
    demo_map = docs_assets / "san-juan-demo.html"
    map_html = (
        maps.selected_html.read_text()
        .replace("<head>", "<head><title>San Juan Islands viewshed demo</title>", 1)
        .replace("<body>", "<body>" + note, 1)
    )
    demo_map.write_text("\n".join(line.rstrip() for line in map_html.splitlines()) + "\n")
    print(figure)
    print(demo_map)
    print(data_root / "demo-receipt.json")


if __name__ == "__main__":
    main()
