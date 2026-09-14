from __future__ import annotations

import argparse
import logging
from typing import Sequence

from ...config import DEFAULT_CONFIG

from .rasters import download_canopy_height_for_config, download_landcover_for_config


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Prepare vegetation support rasters for viewshed weighting."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    canopy = sub.add_parser("canopy", help="Prepare ETH Global Canopy Height rasters.")
    canopy.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    canopy.add_argument("--overwrite", action="store_true")
    canopy.add_argument("--dry-run", action="store_true")

    landcover = sub.add_parser("landcover", help="Prepare ESA WorldCover landcover rasters.")
    landcover.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    landcover.add_argument("--overwrite", action="store_true")
    landcover.add_argument("--dry-run", action="store_true")
    landcover.add_argument("--method", choices=["aws"], default=None)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    if args.command == "canopy":
        download_canopy_height_for_config(
            args.config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    elif args.command == "landcover":
        download_landcover_for_config(
            args.config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            method=args.method,
        )
    else:  # pragma: no cover
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
