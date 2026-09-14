"""Source-type H3 centroid-distance decay weights for viewshed pairs.

This module is intentionally narrow and lookup-driven.

Canonical input
---------------
The source-target pair universe is created upstream by ``prepare_area.py`` and
must be stored as the new-standard lookup schema::

    source_h3: str
    target_h3: str
    distance_km: float
    source_type: str  # one of {"land", "water"}

This module does not classify land/water cells, generate H3 neighborhoods, or
recompute centroid distances. If the lookup does not contain ``distance_km`` or
``source_type``, the lookup is invalid and must be rebuilt.

Production output
-----------------
The compact distance-weight artifact contains exactly::

    source_h3: str
    target_h3: str
    distance_km: float32
    weight_distance: float32

The selected source type is encoded by the output path, not repeated in every
row of the final distance table.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

try:
    from ...config import (
        DEFAULT_CONFIG,
    )
except ImportError:  # pragma: no cover - loose script execution
    from viewshed_toolkit.pipeline.config import (
        DEFAULT_CONFIG,
    )

LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


from ...config.distance import (
    DistanceRuntime,
    DistanceWeightConfig,
    load_distance_runtime,
    load_distance_weight_config,
    setup_logging,
)
from .compute import (
    aggregate_distance_weight_partitions_to_source,
    aggregate_distance_weight_partitions_to_target,
    build_distance_weight_partitions,
    clean_distance_weight_outputs,
    combine_distance_weight_partitions,
)


def _load_runtime_and_config(
    args: argparse.Namespace,
) -> tuple[DistanceRuntime, DistanceWeightConfig]:
    runtime = load_distance_runtime(args.config)
    cfg = load_distance_weight_config(runtime.raw_config)
    if getattr(args, "overwrite", False):
        cfg = DistanceWeightConfig(**{**cfg.__dict__, "overwrite": True})
    return runtime, cfg


def _cmd_build_distance_weights(args: argparse.Namespace) -> None:
    runtime, cfg = _load_runtime_and_config(args)
    manifest = build_distance_weight_partitions(
        runtime,
        cfg,
        limit=args.limit,
        start=args.start,
        progress_every=args.progress_every,
        source_type=args.source,
    )
    if args.combine:
        partition_paths = [
            Path(path) for path in manifest["distance_weight_partition"].dropna().tolist()
        ]
        result = combine_distance_weight_partitions(
            runtime,
            cfg,
            partition_paths=partition_paths,
            source_type=args.source,
        )
        print(
            f"distance_weights[{result['source_type']}]: "
            f"{result['path']} ({result['row_count']:,} rows)"
        )
        if getattr(args, "clean_intermediates", False):
            clean_distance_weight_outputs(runtime, cfg, args.source)


def _cmd_combine_distance_weights(args: argparse.Namespace) -> None:
    runtime, cfg = _load_runtime_and_config(args)
    result = combine_distance_weight_partitions(runtime, cfg, source_type=args.source)
    print(
        f"distance_weights[{result['source_type']}]: "
        f"{result['path']} ({result['row_count']:,} rows)"
    )
    if getattr(args, "clean_intermediates", False):
        clean_distance_weight_outputs(runtime, cfg, args.source)


def _cmd_aggregate_targets(args: argparse.Namespace) -> None:
    runtime, cfg = _load_runtime_and_config(args)
    result = aggregate_distance_weight_partitions_to_target(
        runtime,
        cfg,
        source_type=args.source,
    )
    print(
        f"distance_target_summary[{result['source_type']}]: "
        f"{result['path']} ({result['row_count']:,} rows)"
    )


def _cmd_aggregate_sources(args: argparse.Namespace) -> None:
    runtime, cfg = _load_runtime_and_config(args)
    result = aggregate_distance_weight_partitions_to_source(
        runtime,
        cfg,
        source_type=args.source,
    )
    print(
        f"distance_source_summary[{result['source_type']}]: "
        f"{result['path']} ({result['row_count']:,} rows)"
    )


def _cmd_clean(args: argparse.Namespace) -> None:
    runtime, cfg = _load_runtime_and_config(args)
    clean_distance_weight_outputs(runtime, cfg, args.source)


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build H3 centroid-distance weights from the canonical narrow "
            "source-target lookup. The command filters by --source land/water, "
            "applies distance decay, and writes a compact distance factor table."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_config = str(DEFAULT_CONFIG)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=default_config)
        p.add_argument("--overwrite", action="store_true")
        p.add_argument(
            "--clean-intermediates",
            action="store_true",
            help="Delete this source-type distance scratch directory after successful command.",
        )
        p.add_argument(
            "--log-level",
            default="WARNING",
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        )
        p.add_argument("--log-file", default=None)
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--source", choices=["land", "water"], default="land")

    p = sub.add_parser(
        "build-distance-weights",
        help="Build source-type distance-weight partitions from the canonical lookup.",
    )
    add_common(p)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional number of source H3 cells to process. Useful for smoke tests.",
    )
    p.add_argument(
        "--start",
        type=int,
        default=0,
        help="Optional source H3 cell offset into sorted source universe.",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=0,
        help="Log progress every N chunks. Use 0 to disable.",
    )
    p.add_argument("--combine", action="store_true")
    p.set_defaults(func=_cmd_build_distance_weights)

    p = sub.add_parser("combine-distance-weights")
    add_common(p)
    p.set_defaults(func=_cmd_combine_distance_weights)

    p = sub.add_parser("aggregate-distance-weighted-targets")
    add_common(p)
    p.set_defaults(func=_cmd_aggregate_targets)

    p = sub.add_parser("aggregate-distance-weighted-sources")
    add_common(p)
    p.set_defaults(func=_cmd_aggregate_sources)

    p = sub.add_parser("clean-distance-weights")
    add_common(p)
    p.set_defaults(func=_cmd_clean)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_cli()
    ns = parser.parse_args(argv)
    setup_logging(ns.log_level, log_file=ns.log_file, quiet=ns.quiet)
    ns.func(ns)


if __name__ == "__main__":
    main()
