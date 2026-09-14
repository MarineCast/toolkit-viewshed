"""
Prepare regional USGS 3DEP elevation rasters for viewshed modeling.

This file is the DEM acquisition and preparation entrypoint for the viewshed
pipeline. Given a viewshed YAML configuration, it reads the configured modeling
bounding box, downloads USGS 3DEP DEM tiles for one or more requested
resolutions, mosaics the downloaded chunks, and writes a final compressed
GeoTIFF in WGS84 for downstream visibility/ray-walking stages.

Why this exists
---------------
The viewshed pipeline needs a local, reproducible DEM covering the exact study
region before it can compute terrain-adjusted line-of-sight visibility. Pulling
the DEM in one large request can be brittle for Puget Sound / Salish Sea-scale
runs, so this module splits the configured bounding box into smaller geographic
chunks, downloads each chunk independently, mosaics the chunks, and then warps
the result into the coordinate system expected by the rest of the pipeline.

High-level workflow
-------------------
1. Load the viewshed config and resolve the target DEM path, raw DEM cache
   directory, bounding box, and desired DEM resolution(s).
2. Split the bounding box into a configurable or inferred chunk grid.
3. Download each chunk from USGS 3DEP through py3dep, optionally in parallel.
4. Validate chunk rasters and mosaic them into a native-resolution DEM.
5. Reproject/warp the mosaic to EPSG:4326 and write a compressed tiled GeoTIFF.
6. Reuse existing valid DEM outputs when possible, unless --overwrite is used.
7. Optionally preserve intermediate chunk files for debugging/reproducibility.

Operational notes
-----------------
- Final rasters are written safely: temporary outputs are validated before they
  replace any existing DEM. This prevents a failed download, mosaic, or warp from
  destroying a previously good raster.
- GDAL command-line tools are used when available for mosaic/warp speed. Rasterio
  fallbacks are provided so the module still works in lighter Python-only
  environments.
- Download failures are retried, and parallel chunk failures include chunk index
  and bounding-box context to make broken network/data requests easier to debug.
- This module intentionally does not build H3 cells, run viewshed calculations,
  or apply distance/vegetation weighting. It only prepares the DEM substrate.

Logging and CLI behavior
------------------------
The command-line interface is quiet by default. Routine progress and completion
messages are emitted only when logging is explicitly enabled. Use --verbose for
normal progress logs, or --log-level DEBUG/INFO/WARNING/ERROR/CRITICAL for
explicit control. Avoid adding direct print(...) calls to this module; route
status messages through LOGGER so batch/Makefile runs can stay quiet unless the
caller opts into logs.

Typical usage
-------------
    python -m <package>.prepare.elevation.acquire --config configs/salish_sea.yaml --verbose
    python -m <package>.prepare.elevation.acquire --config configs/salish_sea.yaml --resolutions 10 30
    python -m <package>.prepare.elevation.acquire --config configs/salish_sea.yaml --chunk-grid 4x3
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box

from viewshed_toolkit._internal.artifacts import checksum_path

from ...config import (
    DEFAULT_CONFIG,
    bbox_from_config,
    dem_path_from_config,
    dem_resolution_from_config,
    load_yaml,
    raw_dem_dir_from_config,
)
from ..vegetation.assets import (
    build_or_reuse_vrt,
    profile_timer,
    raster_covers_bbox,
)

CRS_WGS84 = "EPSG:4326"


def dem_source_metadata_path(dem_path: Path) -> Path:
    return Path(dem_path).with_suffix(Path(dem_path).suffix + ".source.json")


def write_dem_source_metadata(
    dem_path: Path,
    *,
    bbox_wgs84: tuple[float, float, float, float],
    resolution_m: int,
    provenance_status: str,
    overwrite: bool,
    chunk_grid: tuple[int, int] | None = None,
) -> Path:
    """Persist honest acquisition lineage for the regional DEM."""

    metadata_path = dem_source_metadata_path(dem_path)
    if metadata_path.exists() and not overwrite:
        try:
            existing = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"DEM source metadata is unreadable: {metadata_path}") from exc
        actual_checksum = checksum_path(dem_path)
        if existing.get("artifact_checksum") != actual_checksum:
            raise ValueError(
                "Existing DEM content does not match its source metadata; reacquire with "
                f"overwrite=True: {dem_path}"
            )
        return metadata_path
    payload = {
        "schema_version": "viewshed_dem_source_v1",
        "artifact_path": str(Path(dem_path).resolve()),
        "artifact_checksum": checksum_path(dem_path),
        "provider_pipeline": "USGS 3DEP via py3dep",
        "provenance_status": provenance_status,
        "bbox_wgs84": list(bbox_wgs84),
        "resolution_m": int(resolution_m),
        "chunk_grid": list(chunk_grid) if chunk_grid is not None else None,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = metadata_path.with_name(f".{metadata_path.name}.part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(metadata_path)
    return metadata_path


DEFAULT_MAX_WORKERS = 4
DEFAULT_CHUNK_WIDTH_DEG = 0.55
DEFAULT_CHUNK_HEIGHT_DEG = 0.35
DEFAULT_DOWNLOAD_ATTEMPTS = 3
GTIFF_CREATION_OPTIONS = [
    "COMPRESS=DEFLATE",
    "TILED=YES",
    "BIGTIFF=IF_SAFER",
]

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


@dataclass(frozen=True)
class DemDownloadResult:
    regional_dem_path: Path
    chunk_paths: list[Path]
    bbox_wgs84: tuple[float, float, float, float]
    resolution_m: int
    chunk_count_last_resolution: int = 0


def _cleanup_dem_temp_root(raw_dem_dir: Path) -> None:
    """Remove the dedicated DEM temp root used for chunk/mosaic staging."""
    if not raw_dem_dir.exists():
        return
    shutil.rmtree(raw_dem_dir, ignore_errors=True)


def valid_raster(path: Path) -> bool:
    """Return True when path exists and can be opened as a non-empty raster."""
    if not path.exists() or not path.is_file():
        return False

    try:
        with rasterio.open(path) as src:
            return src.width > 0 and src.height > 0 and src.count > 0 and src.bounds is not None
    except Exception:
        LOGGER.debug("Invalid raster: %s", path, exc_info=True)
        return False


def _validate_bbox_wgs84(
    bbox_wgs84: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if len(bbox_wgs84) != 4:
        raise ValueError("bbox_wgs84 must be a 4-tuple: (min_lon, min_lat, max_lon, max_lat).")

    min_lon, min_lat, max_lon, max_lat = map(float, bbox_wgs84)
    if not (-180 <= min_lon < max_lon <= 180):
        raise ValueError(f"Invalid longitude bounds in bbox_wgs84: {bbox_wgs84}")
    if not (-90 <= min_lat < max_lat <= 90):
        raise ValueError(f"Invalid latitude bounds in bbox_wgs84: {bbox_wgs84}")

    return min_lon, min_lat, max_lon, max_lat


def _validate_positive_int(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _is_wgs84_crs(crs: object) -> bool:
    """Handle equivalent CRS objects instead of relying on string equality."""
    if crs is None:
        return False
    try:
        return crs.to_epsg() == 4326  # type: ignore[attr-defined]
    except Exception:
        return str(crs).upper() == CRS_WGS84


def _temporary_output_path(final_path: Path, suffix: str | None = None) -> Path:
    """Create a unique temporary path beside the final output for atomic replacement."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    effective_suffix = suffix if suffix is not None else final_path.suffix
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{final_path.stem}.",
        suffix=effective_suffix,
        dir=final_path.parent,
        delete=False,
    )
    tmp_path = Path(handle.name)
    handle.close()
    tmp_path.unlink(missing_ok=True)
    return tmp_path


