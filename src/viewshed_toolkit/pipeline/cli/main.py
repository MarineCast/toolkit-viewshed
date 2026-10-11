"""Top-level viewshed command parser and dispatcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import components, compute, finalize, prepare, visualize
from ..config import DEFAULT_CONFIG

STATIC_INPUT_CACHE_PATHS = [
    Path("data/raw/canopy/eth_10m"),
    Path("data/processed/canopy/eth_global_canopy_height_2020_10m_metadata.json"),
]
TERRAIN_COMMANDS = {
    "validate-inputs",
    "run-source-cells",
    "run-cell",
    "run-one",
    "combine-partitions",
}


def _run_terrain_cli(argv: Sequence[str]) -> None:
    from ..weights.terrain.cli import main as terrain_main

    terrain_main(argv)


def _cmd_phase1(args: argparse.Namespace) -> None:
    _run_terrain_cli(args.phase1_args)


def _cmd_plan_study(args: argparse.Namespace) -> None:
    from ..config.study import load_study_config, planning_report

    print(
        json.dumps(planning_report(load_study_config(args.study_config, planning=True)), indent=2)
    )


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Viewshed Toolkit pipeline")
    parser.add_argument("--study-config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_config = str(DEFAULT_CONFIG)
    components.register_commands(subparsers, default_config=default_config)
    prepare.register_commands(
        subparsers,
        default_config=default_config,
        static_input_cache_paths=STATIC_INPUT_CACHE_PATHS,
    )
    compute.register_commands(subparsers, default_config=default_config)
    finalize.register_commands(subparsers, default_config=default_config)
    visualize.register_commands(subparsers, default_config=default_config)
    plan = subparsers.add_parser(
        "plan-study", help="Read-only shared study support and provenance."
    )
    plan.add_argument("--study-config", type=Path, default=argparse.SUPPRESS)
    plan.set_defaults(func=_cmd_plan_study)
    for command, child in subparsers.choices.items():
        if command != "plan-study":
            child.add_argument("--study-config", type=Path, default=argparse.SUPPRESS)
    phase1 = subparsers.add_parser("phase1", help="Run detailed terrain commands.")
    phase1.add_argument("--study-config", type=Path, default=argparse.SUPPRESS)
    phase1.add_argument("phase1_args", nargs=argparse.REMAINDER)
    phase1.set_defaults(func=_cmd_phase1)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] in TERRAIN_COMMANDS:
        from ..config.study import load_study_config, selected_study_path

        if selected_study_path() is not None:
            load_study_config()
        _run_terrain_cli(arguments)
        return
    args = build_cli().parse_args(arguments)
    from ..config.study import load_study_config, selected_study_path, study_selection

    if args.command != "plan-study" and selected_study_path(getattr(args, "study_config", None)):
        load_study_config(getattr(args, "study_config", None))
    with study_selection(getattr(args, "study_config", None)):
        args.func(args)


if __name__ == "__main__":
    main()
