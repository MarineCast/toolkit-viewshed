"""Finalization and diagnostic command definitions."""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..api import stages as pipeline
from ..diagnostics.source_cells import audit_source_cells, print_audit_report


def _cmd_finalize_static(args: argparse.Namespace) -> None:
    outputs = pipeline.finalize_static_outputs(
        args.config,
        overwrite=args.overwrite,
        clean_intermediates=args.clean_intermediates,
    )
    print("Finalized viewshed outputs:")
    for name, path in outputs.items():
        print(f"  {name}: {path}")


def _cmd_finalize_score(args: argparse.Namespace) -> None:
    path = pipeline.finalize_view_score(
        args.config, source_type=args.source, overwrite=args.overwrite
    )
    print(f"physical_view_score: {path}")


def _cmd_audit(args: argparse.Namespace) -> None:
    reports = audit_source_cells(
        args.config,
        [str(value) for value in args.source_h3],
        source_type=args.source,
    )
    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        print_audit_report(reports)


def register_commands(subparsers: Any, *, default_config: str) -> None:
    parser = subparsers.add_parser("finalize-view-score", help="Finalize physical view scores.")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.set_defaults(func=_cmd_finalize_score)

    parser = subparsers.add_parser(
        "finalize-viewshed-lookups", help="Materialize final static viewshed weights."
    )
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.set_defaults(func=_cmd_finalize_static)

    parser = subparsers.add_parser(
        "audit-source-cell", help="Trace source H3 cells through final artifacts."
    )
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("source_h3", nargs="+")
    parser.set_defaults(func=_cmd_audit)
