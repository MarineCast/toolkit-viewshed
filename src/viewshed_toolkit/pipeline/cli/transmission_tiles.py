"""CLI presentation for acquisition-free persistent direct-transmission tiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..api.registry import TRANSMISSION_TILE_STAGES
from ..api.transmission_tile_workflow import run_transmission_tiles


def _run(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            run_transmission_tiles(args.config, stage=args.stage, resume=args.resume), indent=2
        )
    )


def register_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "transmission-tiles",
        help="Plan/preprocess one persistent tile store, then score provisioned sources.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=TRANSMISSION_TILE_STAGES, default="plan")
    parser.add_argument("--resume", action="store_true")
    parser.set_defaults(func=_run)
