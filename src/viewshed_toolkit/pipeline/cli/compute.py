"""Weight-computation command definitions and presentation."""

from __future__ import annotations

import argparse
from typing import Any

from ..api import stages as pipeline


def _print_mapping(values: object) -> None:
    if isinstance(values, dict):
        for name, value in values.items():
            print(f"{name}: {value}")


def _cmd_distance(args: argparse.Namespace) -> None:
    result = pipeline.build_distance_weights(
        args.config,
        source_type=args.source,
        overwrite=args.overwrite,
        combine=args.combine,
        clean_intermediates=args.clean_intermediates,
    )
    _print_mapping(result.get("combined"))


def _cmd_distance_alias(args: argparse.Namespace) -> None:
    args.combine = True
    _cmd_distance(args)


def _cmd_combine_distance(args: argparse.Namespace) -> None:
    _print_mapping(
        pipeline.combine_distance_weights(
            args.config,
            source_type=args.source,
            overwrite=args.overwrite,
            clean_intermediates=args.clean_intermediates,
        )
    )


def _cmd_aggregate_distance(args: argparse.Namespace) -> None:
    _print_mapping(
        pipeline.aggregate_distance_weights(
            args.config, source_type=args.source, by=args.aggregate_by
        )
    )


def _cmd_clean_distance(args: argparse.Namespace) -> None:
    pipeline.clean_distance_weights(args.config, source_type=args.source)


def _cmd_vegetation(args: argparse.Namespace) -> None:
    _print_mapping(
        dict(
            pipeline.build_vegetation_weights(
                args.config,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                resolution_m=args.resolution_m,
                h3_resolution=args.h3_resolution,
                skip_rasters=args.skip_rasters,
                skip_h3=args.skip_h3,
            )
        )
    )


def _cmd_vegetation_path(args: argparse.Namespace) -> None:
    _print_mapping(
        dict(
            pipeline.build_vegetation_path_weights(
                args.config,
                source_type=args.source,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                limit=args.limit,
                clean_intermediates=not args.keep_intermediates,
            )
        )
    )


def _cmd_dual_surface(args: argparse.Namespace) -> None:
    result = pipeline.build_dual_surface_canopy_weights(
        args.config,
        overwrite=args.overwrite,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
    )
    for name in (
        "terrain_weights",
        "canopy_los_weights",
        "vegetation_weights",
        "dual_surface_factors",
    ):
        print(f"{name}: {getattr(result, name)}")


def _cmd_terrain(args: argparse.Namespace) -> None:
    combine = args.limit is None and not args.start
    pipeline.run_terrain_weights(
        args.config,
        source_type=args.source,
        overwrite=args.overwrite,
        limit=args.limit,
        start=args.start,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        combine=combine,
        clean_intermediates=False,
    )
    if not combine:
        print(
            "Skipped terrain combine because --limit/--start was supplied; "
            "run without LIMIT/START to materialize final terrain weights."
        )


def _cmd_retired_water_los(_args: argparse.Namespace) -> None:
    raise ValueError("water-los-weight has been retired. Use terrain-weight --source water.")


def register_commands(subparsers: Any, *, default_config: str) -> None:
    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("--config", default=default_config)
        parser.add_argument("--overwrite", action="store_true")
        return parser

    parser = common("build-vegetation-weights", "Build vegetation support weights.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resolution-m", type=int, default=None)
    parser.add_argument("--h3-resolution", type=int, default=None)
    parser.add_argument("--skip-rasters", action="store_true")
    parser.add_argument("--skip-h3", action="store_true")
    parser.set_defaults(func=_cmd_vegetation)

    for name in ("build-vegetation-path-weights", "vegetation-weight"):
        parser = common(name, "Build source-target vegetation attenuation weights.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--keep-intermediates", action="store_true")
        parser.add_argument("--source", choices=["land", "water"], default="land")
        parser.set_defaults(func=_cmd_vegetation_path)

    parser = common(
        "build-dual-surface-canopy-weights",
        "Build matched bare-earth and canopy visibility factors.",
    )
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.set_defaults(func=_cmd_dual_surface)

    parser = common(
        "terrain-weight",
        "Run paired bare-earth/canopy land weighting or water terrain weighting.",
    )
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--keep-intermediates", action="store_true")
    parser.set_defaults(func=_cmd_terrain)

    for name, combine in (
        ("build-distance-weights", False),
        ("distance-weight", True),
    ):
        parser = common(name, "Build source-target distance weights.")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--start", type=int, default=0)
        parser.add_argument("--combine", action="store_true", default=combine)
        parser.add_argument("--source", choices=["land", "water"], default="land")
        parser.add_argument("--clean-intermediates", action="store_true")
        parser.set_defaults(func=_cmd_distance_alias if combine else _cmd_distance)

    parser = common("combine-distance-weights", "Combine distance partitions.")
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.set_defaults(func=_cmd_combine_distance)

    for name, by in (
        ("aggregate-distance-weighted-targets", "target"),
        ("aggregate-distance-weighted-sources", "source"),
    ):
        parser = common(name, "Aggregate source-target distance weights.")
        parser.add_argument("--source", choices=["land", "water"], default="land")
        parser.set_defaults(func=_cmd_aggregate_distance, aggregate_by=by)

    parser = common("clean-distance-weights", "Delete distance-weight scratch outputs.")
    parser.add_argument("--source", choices=["land", "water"], default="land")
    parser.set_defaults(func=_cmd_clean_distance)

    parser = common("water-los-weight", "Deprecated water LOS command.")
    parser.add_argument("--source", choices=["land", "water"], default="water")
    parser.set_defaults(func=_cmd_retired_water_los)