def _replace_with_valid_raster(tmp_path: Path, final_path: Path) -> Path:
    if not valid_raster(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Temporary raster was not valid: {tmp_path}")

    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.unlink(missing_ok=True)
    tmp_path.replace(final_path)

    if not valid_raster(final_path):
        raise RuntimeError(f"Final raster was not valid after replace: {final_path}")

    return final_path


def _run_command(args: Sequence[str], *, label: str) -> None:
    LOGGER.debug("Running %s: %s", label, " ".join(map(str, args)))
    try:
        subprocess.run(
            list(map(str, args)),
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        message = f"{label} failed with exit code {exc.returncode}."
        if stderr:
            message += f"\nSTDERR:\n{stderr[-4000:]}"
        elif stdout:
            message += f"\nSTDOUT:\n{stdout[-4000:]}"
        raise RuntimeError(message) from exc


def split_bbox(
    bbox_wgs84: tuple[float, float, float, float],
    nx: int,
    ny: int,
) -> list[tuple[int, int, tuple[float, float, float, float]]]:
    """Split a WGS84 bbox into an nx by ny grid of chunk bboxes."""
    min_lon, min_lat, max_lon, max_lat = _validate_bbox_wgs84(bbox_wgs84)
    nx = _validate_positive_int(nx, "nx")
    ny = _validate_positive_int(ny, "ny")

    lon_step = (max_lon - min_lon) / nx
    lat_step = (max_lat - min_lat) / ny

    chunks: list[tuple[int, int, tuple[float, float, float, float]]] = []
    for iy in range(ny):
        for ix in range(nx):
            left = min_lon + ix * lon_step
            right = max_lon if ix == nx - 1 else min_lon + (ix + 1) * lon_step
            bottom = min_lat + iy * lat_step
            top = max_lat if iy == ny - 1 else min_lat + (iy + 1) * lat_step
            chunks.append((ix, iy, (left, bottom, right, top)))

    return chunks


def infer_chunk_grid(
    bbox_wgs84: tuple[float, float, float, float],
    target_chunk_width_deg: float = DEFAULT_CHUNK_WIDTH_DEG,
    target_chunk_height_deg: float = DEFAULT_CHUNK_HEIGHT_DEG,
) -> tuple[int, int]:
    """Infer a chunk grid that keeps py3dep requests reasonably sized."""
    min_lon, min_lat, max_lon, max_lat = _validate_bbox_wgs84(bbox_wgs84)

    if target_chunk_width_deg <= 0 or target_chunk_height_deg <= 0:
        raise ValueError("target chunk dimensions must be positive.")

    nx = max(1, math.ceil((max_lon - min_lon) / target_chunk_width_deg))
    ny = max(1, math.ceil((max_lat - min_lat) / target_chunk_height_deg))
    return nx, ny


def download_3dep_chunk(
    bbox_wgs84: tuple[float, float, float, float],
    out_tif: Path,
    resolution_m: int,
    overwrite: bool = False,
    *,
    attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
) -> Path:
    """Download one USGS 3DEP DEM chunk and write it atomically to a GeoTIFF."""
    _validate_bbox_wgs84(bbox_wgs84)
    resolution_m = _validate_positive_int(resolution_m, "resolution_m")
    attempts = _validate_positive_int(attempts, "attempts")

    if valid_raster(out_tif) and not overwrite:
        return out_tif

    import importlib

    import py3dep

    importlib.import_module("rioxarray")  # Registers the xarray .rio accessor.

    out_tif.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _temporary_output_path(out_tif)

    last_error: Exception | None = None
    for attempt_index in range(1, attempts + 1):
        try:
            tmp_path.unlink(missing_ok=True)

            geom = box(*bbox_wgs84)
            try:
                dem = py3dep.get_dem(geom, resolution=resolution_m, crs=4326)
            except TypeError:
                # Some py3dep versions prefer a GeoJSON-like mapping.
                dem = py3dep.get_dem(geom.__geo_interface__, resolution=resolution_m, crs=4326)

            if getattr(dem, "rio", None) is not None and dem.rio.crs is None:
                dem = dem.rio.write_crs(CRS_WGS84)

            dem.rio.to_raster(tmp_path)
            return _replace_with_valid_raster(tmp_path, out_tif)

        except Exception as exc:
            last_error = exc
            tmp_path.unlink(missing_ok=True)

            if attempt_index < attempts:
                delay_s = min(2 ** (attempt_index - 1), 8)
                LOGGER.warning(
                    "DEM chunk download failed on attempt %s/%s for bbox=%s; retrying in %ss.",
                    attempt_index,
                    attempts,
                    bbox_wgs84,
                    delay_s,
                )
                time.sleep(delay_s)

    raise RuntimeError(
        f"Failed to download 3DEP DEM chunk after {attempts} attempts: {bbox_wgs84}"
    ) from last_error


def mosaic_chunks(
    chunk_paths: Sequence[Path],
    out_tif: Path,
    overwrite: bool = False,
) -> Path:
    """Mosaic DEM chunks into one compressed/tiled GeoTIFF."""
    chunk_paths = [Path(path) for path in chunk_paths]
    if not chunk_paths:
        raise ValueError("mosaic_chunks requires at least one chunk path.")

    invalid_paths = [path for path in chunk_paths if not valid_raster(path)]
    if invalid_paths:
        raise ValueError(f"Cannot mosaic invalid DEM chunk(s): {invalid_paths[:5]}")

    if valid_raster(out_tif) and not overwrite:
        return out_tif

    gdal_translate = shutil.which("gdal_translate")
    gdalbuildvrt = shutil.which("gdalbuildvrt")
    if gdal_translate and gdalbuildvrt:
        tmp_tif = _temporary_output_path(out_tif)
        tmp_vrt = _temporary_output_path(out_tif, suffix=".vrt")
        try:
            build_or_reuse_vrt(chunk_paths, tmp_vrt, overwrite=True)
            with profile_timer("translate_dem_vrt", source=tmp_vrt, output=out_tif):
                _run_command(
                    [
                        gdal_translate,
                        "-of",
                        "GTiff",
                        *sum((["-co", option] for option in GTIFF_CREATION_OPTIONS), []),
                        tmp_vrt,
                        tmp_tif,
                    ],
                    label="gdal_translate DEM VRT",
                )
            return _replace_with_valid_raster(tmp_tif, out_tif)
        except Exception:
            tmp_tif.unlink(missing_ok=True)
            raise
        finally:
            tmp_vrt.unlink(missing_ok=True)

    from rasterio.merge import merge

    tmp_tif = _temporary_output_path(out_tif)
    datasets = [rasterio.open(path) for path in chunk_paths]
    try:
        with profile_timer("mosaic_dem_chunks_fallback", chunks=len(datasets), output=out_tif):
            mosaic, transform = merge(datasets)

        profile = datasets[0].profile.copy()
        profile.update(
            {
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": transform,
                "compress": "deflate",
                "tiled": True,
                "blockxsize": 256,
                "blockysize": 256,
                "BIGTIFF": "IF_SAFER",
            }
        )

        with rasterio.open(tmp_tif, "w", **profile) as dst:
            dst.write(mosaic)

        return _replace_with_valid_raster(tmp_tif, out_tif)
    except Exception:
        tmp_tif.unlink(missing_ok=True)
        raise
    finally:
        for dataset in datasets:
            dataset.close()


def _copy_raster_blockwise(src_path: Path, dst_path: Path) -> Path:
    """Copy a raster block-by-block to avoid materializing the whole raster in memory."""
    tmp_path = _temporary_output_path(dst_path)
    try:
        with rasterio.open(src_path) as src:
            profile = src.profile.copy()
            profile.update(
                {
                    "compress": "deflate",
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                    "BIGTIFF": "IF_SAFER",
                }
            )

            with rasterio.open(tmp_path, "w", **profile) as dst:
                for _, window in src.block_windows(1):
                    dst.write(src.read(window=window), window=window)

        return _replace_with_valid_raster(tmp_path, dst_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def reproject_raster_to_wgs84(
    src_path: Path,
    dst_path: Path,
    overwrite: bool = False,
) -> Path:
    """Reproject a raster to EPSG:4326 using GDAL when available, otherwise rasterio."""
    if not valid_raster(src_path):
        raise ValueError(f"Source raster is not valid: {src_path}")

    if valid_raster(dst_path) and not overwrite:
        return dst_path

    gdalwarp = shutil.which("gdalwarp")
    if gdalwarp:
        tmp_tif = _temporary_output_path(dst_path)
        try:
            with profile_timer("warp_dem_to_wgs84", source=src_path, output=dst_path):
                _run_command(
                    [
                        gdalwarp,
                        "-overwrite",
                        "-t_srs",
                        CRS_WGS84,
                        "-r",
                        "bilinear",
                        "-of",
                        "GTiff",
                        *sum((["-co", option] for option in GTIFF_CREATION_OPTIONS), []),
                        src_path,
                        tmp_tif,
                    ],
                    label="gdalwarp DEM to WGS84",
                )
            return _replace_with_valid_raster(tmp_tif, dst_path)
        except Exception:
            tmp_tif.unlink(missing_ok=True)
            raise

    try:
        with rasterio.open(src_path) as src:
            if src.crs is None:
                raise ValueError(f"Source raster has no CRS: {src_path}")

            if _is_wgs84_crs(src.crs):
                with profile_timer("copy_dem_wgs84_blockwise", source=src_path, output=dst_path):
                    return _copy_raster_blockwise(src_path, dst_path)

            transform, width, height = calculate_default_transform(
                src.crs,
                CRS_WGS84,
                src.width,
                src.height,
                *src.bounds,
            )

            profile = src.profile.copy()
            profile.update(
                {
                    "crs": CRS_WGS84,
                    "transform": transform,
                    "width": width,
                    "height": height,
                    "compress": "deflate",
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                    "BIGTIFF": "IF_SAFER",
                }
            )

            tmp_tif = _temporary_output_path(dst_path)
            try:
                with rasterio.open(tmp_tif, "w", **profile) as dst:
                    for band_index in range(1, src.count + 1):
                        reproject(
                            source=rasterio.band(src, band_index),
                            destination=rasterio.band(dst, band_index),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=CRS_WGS84,
                            resampling=Resampling.bilinear,
                        )
                return _replace_with_valid_raster(tmp_tif, dst_path)
            except Exception:
                tmp_tif.unlink(missing_ok=True)
                raise
    except Exception:
        raise


def _resolve_requested_resolutions(
    raw: dict,
    resolutions: Sequence[int] | None,
) -> list[int]:
    default_resolution = dem_resolution_from_config(raw)
    raw_resolutions = resolutions if resolutions is not None else [default_resolution]

    resolved: list[int] = []
    for value in raw_resolutions:
        resolution_m = _validate_positive_int(int(value), "resolution_m")
        if resolution_m not in resolved:
            resolved.append(resolution_m)

    return resolved or [default_resolution]


def _download_chunks(
    chunks: Sequence[tuple[int, int, tuple[float, float, float, float]]],
    *,
    chunk_dir: Path,
    resolution_m: int,
    overwrite: bool,
    workers: int,
) -> list[Path]:
    def fetch(item: tuple[int, int, tuple[float, float, float, float]]) -> Path:
        ix, iy, chunk_bbox = item
        chunk_path = chunk_dir / f"DEM_{resolution_m}M_chunk_x{ix:02d}_y{iy:02d}.tif"
        return download_3dep_chunk(
            chunk_bbox,
            chunk_path,
            resolution_m,
            overwrite=overwrite,
        )

    worker_count = max(1, min(workers, len(chunks)))
    if worker_count == 1:
        return [fetch(chunk) for chunk in chunks]

    chunk_paths: list[Path] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(fetch, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            ix, iy, chunk_bbox = futures[future]
            try:
                chunk_paths.append(future.result())
            except Exception as exc:
                raise RuntimeError(
                    f"DEM chunk failed at ix={ix}, iy={iy}, bbox={chunk_bbox}"
                ) from exc

    return sorted(chunk_paths)


def download_dem_for_config(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    workers: int | None = None,
    chunk_grid: tuple[int, int] | None = None,
    resolutions: Sequence[int] | None = None,
    keep_intermediates: bool | None = None,
) -> DemDownloadResult:
    """Download, mosaic, and reproject DEM data needed by a viewshed config."""
    raw, config_dir = load_yaml(config_path)
    bbox_wgs84 = _validate_bbox_wgs84(bbox_from_config(raw))
    requested_resolutions = _resolve_requested_resolutions(raw, resolutions)

    configured_workers = raw.get("batch", {}).get("max_workers", DEFAULT_MAX_WORKERS)
    worker_count = _validate_positive_int(
        workers if workers is not None else configured_workers, "workers"
    )

    keep_intermediates = bool(keep_intermediates)
    primary_resolution = dem_resolution_from_config(raw)
    raw_dem_dir = raw_dem_dir_from_config(raw, config_dir)
    if not keep_intermediates:
        _cleanup_dem_temp_root(raw_dem_dir)
    raw_dem_dir.mkdir(parents=True, exist_ok=True)

    nx, ny = chunk_grid or infer_chunk_grid(bbox_wgs84)
    chunks = split_bbox(bbox_wgs84, nx=nx, ny=ny)

    LOGGER.info(
        "Preparing DEM for bbox=%s, resolutions=%s, chunk_grid=%sx%s, workers=%s",
        bbox_wgs84,
        requested_resolutions,
        nx,
        ny,
        worker_count,
    )

    last_chunk_paths: list[Path] = []
    last_chunk_count = 0
    primary_path = dem_path_from_config(raw, config_dir, primary_resolution)

    for resolution_m in requested_resolutions:
        final_dem_path = dem_path_from_config(raw, config_dir, resolution_m)

        if (
            valid_raster(final_dem_path)
            and not overwrite
            and raster_covers_bbox(final_dem_path, bbox_wgs84, expected_crs=CRS_WGS84)
        ):
            write_dem_source_metadata(
                final_dem_path,
                bbox_wgs84=bbox_wgs84,
                resolution_m=resolution_m,
                provenance_status="legacy_existing_artifact_unverified",
                overwrite=False,
            )
            LOGGER.info("Reusing existing DEM: %s", final_dem_path)
            continue

        with tempfile.TemporaryDirectory(
            prefix=f"dem_{resolution_m}m_",
            dir=raw_dem_dir,
        ) as tmp_dir_raw:
            tmp_dir = Path(tmp_dir_raw)
            chunk_dir = tmp_dir / "chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            native_mosaic_path = tmp_dir / f"DEM_{resolution_m}M_native.tif"

            LOGGER.info(
                "Downloading %s DEM chunk(s) at %sm resolution.",
                len(chunks),
                resolution_m,
            )
            chunk_paths = _download_chunks(
                chunks,
                chunk_dir=chunk_dir,
                resolution_m=resolution_m,
                overwrite=overwrite,
                workers=worker_count,
            )
            last_chunk_count = len(chunk_paths)

            mosaic_chunks(chunk_paths, native_mosaic_path, overwrite=True)
            reproject_raster_to_wgs84(native_mosaic_path, final_dem_path, overwrite=True)

            if not raster_covers_bbox(final_dem_path, bbox_wgs84, expected_crs=CRS_WGS84):
                raise RuntimeError(
                    f"Prepared DEM does not cover requested bbox. path={final_dem_path}, bbox={bbox_wgs84}"
                )
            write_dem_source_metadata(
                final_dem_path,
                bbox_wgs84=bbox_wgs84,
                resolution_m=resolution_m,
                provenance_status="downloaded_by_usgs_3dep_pipeline",
                overwrite=True,
                chunk_grid=(nx, ny),
            )

            if keep_intermediates:
                keep_dir = raw_dem_dir / f"DEM_{resolution_m}M_intermediates"
                if keep_dir.exists():
                    shutil.rmtree(keep_dir)
                shutil.copytree(tmp_dir, keep_dir)
                last_chunk_paths = sorted((keep_dir / "chunks").glob("*.tif"))
            else:
                # Avoid returning paths that will be deleted when TemporaryDirectory exits.
                last_chunk_paths = []

    if not keep_intermediates:
        _cleanup_dem_temp_root(raw_dem_dir)

    return DemDownloadResult(
        regional_dem_path=primary_path,
        chunk_paths=last_chunk_paths,
        bbox_wgs84=bbox_wgs84,
        resolution_m=primary_resolution,
        chunk_count_last_resolution=last_chunk_count,
    )


def _parse_chunk_grid(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None

    cleaned = value.lower().replace("x", ",")
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]

    if len(parts) != 2:
        raise ValueError("--chunk-grid must be formatted like '4,3' or '4x3'.")

    nx, ny = int(parts[0]), int(parts[1])
    return (
        _validate_positive_int(nx, "--chunk-grid nx"),
        _validate_positive_int(ny, "--chunk-grid ny"),
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Download and mosaic USGS 3DEP DEM data for a viewshed config."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to viewshed YAML config.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing DEM outputs.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of parallel DEM chunk download workers. Defaults to batch.max_workers in config.",
    )
    parser.add_argument(
        "--resolutions",
        type=int,
        nargs="+",
        default=None,
        help="DEM resolutions to download. Defaults to viewshed.dem_resolution_m in config.",
    )
    parser.add_argument(
        "--chunk-grid",
        default=None,
        help="Optional chunk grid as 'nx,ny' or 'nxxny'. Defaults to an inferred grid from the bbox.",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        default=None,
        help="Keep downloaded DEM chunk intermediates.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable INFO-level progress logging.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=(
            "Enable logging at the selected level. Overrides --verbose when both "
            "are provided. By default, the CLI is quiet unless an exception is raised."
        ),
    )

    args = parser.parse_args()
    selected_log_level = args.log_level or ("INFO" if args.verbose else None)
    if selected_log_level:
        logging.basicConfig(
            level=getattr(logging, selected_log_level),
            format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        )

    result = download_dem_for_config(
        args.config,
        overwrite=args.overwrite,
        workers=args.workers,
        chunk_grid=_parse_chunk_grid(args.chunk_grid),
        resolutions=args.resolutions,
        keep_intermediates=args.keep_intermediates,
    )

    LOGGER.info("DEM download/prep complete.")
    LOGGER.info("regional_dem_path: %s", result.regional_dem_path)
    LOGGER.info("resolution_m: %s", result.resolution_m)
    LOGGER.info("bbox_wgs84: %s", result.bbox_wgs84)
    LOGGER.info("n_chunks_last_resolution: %s", result.chunk_count_last_resolution)
    if result.chunk_paths:
        LOGGER.info("kept_chunk_dir: %s", result.chunk_paths[0].parent)


if __name__ == "__main__":
    main()
