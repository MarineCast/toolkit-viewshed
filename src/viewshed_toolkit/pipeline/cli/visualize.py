"""Visualization command definitions and presentation."""

from __future__ import annotations

import argparse
from typing import Any

from ..api.stages import export_static_maps


def _cmd_export_static_maps(args: argparse.Namespace) -> None:
    result = export_static_maps(
        args.config,
        source_type=args.source,
        overwrite=args.overwrite,
    )
    if args.source != "all":
        print(f"source_type: {result.source_type}")
        print(f"aggregate_html: {result.aggregate_html}")
        print(f"target_aggregate_values: {result.target_aggregate_values}")
        print(f"source_aggregate_values: {result.source_aggregate_values}")
    else:
        print(f"selected_source_h3: {result.selected_source_h3}")
        print(f"selected_location_html: {result.selected_html}")
        print(f"land_source_aggregate_html: {result.land_aggregate_html}")
        print(f"water_source_aggregate_html: {result.water_aggregate_html}")
    print(f"manifest: {result.manifest}")


def register_commands(subparsers: Any, *, default_config: str) -> None:
    parser = subparsers.add_parser(
        "export-static-maps", help="Export selected and aggregate static viewshed maps."
    )
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--source", choices=["all", "land", "water"], default="all")
    parser.add_argument("--overwrite", action="store_true")
    parser.set_defaults(func=_cmd_export_static_maps)
