"""Thin command presentation for the explicit component workflow."""

from __future__ import annotations

import argparse
import json

from ..api import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    validate_distance_product,
)
from ..api.components import run_component_stage, run_components
from ..api.registry import COMPONENT_STAGES


def _build_pair_distances(args: argparse.Namespace) -> None:
    print(
        build_pair_distances(
            args.config,
            source_type=args.source_type,
            overwrite=args.overwrite,
        )
    )


def _build_distance_profile(args: argparse.Namespace) -> None:
    profile = DistanceProfile(
        args.profile_id,
        selected_model=args.model,
        logistic_d50_km=args.logistic_d50_km,
        logistic_slope_km=args.logistic_slope_km,
        normalize_at_zero=args.normalize_at_zero,
        exponential_lambda_km=args.exponential_lambda_km,
        piecewise_full_weight_km=args.piecewise_full_weight_km,
        piecewise_zero_weight_km=args.piecewise_zero_weight_km,
        hard_cutoff_km=args.hard_cutoff_km,
    )
    print(build_distance_profile(args.pair_distances, profile, overwrite=args.overwrite))


def _validate_distance_product(args: argparse.Namespace) -> None:
    record = validate_distance_product(args.path)
    print(
        json.dumps(
            {
                "valid": True,
                "path": str(args.path),
                "product_type": record["contract"]["product_type"],
                "rows": record["rows"],
                "fingerprint": record["fingerprint"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def register_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], *, default_config: str
) -> None:
    region = subparsers.add_parser(
        "validate-region", help="Read-only canonical Salish Sea readiness and output validation."
    )
    region.add_argument("--config", default=default_config)
    region.add_argument("--inputs-only", action="store_true")
    region.set_defaults(func=_validate_region)
    pairs = subparsers.add_parser(
        "build-pair-distances",
        help="Materialize reusable H3 pair distances from the validated lookup.",
    )
    pairs.add_argument("--config", default=default_config)
    pairs.add_argument("--source-type", choices=("land", "water"), default="land")
    pairs.add_argument("--overwrite", action="store_true")
    pairs.set_defaults(func=_build_pair_distances)
    profile = subparsers.add_parser(
        "build-distance-profile",
        help="Apply a reusable attenuation profile to a pair-distance product.",
    )
    profile.add_argument("--pair-distances", required=True)
    profile.add_argument("--profile-id", required=True)
    profile.add_argument("--model", choices=("logistic", "exponential", "piecewise"), required=True)
    profile.add_argument("--logistic-d50-km", type=float, default=9.0)
    profile.add_argument("--logistic-slope-km", type=float, default=2.7)
    profile.add_argument("--normalize-at-zero", action="store_true")
    profile.add_argument("--exponential-lambda-km", type=float, default=8.0)
    profile.add_argument("--piecewise-full-weight-km", type=float, default=3.0)
    profile.add_argument("--piecewise-zero-weight-km", type=float, default=30.0)
    profile.add_argument("--hard-cutoff-km", type=float, default=None)
    profile.add_argument("--overwrite", action="store_true")
    profile.set_defaults(func=_build_distance_profile)
    validate_product = subparsers.add_parser(
        "validate-distance-product",
        help="Validate a pair-distance or attenuation-profile product and its provenance.",
    )
    validate_product.add_argument("path")
    validate_product.set_defaults(func=_validate_distance_product)
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
