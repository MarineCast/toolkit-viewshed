"""Radius-viewshed execution for a bare-earth or canopy obstacle surface.

Each observer sample runs one radius-based GDAL viewshed. Water pixels are
aggregated to target H3 cells with observer-to-pixel distance decay inside the
kernel:

    weight_surface = mean_observer,target_pixel(LOS * D(distance))

For the bare-earth run this is persisted as `weight_terrain`. A matched
DTM+CHM run is persisted separately and converted to conditional canopy
attenuation. These are physical support factors in [0, 1]; sums across source
cells are opportunity indices, not probabilities.
"""

from __future__ import annotations

import argparse
import logging
import warnings
from dataclasses import replace
from typing import Any, Sequence

import pandas as pd
import polars as pl

from ...config import (
    DEFAULT_CONFIG,
    AppConfig,
    apply_source_type_policy,
    initialize_app_config,
    load_app_config,
)
from ...prepare.area import domains
from ...prepare.area.inputs import source_cells_input_path, validate_viewshed_inputs

LOGGER = logging.getLogger(__name__)
_AREA_LOOKUP_TARGET_CACHE: dict[tuple[str, str], set[str]] = {}
_LOOKUP_TARGETS_BY_SOURCE_CACHE: dict[tuple[Any, ...], dict[str, set[str]]] = {}
_TARGET_WATER_AREA_BY_H3_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_WATER_TERRAIN_DOMAIN_CACHE: dict[str, domains.DomainGeometries] = {}
_WATER_REPRESENTATIVE_POINT_CACHE: dict[tuple[str, str], Any] = {}
_PREPARED_LAND_DOMAIN_CACHE: dict[str, Any] = {}
_WATER_TERRAIN_PREFILTER_CACHE: dict[
    tuple[str, str, str, int], tuple[pd.DataFrame, set[str] | None]
] = {}

try:
    import xarray as xr
except ImportError:
    xr = None

try:
    import rioxarray
except ImportError:
    rioxarray = None

_XR_VIEW_SHED = None
_XR_VIEW_SHED_IMPORT_ERROR: Exception | None = None


from .cleanup import cleanup_terrain_intermediates, combine_partitions
from .runner import (
    run_one_latlon,
    run_single_cell,
    run_source_cells,
    save_source_cell_map,
)


def _require_land_source(args: argparse.Namespace) -> None:
    if getattr(args, "source", "land") not in {"land", "water"}:
        raise ValueError("source must be land or water")


def _apply_cli_overrides(app: AppConfig, args: argparse.Namespace) -> AppConfig:
    if getattr(args, "batch_size", None) is not None:
        app = replace(app, batch=replace(app.batch, batch_size_cells=int(args.batch_size)))
    if getattr(args, "max_workers", None) is not None:
        app = replace(app, batch=replace(app.batch, max_workers=int(args.max_workers)))
    if getattr(args, "overwrite", False):
        app = replace(app, run=replace(app.run, overwrite=True))
    if getattr(args, "source", None) is not None:
        app = apply_source_type_policy(app, str(args.source))
    return app


def _load_runtime_config(args: argparse.Namespace) -> AppConfig:
    """Load CLI config, apply overrides, and initialize runtime paths."""

    return initialize_app_config(_apply_cli_overrides(load_app_config(args.config), args))


def _cmd_validate_inputs(args: argparse.Namespace) -> None:
    _require_land_source(args)
    app = _apply_cli_overrides(load_app_config(args.config), args)
    validate_viewshed_inputs(app)
    source_path = source_cells_input_path(app)
    print("Viewshed inputs are valid:")
    print(f"  regional_dem_path: {app.paths.regional_dem_path}")
    print(f"  source_cells_path: {source_path}")
    print(f"  water_polygon_path: {app.paths.water_polygon_path}")


def _cmd_run_source_cells(args: argparse.Namespace) -> None:
    _require_land_source(args)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"h3\.aggregation_mode=sampled with pixel_stride > 1 is approximate.*",
            category=RuntimeWarning,
        )
        app = _load_runtime_config(args)
        if getattr(args, "combine", False):
            app = replace(app, run=replace(app.run, combine_final_parquet=True))
    run_source_cells(app, limit=args.limit, start=args.start)
    if getattr(args, "combine", False) and getattr(args, "clean_intermediates", False):
        cleanup_terrain_intermediates(app)


def _cmd_run_cell(args: argparse.Namespace) -> None:
    _require_land_source(args)
    app = _load_runtime_config(args)
    result = run_single_cell(app, args.source_cell)
    print(f"Wrote {result.n_rows:,} visible H3 rows -> {result.partition_path}")
    if app.run.write_maps:
        out = save_source_cell_map(result, app)
        print(f"Wrote map -> {out}")


def _cmd_run_one(args: argparse.Namespace) -> None:
    _require_land_source(args)
    app = _load_runtime_config(args)
    result = run_one_latlon(app, args.lat, args.lon)
    print(f"Source cell: {result.source_h3_cell}")
    print(f"Wrote {result.n_rows:,} visible H3 rows -> {result.partition_path}")
    if app.run.write_maps:
        out = save_source_cell_map(result, app)
        print(f"Wrote map -> {out}")


def _cmd_combine_partitions(args: argparse.Namespace) -> None:
    _require_land_source(args)
    app = _load_runtime_config(args)
    result = combine_partitions(app)
    print(f"Wrote {int(result['rows']):,} rows -> {result['path']}")
    if getattr(args, "clean_intermediates", False):
        cleanup_terrain_intermediates(app)


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simplified H3 viewshed runner")
    sub = parser.add_subparsers(dest="command", required=True)
    default_config = str(DEFAULT_CONFIG)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=default_config)
        p.add_argument("--overwrite", action="store_true")
        p.add_argument(
            "--clean-intermediates",
            action="store_true",
            help="Delete terrain scratch partitions/intermediates after successful combine.",
        )
        p.add_argument(
            "--keep-intermediates",
            action="store_true",
            help=argparse.SUPPRESS,
        )
        p.add_argument("--source", choices=["land", "water"], default="land")
        p.add_argument("--batch-size", type=int, default=None)
        p.add_argument(
            "--max-workers",
            type=int,
            default=None,
            help="Total worker budget per prepared batch. Default comes from config.",
        )

    p = sub.add_parser(
        "validate-inputs",
        help="Validate configured DEM, water polygon, and source H3 inputs.",
    )
    add_common(p)
    p.set_defaults(func=_cmd_validate_inputs)

    p = sub.add_parser(
        "run-source-cells",
        help="Run all configured source H3 cells using prebuilt DEM/source inputs.",
    )
    add_common(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--start", type=int, default=0)
    p.add_argument(
        "--combine",
        action="store_true",
        help="Materialize TERRAIN_WEIGHTS_H3R{res}.parquet after source partitions finish.",
    )
    p.set_defaults(func=_cmd_run_source_cells)

    p = sub.add_parser("run-cell", help="Run one source H3 cell by ID.")
    add_common(p)
    p.add_argument("--source-cell", required=True)
    p.set_defaults(func=_cmd_run_cell)

    p = sub.add_parser("run-one", help="Run one lat/lon by converting it to the source H3 cell.")
    add_common(p)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.set_defaults(func=_cmd_run_one)

    p = sub.add_parser(
        "combine-partitions",
        help="Combine per-source partitions into one final Parquet.",
    )
    add_common(p)
    p.set_defaults(func=_cmd_combine_partitions)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    cli = build_cli()
    ns = cli.parse_args(argv)
    ns.func(ns)


if __name__ == "__main__":
    main()
