"""Thin command presentation for the explicit component workflow."""

from __future__ import annotations

import argparse

from ..api.components import run_component_stage, run_components
from ..api.registry import COMPONENT_STAGES


def register_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], *, default_config: str
) -> None:
    region = subparsers.add_parser(
        "validate-region", help="Read-only canonical Salish Sea readiness and output validation."
    )
    region.add_argument("--config", default=default_config)
    region.add_argument("--inputs-only", action="store_true")
    region.set_defaults(func=_validate_region)
    build = subparsers.add_parser(
        "build", help="Build DEM, CHM, distance, or all components with dependencies."
    )
    build.add_argument("target", choices=("dem", "chm", "distance", "all"))
    build.add_argument("--run-id", default="components")
    stage = subparsers.add_parser("stage", help="Execute exactly one component stage.")
    stage.add_argument("stage", choices=COMPONENT_STAGES)
    for parser in (build, stage):
        parser.add_argument("--config", default=default_config)
        parser.add_argument("--source-type", choices=("land", "water"), default="land")
        parser.add_argument("--overwrite", action="store_true")
    build.set_defaults(
        func=lambda args: print(
            run_components(
                args.config,
                target=args.target,
                run_id=args.run_id,
                source_type=args.source_type,
                overwrite=args.overwrite,
            )
        )
    )
    stage.set_defaults(
        func=lambda args: print(
            run_component_stage(
                args.config, args.stage, source_type=args.source_type, overwrite=args.overwrite
            )
        )
    )


def _validate_region(args: argparse.Namespace) -> None:
    import json

    from ..api.regional import validate_region

    report = validate_region(args.config, require_outputs=not args.inputs_only)
    print(json.dumps(report, indent=2))
    if not report["valid"]:
        raise SystemExit(1)
