"""CLI presentation for the explicit versioned direct-transmission workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..api.registry import TRANSMISSION_STAGES
from ..api.transmission import run_transmission


def _run(args: argparse.Namespace) -> None:
    print(json.dumps(run_transmission(args.config, stage=args.stage), indent=2, sort_keys=True))


def register_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser(
        "transmission",
        help="Versioned direct-ray transmission over provisioned inputs; no acquisition or overwrite.",
    )
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--stage", choices=TRANSMISSION_STAGES, default="run")
    command.set_defaults(func=_run)
