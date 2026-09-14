"""Preparation command definitions and presentation."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.config.data import load_data_config
from viewshed_toolkit._internal.config.paths import resolve_config_path

from ..api import stages as pipeline
from ..config import viewshed_domain_relative


def _cmd_download_dem(args: argparse.Namespace) -> None:
    result = pipeline.download_dem(
        args.config,
        overwrite=args.overwrite,
        workers=args.workers,
        chunk_grid=tuple(args.chunk_grid) if args.chunk_grid else None,
        resolutions=args.resolutions,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"Wrote DEM -> {result.regional_dem_path}")


def _cmd_download_data(args: argparse.Namespace) -> None:
    dem, canopy = pipeline.download_data(
        args.config,
        overwrite=args.overwrite,
        workers=args.workers,
        chunk_grid=tuple(args.chunk_grid) if args.chunk_grid else None,
        resolutions=args.resolutions,
        keep_intermediates=args.keep_intermediates,
    )
    print(f"DEM ready -> {dem.regional_dem_path}")
    print(f"CHM ready -> {canopy.output_path}")
    if args.clean_input_caches:
        removed = pipeline.cleanup_static_input_caches(
            [dem.regional_dem_path, canopy.output_path], args.static_input_cache_paths
        )
        for path in removed:
            print(f"Deleted static input cache -> {path}")


def _cmd_build_land_cells(args: argparse.Namespace) -> None:
    result = pipeline.build_land_cells(
        args.config,
        overwrite=args.overwrite,
        h3_resolution=args.h3_resolution,
    )
    print(f"Wrote {result.n_h3_cells:,} land H3 cells -> {result.land_h3_path}")


def _cmd_prepare_lookup(args: argparse.Namespace) -> None:
    limit = args.limit_per_source_type
    if limit is None:
        limit = args.limit
    result = pipeline.prepare_source_target_lookup(
        args.config,
        overwrite=args.overwrite,
        combine=not args.no_combine,
        limit_per_source_type=limit,
    )
    print(f"Wrote {result.n_pairs:,} source-target lookup pairs -> {result.lookup_path}")


def _cmd_download_canopy(args: argparse.Namespace) -> None:
    result = pipeline.download_canopy_height(
        args.config, overwrite=args.overwrite, dry_run=args.dry_run
    )
    print(f"Canopy height clip -> {result.output_path}")


def _cmd_download_landcover(args: argparse.Namespace) -> None:
    result = pipeline.download_landcover(
        args.config,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        method=args.method,
    )
    print(f"Landcover clip -> {result.output_path}")


def _cmd_prepare_vegetation_rasters(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    canopy_started = time.perf_counter()
    canopy = pipeline.download_canopy_height(
        args.config, overwrite=args.overwrite, dry_run=args.dry_run
    )
    canopy_elapsed = time.perf_counter() - canopy_started
    landcover_started = time.perf_counter()
    landcover = pipeline.download_landcover(
        args.config,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        method=args.method,
    )
    landcover_elapsed = time.perf_counter() - landcover_started
    print(f"Prepared canopy height -> {canopy.output_path}")
    print(f"Prepared landcover -> {landcover.output_path}")
    if args.benchmark:
        print("Vegetation raster benchmark")
        print(f"  chm_prep_seconds: {canopy_elapsed:.1f}")
        print(f"  landcover_prep_seconds: {landcover_elapsed:.1f}")
        print(f"  total_seconds: {time.perf_counter() - started:.1f}")


def _default_test_output_dir(config_path: str | Path) -> str:
    default = f"{viewshed_domain_relative()}_test"
    raw = load_data_config(resolve_config_path(config_path), domains="HUMAN_LAYER")
    viewshed = raw.get("viewshed", {}) or {}
    dev = viewshed.get("dev", {}) if isinstance(viewshed, dict) else {}
    return str(dev.get("test_output_dir", default)) if isinstance(dev, dict) else default


def _cmd_write_test_config(args: argparse.Namespace) -> None:
    import yaml

    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config must parse to a mapping: {args.config}")
    output_dir = args.output_dir or _default_test_output_dir(args.config)
    suffix = str(args.suffix).strip("_")
    run = dict(raw.get("run", {}) or {})
    run["name"] = f"{run.get('name', 'viewshed')}_{suffix}"
    run["version"] = f"{run.get('version', 'local')}_{suffix}"
    raw["run"] = run
    paths = dict(raw.get("paths", {}) or {})
    paths["output_dir"] = output_dir
    source_res = int((raw.get("h3", {}) or {}).get("source_resolution", 6))
    paths["land_h3_path"] = str(Path(output_dir) / f"LAND_AREA_H3R{source_res}.parquet")
    raw["paths"] = paths
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = (Path(args.config).expanduser().resolve().parent / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"Wrote isolated test config -> {output}")


def register_commands(
    subparsers: Any, *, default_config: str, static_input_cache_paths: list[Path]
) -> None:
    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("--config", default=default_config)
        parser.add_argument("--overwrite", action="store_true")
        return parser

    for name in ("download-dem", "download-data"):
        parser = common(name, "Prepare configured DEM inputs.")
        parser.add_argument("--workers", type=int, default=None)
        parser.add_argument("--chunk-grid", type=int, nargs=2, metavar=("NX", "NY"))
        parser.add_argument("--resolutions", type=int, nargs="+", default=None)
        parser.add_argument("--keep-intermediates", action="store_true", default=None)
        if name == "download-data":
            parser.add_argument("--clean-input-caches", action="store_true")
            parser.set_defaults(
                func=_cmd_download_data,
                static_input_cache_paths=static_input_cache_paths,
            )
        else:
            parser.set_defaults(func=_cmd_download_dem)

    parser = common("build-land-cells", "Build land-intersecting H3 cells.")
    parser.add_argument("--h3-resolution", type=int, default=None)
    parser.set_defaults(func=_cmd_build_land_cells)

    for name in ("prepare-area-lookup", "prepare-source-target-lookup"):
        parser = common(name, "Build canonical source-target H3 lookup pairs.")
        parser.add_argument("--limit-per-source-type", type=int, default=None)
        parser.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
        parser.add_argument("--no-combine", action="store_true")
        parser.set_defaults(func=_cmd_prepare_lookup)

    parser = common("download-canopy-height", "Prepare the canopy-height raster.")
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(func=_cmd_download_canopy)

    parser = common("download-landcover", "Prepare the land-cover raster.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--method", choices=["aws"], default=None)
    parser.set_defaults(func=_cmd_download_landcover)

    parser = common("prepare-vegetation-rasters", "Prepare vegetation-support rasters.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--method", choices=["aws"], default=None)
    parser.set_defaults(func=_cmd_prepare_vegetation_rasters)

    parser = subparsers.add_parser("write-test-config", help="Write an isolated test config.")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--output", required=True)
    parser.add_argument("--suffix", default="smoke")
    parser.add_argument("--output-dir", default=None)
    parser.set_defaults(func=_cmd_write_test_config)
