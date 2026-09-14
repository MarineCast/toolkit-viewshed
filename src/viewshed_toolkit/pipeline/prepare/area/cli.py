"""Build the canonical source-target H3 lookup universe.

This module owns the canonical pair universe for the viewshed weighting stages.
The production lookup is deliberately narrow and strict:

    source_h3, target_h3, distance_km, source_type

``source_type`` is the modeling role of the observer source, either ``land`` or
``water``. Physical land/water composition is used internally to construct the
source and target universes, but it is not persisted in the production lookup.

Design contract
---------------
- ``prepare_area.py`` decides which source-target pairs exist.
- ``distance.py`` transforms ``distance_km`` into ``weight_distance``.
- ``terrain.py`` transforms the same pair universe into ``weight_terrain``.
- Production artifacts stay compact; QA/intermediate details stay out of the
  canonical lookup.
"""

from __future__ import annotations

import argparse
import logging
from typing import Sequence

from ...config import DEFAULT_CONFIG

LOGGER = logging.getLogger(__name__)

from .lookup import build_source_target_lookup


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build canonical source-target H3 lookup pairs.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-combine", action="store_true")
    parser.add_argument("--limit-per-source-type", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--clean-intermediates",
        action="store_true",
        help="Delete source-target lookup chunk files after successful combine.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    result = build_source_target_lookup(
        args.config,
        overwrite=args.overwrite,
        combine=not args.no_combine,
        limit_per_source_type=args.limit_per_source_type,
        limit=args.limit,
        clean_intermediates=args.clean_intermediates,
    )
    print(f"Wrote {result.n_pairs} source-target lookup pairs -> {result.lookup_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
