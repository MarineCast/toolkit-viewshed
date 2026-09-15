"""Terrain clear-sky visibility support for viewshed weights.

Scientific role
---------------
This module estimates terrain and hard-canopy viewing opportunity from
source-domain H3 source cells to water-domain H3 target cells. It represents
the clear-sky physical line-of-sight portion of the model: what could be seen
if distance attenuation, land-cover accessibility, weather, observer behavior,
and animal availability were ignored.

The terrain factor is intentionally kept separate from later attenuation
layers. Its persisted output is a sparse pair table:

    source_h3
        Source-domain H3 cell.

    target_h3
        Water/target H3 cell.

    weight_terrain
        Terrain visibility support in [0, 1]. A value of 1 means the sampled
        observer points and visible water area provide full terrain support for
        that source-target pair. A value of 0 means terrain provides no support.

Method summary
--------------
For each source H3 cell, the pipeline samples one or more observer points,
runs a DEM-backed viewshed from each point, masks the result to water, and
aggregates visible water pixels into target H3 cells. Internally it keeps richer
diagnostics such as observer-sample fraction, visible-water area fraction,
visible pixel counts, and near/mean/far view distances. The persisted pair
factor is compact by design and stores only `weight_terrain` plus the H3 keys.

Core assumptions
----------------
- The endpoint DEM supplies observer ground and water-target elevations.
- When ``viewshed.surface_model=canopy``, intervening land obstruction uses
  ``DTM + CHM`` while sampled observer pixels remain at DTM elevation.
- Land cover is not part of this hard line-of-sight calculation.
- Observer eye height and target height come from the active viewshed config.
- Source and target H3 resolutions are treated as aligned for this workflow.
- Water targets are derived from the configured water/land domain, not from a
  dense all-source/all-target matrix.
- `weight_terrain` is a visibility-support factor, not a literal percent
  obscured. It is clipped to [0, 1] before being written.

Downstream contract
-------------------
The final viewability stage multiplies this distance-integrated factor only by
conditional vegetation attenuation:

    physical_view_score =
        weight_terrain * weight_vegetation

The centroid distance weight remains a diagnostic and is not applied twice.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import polars as pl
import rasterio
from affine import Affine
from rasterio.crs import CRS

from viewshed_toolkit._internal.geo.raster import remove_partial

from ...config import (
    AppConfig,
    BatchContext,
    normalize_viewshed_backend,
    timer,
)
from ...prepare.area import domains

LOGGER = logging.getLogger(__name__)
_AREA_LOOKUP_TARGET_CACHE: dict[tuple[str, str], set[str]] = {}
_LOOKUP_TARGETS_BY_SOURCE_CACHE: dict[tuple[Any, ...], dict[str, set[str]]] = {}
_TARGET_WATER_AREA_BY_H3_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_WATER_TERRAIN_DOMAIN_CACHE: dict[str, domains.DomainGeometries] = {}
_WATER_REPRESENTATIVE_POINT_CACHE: dict[tuple[str, str], Any] = {}
_PREPARED_LAND_DOMAIN_CACHE: dict[str, Any] = {}
_WATER_TERRAIN_PREFILTER_CACHE: dict[
    tuple[str, str, str, int], tuple[pd.DataFrame, set[str] | None]
] = {}

try:
    import xarray as xr
except ImportError:
    xr = None

try:
    import rioxarray
except ImportError:
    rioxarray = None

_XR_VIEW_SHED = None
_XR_VIEW_SHED_IMPORT_ERROR: Exception | None = None
_GDAL_PYTHON = None
_GDAL_PYTHON_IMPORT_ERROR: Exception | None = None
_GDAL_PYTHON_LOCK = threading.Lock()
_GDAL_CLI_FALLBACK_NOTICE_LOGGED = False
_GDAL_WORKER_DATASETS = threading.local()


def _load_gdal_python():
    """Load the Python GDAL viewshed API without changing global error mode."""

    global _GDAL_PYTHON, _GDAL_PYTHON_IMPORT_ERROR
    if _GDAL_PYTHON is not None:
        return _GDAL_PYTHON
    if _GDAL_PYTHON_IMPORT_ERROR is not None:
        raise ImportError(
            "In-process GDAL viewsheds require osgeo.gdal.ViewshedGenerate."
        ) from _GDAL_PYTHON_IMPORT_ERROR
    # Multiple observer threads can arrive here together. Serializing the first
    # import prevents one thread from inspecting a partially initialized osgeo
    # module and permanently poisoning the process-wide capability cache.
    with _GDAL_PYTHON_LOCK:
        if _GDAL_PYTHON is not None:
            return _GDAL_PYTHON
        if _GDAL_PYTHON_IMPORT_ERROR is not None:
            raise ImportError(
                "In-process GDAL viewsheds require osgeo.gdal.ViewshedGenerate."
            ) from _GDAL_PYTHON_IMPORT_ERROR
        try:
            from osgeo import gdal

            if not hasattr(gdal, "ViewshedGenerate"):
                raise AttributeError("osgeo.gdal.ViewshedGenerate is unavailable")
        except Exception as exc:
            _GDAL_PYTHON_IMPORT_ERROR = exc
            raise ImportError(
                "In-process GDAL viewsheds require osgeo.gdal.ViewshedGenerate."
            ) from exc
        _GDAL_PYTHON = gdal
        return gdal


def _log_gdal_cli_fallback_once(error: Exception) -> None:
    """Report a missing optional Python API once, not once per observer."""

    global _GDAL_CLI_FALLBACK_NOTICE_LOGGED
    with _GDAL_PYTHON_LOCK:
        if _GDAL_CLI_FALLBACK_NOTICE_LOGGED:
            return
        _GDAL_CLI_FALLBACK_NOTICE_LOGGED = True
        LOGGER.info(
            "GDAL Python ViewshedGenerate is unavailable; using the gdal_viewshed "
            "CLI for this process. reason=%s: %s",
            type(error).__name__,
            error,
        )


def _worker_gdal_dataset(dem_path: Path):
    """Return a thread-owned read-only GDAL dataset for the active DEM.

    The observer executor uses threads. GDAL raster-band access is not shared
    across them: every worker owns its own handles, with at most the current
    bare-earth and canopy surfaces retained in that worker's small cache.
    """

    gdal = _load_gdal_python()
    path = Path(dem_path).resolve()
    stat = path.stat()
    key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
    datasets = getattr(_GDAL_WORKER_DATASETS, "datasets", None)
    if datasets is None:
        datasets = {}
        _GDAL_WORKER_DATASETS.datasets = datasets
    dataset = datasets.get(key)
    if dataset is not None:
        return gdal, dataset

    # A paired batch needs no more than bare-earth and canopy handles. Avoid
    # retaining stale handles if a caller reuses a worker across many batches.
    if len(datasets) >= 2:
        datasets.clear()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Neither gdal.UseExceptions.*",
            category=FutureWarning,
        )
        dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
    if dataset is None:
        message = str(gdal.GetLastErrorMsg() or "unknown GDAL open error")
        raise RuntimeError(f"GDAL could not open analysis DEM {path}: {message}")
    datasets[key] = dataset
    return gdal, dataset


def _load_xrspatial_viewshed():
    global _XR_VIEW_SHED, _XR_VIEW_SHED_IMPORT_ERROR
    if _XR_VIEW_SHED is not None:
        return _XR_VIEW_SHED
    if _XR_VIEW_SHED_IMPORT_ERROR is not None:
        raise ImportError(
            "viewshed.backend='xrspatial' requires xarray-spatial."
        ) from _XR_VIEW_SHED_IMPORT_ERROR
    try:
        from xrspatial import viewshed as xr_viewshed
    except Exception as exc:
        _XR_VIEW_SHED_IMPORT_ERROR = exc
        raise ImportError("viewshed.backend='xrspatial' requires xarray-spatial.") from exc
    _XR_VIEW_SHED = xr_viewshed
    return xr_viewshed


# =============================================================================
# Viewshed phase 1: baseline clear-sky land H3 viewshed modeling
# =============================================================================
#
# This module contains the actual phase 1 viewshed calculation: GDAL viewshed
# execution, per-observer accumulation, H3 aggregation, partition writing, and CLI
# orchestration. Shared config/path, DEM preparation, water masking, H3 sampling,
# and source-cell input helpers live in utils.py for reuse by later phases.
#
# =============================================================================


@dataclass(frozen=True)
class SourceViewshedResult:
    source_h3_cell: str
    partition_path: Path | None
    n_rows: int
    n_visible_target_cells: int
    open_water_shortcut_pairs: int = 0
    dem_viewshed_pairs: int = 0
    cumulative_visible_count_raster: Path | None = None
    cumulative_min_distance_raster: Path | None = None
    cumulative_mean_distance_raster: Path | None = None
    cumulative_max_distance_raster: Path | None = None
    # Only populated for single-cell/debug/map workflows. Production batch runs
    # return summary stats to avoid holding large GeoDataFrames in memory.
    combined_h3: pd.DataFrame | gpd.GeoDataFrame | None = None
    combined_observers: gpd.GeoDataFrame | None = None


@dataclass(frozen=True)
class ViewshedWindowResult:
    visible: np.ndarray
    y_start: int
    x_start: int
    elapsed_seconds: float
    metadata: dict[str, Any]


GDAL_VIEWSHED_CACHE_VERSION = "gdal_viewshed_observer_v2"


def _gdal_viewshed_fingerprint(
    dem_path: Path,
    *,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
) -> str:
    dem = Path(dem_path).resolve()
    stat = dem.stat()
    payload = {
        "algorithm_version": GDAL_VIEWSHED_CACHE_VERSION,
        "dem": {
            "path": str(dem),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        },
        "observer_x": float(observer_x),
        "observer_y": float(observer_y),
        "observer_height_m": float(observer_height_m),
        "target_height_m": float(target_height_m),
        "max_distance_m": float(max_distance_m),
        "curvature_coefficient": float(curvature_coefficient),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_gdal_viewshed(
    dem_path: Path,
    out_path: Path,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    overwrite: bool = False,
) -> Path:
    """Run GDAL viewshed quietly and only surface output on hard failure.

    GDAL's command-line progress meter is very noisy when many observers are run
    concurrently. `--quiet` suppresses progress/non-error output, and capturing
    stdout/stderr prevents interleaved warning/progress text from flooding the
    notebook or terminal. If GDAL exits non-zero, the captured output is included
    in the exception so debugging information is not lost.
    """
    cache_fingerprint = _gdal_viewshed_fingerprint(
        dem_path,
        observer_x=observer_x,
        observer_y=observer_y,
        observer_height_m=observer_height_m,
        target_height_m=target_height_m,
        max_distance_m=max_distance_m,
        curvature_coefficient=curvature_coefficient,
    )
    if out_path.exists() and not overwrite:
        try:
            with rasterio.open(out_path) as existing:
                if existing.tags().get("cache_fingerprint") == cache_fingerprint:
                    return out_path
        except (OSError, rasterio.errors.RasterioError):
            pass
        LOGGER.info("Rebuilding stale per-observer GDAL viewshed: %s", out_path)
        out_path.unlink(missing_ok=True)
    elif out_path.exists():
        out_path.unlink(missing_ok=True)

    gdal_viewshed = shutil.which("gdal_viewshed")
    if gdal_viewshed is None:
        raise RuntimeError(
            "gdal_viewshed was not found on PATH. Install GDAL command-line utilities."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        gdal_viewshed,
        "--quiet",
        "-b",
        "1",
        "-ox",
        str(observer_x),
        "-oy",
        str(observer_y),
        "-oz",
        str(observer_height_m),
        "-tz",
        str(target_height_m),
        "-md",
        str(max_distance_m),
        "-cc",
        str(curvature_coefficient),
        "-vv",
        "1",
        "-iv",
        "0",
        "-ov",
        "0",
        str(dem_path),
        str(out_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            details = "\n".join(
                part.strip() for part in [result.stdout, result.stderr] if part and part.strip()
            )
            raise RuntimeError(
                f"gdal_viewshed failed with exit code {result.returncode}."
                + (f"\n{details}" if details else "")
            )
    except Exception:
        remove_partial(out_path)
        raise

    with rasterio.open(out_path, "r+") as output:
        output.update_tags(
            algorithm_version=GDAL_VIEWSHED_CACHE_VERSION,
            cache_fingerprint=cache_fingerprint,
        )

    return out_path


def load_xrspatial_dem_dataarray(path: Path):
    if rioxarray is None:
        raise ImportError(
            "xrspatial backend requires rioxarray. Install with: pip install rioxarray"
        )

    da = rioxarray.open_rasterio(path, masked=True)

    if "band" in da.dims:
        da = da.squeeze("band", drop=True)

    if "x" not in da.dims or "y" not in da.dims:
        raise ValueError(f"Expected DEM DataArray with x/y dimensions. Got dims={da.dims}")

    if da.rio.crs is None:
        raise ValueError(f"DEM is missing CRS: {path}")

    return da


def load_xrspatial_analysis_dem_dataarray(path: Path):
    da = load_xrspatial_dem_dataarray(path).astype("float32")
    values = np.asarray(da.values)
    finite = np.isfinite(values)
    if bool(finite.all()):
        da.attrs["xrspatial_nodata_filled"] = False
        da.attrs["xrspatial_nodata_pixel_count"] = 0
        da.attrs["xrspatial_nodata_fill_value_m"] = None
        return da
    if not bool(finite.any()):
        raise ValueError(f"xrspatial DEM has no finite elevation values: {path}")

    fill_value = np.float32(np.nanmin(values[finite]))
    filled = np.where(finite, values, fill_value).astype("float32", copy=False)
    sanitized = xr.DataArray(filled, dims=da.dims, coords=da.coords, attrs=da.attrs)
    sanitized.attrs["xrspatial_nodata_filled"] = True
    sanitized.attrs["xrspatial_nodata_pixel_count"] = int((~finite).sum())
    sanitized.attrs["xrspatial_nodata_fill_value_m"] = float(fill_value)
    return sanitized


def crop_dem_da_to_observer_window(
    dem_da,
    *,
    observer_x: float,
    observer_y: float,
    max_distance_m: float,
):
    xmin = observer_x - max_distance_m
    xmax = observer_x + max_distance_m
    ymin = observer_y - max_distance_m
    ymax = observer_y + max_distance_m

    y_values = dem_da["y"].values
    y_slice = slice(ymax, ymin) if y_values[0] > y_values[-1] else slice(ymin, ymax)
    window = dem_da.sel(x=slice(xmin, xmax), y=y_slice)

    if window.size == 0 or 0 in window.shape:
        raise ValueError(
            "xrspatial DEM window is empty. Check observer coordinates, DEM bounds, and CRS."
        )

    return window


def apply_curvature_correction_to_dem_da(
    dem_da,
    *,
    observer_x: float,
    observer_y: float,
    curvature_coefficient: float,
    earth_radius_m: float,
):
    """
    Apply GDAL-style curvature/refraction correction to a DEM window.

    Formula:
        corrected_height = dem_height - curvature_coefficient * distance_m^2 / (2 * earth_radius_m)

    This mirrors GDAL's documented DEM correction:
        HeightCorrected = HeightDEM - CurvCoeff * TargetDistance^2 / SphereDiameter

    where:
        SphereDiameter = 2 * earth_radius_m
    """
    if curvature_coefficient == 0:
        return dem_da
    if xr is None:
        raise ImportError("xrspatial backend requires xarray. Install with: pip install xarray")

    if "x" not in dem_da.coords or "y" not in dem_da.coords:
        raise ValueError("DEM DataArray must have x/y coordinates for curvature correction.")

    x = np.asarray(dem_da["x"].values, dtype="float32")
    y = np.asarray(dem_da["y"].values, dtype="float32")
    dx2 = (x - np.float32(observer_x)) ** np.float32(2.0)
    dy2 = (y - np.float32(observer_y)) ** np.float32(2.0)
    curvature_drop_m = (
        np.float32(curvature_coefficient)
        * (dy2[:, None] + dx2[None, :])
        / np.float32(2.0 * float(earth_radius_m))
    )

    corrected_values = np.asarray(dem_da.values, dtype="float32") - curvature_drop_m
    corrected = xr.DataArray(
        corrected_values,
        dims=dem_da.dims,
        coords=dem_da.coords,
        attrs=dict(dem_da.attrs),
    )
    corrected.attrs["curvature_corrected"] = True
    corrected.attrs["curvature_coefficient"] = float(curvature_coefficient)
    corrected.attrs["earth_radius_m"] = float(earth_radius_m)
    return corrected


def _coord_start_index(full_values: np.ndarray, first_window_value: float) -> int:
    matches = np.where(np.isclose(full_values, first_window_value))[0]
    if len(matches) == 0:
        raise ValueError("Window coordinates do not align with the full DEM grid.")
    return int(matches[0])


def window_offsets_in_full_grid(*, window_da, full_da) -> tuple[int, int]:
    full_x = np.asarray(full_da["x"].values)
    full_y = np.asarray(full_da["y"].values)
    win_x = np.asarray(window_da["x"].values)
    win_y = np.asarray(window_da["y"].values)
    return (
        _coord_start_index(full_y, win_y[0]),
        _coord_start_index(full_x, win_x[0]),
    )


def mask_visible_window_to_max_distance(
    visible_window: np.ndarray,
    *,
    window_da,
    observer_x: float,
    observer_y: float,
    max_distance_m: float,
) -> np.ndarray:
    x = np.asarray(window_da["x"].values, dtype="float64")
    y = np.asarray(window_da["y"].values, dtype="float64")
    xx, yy = np.meshgrid(x, y)
    within_distance = ((xx - observer_x) ** 2 + (yy - observer_y) ** 2) <= (
        float(max_distance_m) ** 2
    )
    return np.asarray(visible_window, dtype=bool) & within_distance


def call_xrspatial_viewshed(
    viewshed_callable,
    dem_da,
    *,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
) -> tuple[Any, bool]:
    kwargs = {
        "x": observer_x,
        "y": observer_y,
        "observer_elev": float(observer_height_m),
        "target_elev": float(target_height_m),
    }
    try:
        signature = inspect.signature(viewshed_callable)
    except (TypeError, ValueError):
        signature = None

    used_native_max_distance = signature is not None and "max_distance" in signature.parameters
    if used_native_max_distance:
        kwargs["max_distance"] = float(max_distance_m)

    return viewshed_callable(dem_da, **kwargs), used_native_max_distance


def estimate_xrspatial_peak_memory_bytes(
    *,
    max_distance_m: float,
    dem_resolution_m: float,
) -> int:
    cell_size = max(float(dem_resolution_m), 1.0)
    radius_cells = int(np.ceil(float(max_distance_m) / cell_size))
    side = (2 * radius_cells) + 1
    return int(500 * side * side)


def available_memory_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def xrspatial_source_worker_count(
    *,
    requested_workers: int,
    pending_cell_count: int,
    max_distance_m: float,
    dem_resolution_m: float,
) -> int:
    requested = max(1, min(int(requested_workers), int(pending_cell_count)))
    peak = estimate_xrspatial_peak_memory_bytes(
        max_distance_m=max_distance_m,
        dem_resolution_m=dem_resolution_m,
    )
    available = available_memory_bytes()
    if available is None:
        return requested

    if peak > 0.5 * available:
        raise MemoryError(
            "xrspatial viewshed cannot run this configuration in the current "
            "memory budget. Estimated peak memory per observer is "
            f"{peak / 1e9:.2f} GB for max_distance_m={float(max_distance_m):.0f} "
            f"and dem_resolution_m={float(dem_resolution_m):.0f}; xrspatial "
            f"requires this to be <= 50% of available RAM ({available / 1e9:.2f} GB). "
            "Reduce viewshed.max_distance_m, use a coarser DEM, free memory, or use "
            "viewshed.backend='gdal'."
        )

    memory_limited_workers = max(1, int((0.5 * available) // max(peak, 1)))
    return max(1, min(requested, memory_limited_workers))


def _is_north_up_transform(transform: Any, *, tol: float = 1e-9) -> bool:
    """Return True when a raster transform has no rotation/shear."""
    return abs(float(transform.b)) <= tol and abs(float(transform.d)) <= tol


def _transforms_match_strict(left: Any, right: Any, *, tol: float = 1e-7) -> bool:
    """Return True when every affine component matches within ``tol``."""

    return all(abs(float(a) - float(b)) <= tol for a, b in zip(left, right, strict=True))


def _grid_aligned_window_offsets(
    *,
    child_transform: Any,
    child_shape: tuple[int, int],
    parent_transform: Any,
    parent_shape: tuple[int, int],
    path: Path | str,
    tol: float = 1e-7,
) -> tuple[int, int]:
    """Return row/column offset for a cropped child raster on the parent grid.

    GDAL viewshed commonly writes a cropped raster centered on the observer. The
    cropped raster is already in the same CRS/resolution as the analysis grid, so
    warping it per observer is unnecessary. This helper verifies grid alignment
    and computes the integer parent-grid offset where the child array should be
    pasted/accumulated.
    """
    if not _is_north_up_transform(child_transform) or not _is_north_up_transform(parent_transform):
        raise ValueError(
            "GDAL viewshed output or parent water grid is rotated/sheared. "
            "Per-observer reprojection is disabled; align rasters upstream. "
            f"path={path} child_transform={child_transform} parent_transform={parent_transform}"
        )

    child_px_x = float(child_transform.a)
    parent_px_x = float(parent_transform.a)
    child_px_y = float(child_transform.e)
    parent_px_y = float(parent_transform.e)
    if abs(child_px_x - parent_px_x) > tol or abs(child_px_y - parent_px_y) > tol:
        raise ValueError(
            "GDAL viewshed output pixel size differs from parent water grid. "
            "Per-observer reprojection is disabled; align rasters upstream. "
            f"path={path} child_px=({child_px_x},{child_px_y}) parent_px=({parent_px_x},{parent_px_y})"
        )

    col_float = (float(child_transform.c) - float(parent_transform.c)) / parent_px_x
    row_float = (float(child_transform.f) - float(parent_transform.f)) / parent_px_y
    col_start = int(round(col_float))
    row_start = int(round(row_float))
    expected_child_c = float(parent_transform.c) + col_start * parent_px_x
    expected_child_f = float(parent_transform.f) + row_start * parent_px_y
    if (
        abs(float(child_transform.c) - expected_child_c) > tol
        or abs(float(child_transform.f) - expected_child_f) > tol
    ):
        raise ValueError(
            "GDAL viewshed output is not aligned to an integer parent-grid offset. "
            "Per-observer reprojection is disabled; align rasters upstream. "
            f"path={path} row_offset_pixels={row_float} col_offset_pixels={col_float} "
            f"origin_tolerance={tol} "
            f"child_transform={child_transform} parent_transform={parent_transform}"
        )

    child_h, child_w = int(child_shape[0]), int(child_shape[1])
    parent_h, parent_w = int(parent_shape[0]), int(parent_shape[1])
    if (
        row_start < 0
        or col_start < 0
        or row_start + child_h > parent_h
        or col_start + child_w > parent_w
    ):
        raise ValueError(
            "GDAL viewshed output window falls outside parent water grid. "
            f"path={path} row_start={row_start} col_start={col_start} "
            f"child_shape={child_shape} parent_shape={parent_shape} "
            f"child_transform={child_transform} parent_transform={parent_transform}"
        )
    return row_start, col_start


def _validated_gdal_grid_window_offsets(
    *,
    child_crs: Any,
    child_transform: Any,
    child_shape: tuple[int, int],
    parent_crs: Any,
    parent_transform: Any,
    parent_shape: tuple[int, int],
    path: Path | str,
    parent_path: Path | str,
) -> tuple[int, int]:
    """Validate a GDAL output as the full parent grid or an exact grid subset.

    Binary visibility rasters are never silently warped. The child must have the
    same CRS, signed pixel sizes, and orientation as the parent, and its affine
    origin must resolve to an exact integer-pixel offset within the parent.
    Equal dimensions therefore require the exact same origin.
    """

    if child_crs is None or parent_crs is None:
        raise ValueError(
            "GDAL viewshed output and parent water grid must both declare a CRS. "
            f"path={path} child_crs={child_crs} parent_path={parent_path} "
            f"parent_crs={parent_crs}"
        )
    if child_crs != parent_crs:
        raise ValueError(
            "GDAL viewshed output CRS differs from parent water grid. "
            "Automatic reprojection of binary visibility is disabled. "
            f"path={path} child_crs={child_crs} parent_path={parent_path} "
            f"parent_crs={parent_crs}"
        )
    if int(child_shape[0]) <= 0 or int(child_shape[1]) <= 0:
        raise ValueError(
            "GDAL viewshed output has invalid raster dimensions. "
            f"path={path} child_shape={child_shape}"
        )

    return _grid_aligned_window_offsets(
        child_transform=child_transform,
        child_shape=child_shape,
        parent_transform=parent_transform,
        parent_shape=parent_shape,
        path=path,
    )


def _gdal_result_metadata(
    *,
    context: BatchContext,
    app: AppConfig,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    execution_mode: str,
    uses_external_process: bool,
    uses_temp_raster: bool,
) -> dict[str, Any]:
    return {
        "backend": "gdal",
        "gdal_execution_mode": execution_mode,
        "terrain_surface_model": str(app.viewshed.surface_model),
        "analysis_surface_path": str(context.analysis_dem_path),
        "endpoint_dem_path": str(context.endpoint_dem_path),
        "aligned_canopy_height_path": (
            str(context.aligned_canopy_height_path)
            if context.aligned_canopy_height_path is not None
            else None
        ),
        "observer_base_surface": "endpoint_dtm",
        "target_base_surface": "water_flattened_endpoint_dem",
        "landcover_used": False,
        "curvature_applied": float(curvature_coefficient) != 0.0,
        "curvature_method": "gdal_viewshed_cc",
        "curvature_coefficient": float(curvature_coefficient),
        "observer_height_m": float(observer_height_m),
        "target_height_m": float(target_height_m),
        "grid_alignment": "validated_parent_grid_window",
        "max_distance_m": float(max_distance_m),
        "uses_external_process": bool(uses_external_process),
        "uses_temp_raster": bool(uses_temp_raster),
        "experimental": False,
    }


def _validate_context_parent_grid(
    *,
    context: BatchContext,
    parent_crs: Any,
    parent_transform: Any,
    parent_shape: tuple[int, int],
    source_cell: str,
    sample_index: int,
    parent_path: Path | str,
) -> None:
    expected_shape = tuple(int(value) for value in context.water_mask_arr.shape)
    if tuple(int(value) for value in parent_shape) != expected_shape:
        raise ValueError(
            "Batch water-mask array dimensions differ from its parent raster. "
            f"source_h3_cell={source_cell} sample_index={sample_index} "
            f"parent_path={parent_path} array_shape={expected_shape} "
            f"raster_shape={parent_shape}"
        )
    if not _transforms_match_strict(parent_transform, context.water_transform):
        raise ValueError(
            "Batch water-mask transform differs from the transform stored in its context. "
            f"source_h3_cell={source_cell} sample_index={sample_index} "
            f"parent_path={parent_path} raster_transform={parent_transform} "
            f"context_transform={context.water_transform}"
        )
    if parent_crs is None:
        raise ValueError(
            "Batch parent raster must declare a CRS. "
            f"source_h3_cell={source_cell} sample_index={sample_index} "
            f"parent_path={parent_path}"
        )


def _context_water_grid(context: BatchContext) -> tuple[Any, Any, tuple[int, int]]:
    """Return batch-owned water-grid metadata without reopening its raster.

    The fallback keeps manually constructed/older contexts usable, while
    production contexts always carry these values from batch preparation.
    """

    if context.water_crs is not None and context.water_shape is not None:
        return (
            context.water_crs,
            context.water_transform,
            tuple(int(value) for value in context.water_shape),
        )
    with rasterio.open(context.water_mask_path) as parent:
        return (
            parent.crs,
            parent.transform,
            (int(parent.height), int(parent.width)),
        )


def _validated_viewshed_array_offsets(
    *,
    visible: np.ndarray,
    child_crs: Any,
    child_transform: Any,
    context: BatchContext,
    parent_crs: Any,
    parent_transform: Any,
    parent_shape: tuple[int, int],
    source_cell: str,
    sample_index: int,
    observer_id: str,
    path: Path | str,
    parent_path: Path | str,
) -> tuple[int, int]:
    child_shape = tuple(int(value) for value in visible.shape)
    try:
        return _validated_gdal_grid_window_offsets(
            child_crs=child_crs,
            child_transform=child_transform,
            child_shape=child_shape,
            parent_crs=parent_crs,
            parent_transform=parent_transform,
            parent_shape=parent_shape,
            path=path,
            parent_path=parent_path,
        )
    except ValueError as exc:
        raise ValueError(
            "GDAL viewshed grid validation failed. "
            f"source_h3_cell={source_cell} sample_index={sample_index} "
            f"observer_id={observer_id}. {exc}"
        ) from exc


def _run_gdal_viewshed_in_process_to_bool_array(
    *,
    context: BatchContext,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    app: AppConfig,
    source_cell: str,
    sample_index: int,
    observer_id: str,
) -> ViewshedWindowResult:
    """Run one viewshed through GDAL's Python API and MEM output driver."""

    with timer(f"gdal_viewshed_in_process:{source_cell}:{sample_index}") as gdal_timer:
        gdal, dem_dataset = _worker_gdal_dataset(context.analysis_dem_path)
        parent_shape = (int(dem_dataset.RasterYSize), int(dem_dataset.RasterXSize))
        parent_transform = Affine.from_gdal(*dem_dataset.GetGeoTransform())
        parent_wkt = str(dem_dataset.GetProjectionRef() or "")
        parent_crs = CRS.from_wkt(parent_wkt) if parent_wkt else None
        _validate_context_parent_grid(
            context=context,
            parent_crs=parent_crs,
            parent_transform=parent_transform,
            parent_shape=parent_shape,
            source_cell=source_cell,
            sample_index=sample_index,
            parent_path=context.analysis_dem_path,
        )

        gdal.ErrorReset()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            output_dataset = gdal.ViewshedGenerate(
                dem_dataset.GetRasterBand(1),
                "MEM",
                "",
                [],
                float(observer_x),
                float(observer_y),
                float(observer_height_m),
                float(target_height_m),
                1.0,
                0.0,
                0.0,
                -1.0,
                float(curvature_coefficient),
                gdal.GVM_Edge,
                float(max_distance_m),
                None,
                None,
                gdal.GVOT_NORMAL,
                [],
            )
            error_message = str(gdal.GetLastErrorMsg() or "")
        finally:
            gdal.PopErrorHandler()
        if output_dataset is None:
            raise RuntimeError(
                "gdal.ViewshedGenerate returned no dataset"
                + (f": {error_message}" if error_message else "")
            )

        output_band = output_dataset.GetRasterBand(1)
        output_array = output_band.ReadAsArray() if output_band is not None else None
        if output_array is None:
            raise RuntimeError("gdal.ViewshedGenerate produced no readable output band.")
        if np.asarray(output_array).ndim != 2:
            raise ValueError(
                "In-process GDAL viewshed output must be two-dimensional: "
                f"shape={np.asarray(output_array).shape}"
            )
        visible = np.asarray(output_array == 1, dtype=bool)
        child_transform = Affine.from_gdal(*output_dataset.GetGeoTransform())
        child_wkt = str(output_dataset.GetProjectionRef() or "")
        child_crs = CRS.from_wkt(child_wkt) if child_wkt else None
        row_start, col_start = _validated_viewshed_array_offsets(
            visible=visible,
            child_crs=child_crs,
            child_transform=child_transform,
            context=context,
            parent_crs=parent_crs,
            parent_transform=parent_transform,
            parent_shape=parent_shape,
            source_cell=source_cell,
            sample_index=sample_index,
            observer_id=observer_id,
            path="GDAL_MEM",
            parent_path=context.analysis_dem_path,
        )
        output_dataset = None

    elapsed = float(gdal_timer["elapsed_seconds"] or 0.0)
    LOGGER.info(
        "clear_sky_observer_profile batch_id=%s source_h3_cell=%s sample_index=%d "
        "backend=gdal execution_mode=in_process_mem elapsed_seconds=%.3f "
        "output_shape=%s row_start=%d col_start=%d",
        context.batch_id,
        source_cell,
        sample_index,
        elapsed,
        visible.shape,
        row_start,
        col_start,
    )
    return ViewshedWindowResult(
        visible=visible,
        y_start=row_start,
        x_start=col_start,
        elapsed_seconds=elapsed,
        metadata=_gdal_result_metadata(
            context=context,
            app=app,
            observer_height_m=observer_height_m,
            target_height_m=target_height_m,
            max_distance_m=max_distance_m,
            curvature_coefficient=curvature_coefficient,
            execution_mode="in_process_mem",
            uses_external_process=False,
            uses_temp_raster=False,
        )
        | {"dataset_handle_scope": "thread_local_worker"},
    )


def _run_gdal_viewshed_cli_to_bool_array(
    *,
    context: BatchContext,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    app: AppConfig,
    source_cell: str,
    sample_index: int,
    observer_id: str,
) -> ViewshedWindowResult:
    safe_observer_id = str(observer_id).replace("/", "_")
    if app.run.keep_intermediate_rasters:
        tmp_dir = app.paths.output_dir / "observer_viewsheds" / source_cell
        tmp_dir.mkdir(parents=True, exist_ok=True)
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"{source_cell}_{sample_index:03d}_"))
    viewshed_path = tmp_dir / f"{safe_observer_id}_viewshed_binary.tif"

    row_start = 0
    col_start = 0
    try:
        with timer(f"gdal_viewshed:{source_cell}:{sample_index}") as gdal_timer:
            run_gdal_viewshed(
                context.analysis_dem_path,
                viewshed_path,
                observer_x,
                observer_y,
                observer_height_m,
                target_height_m,
                max_distance_m,
                curvature_coefficient,
                overwrite=app.run.overwrite,
            )
        elapsed = float(gdal_timer["elapsed_seconds"] or 0.0)

        parent_crs, parent_transform, parent_raster_shape = _context_water_grid(context)
        with rasterio.open(viewshed_path) as src:
            _validate_context_parent_grid(
                context=context,
                parent_crs=parent_crs,
                parent_transform=parent_transform,
                parent_shape=parent_raster_shape,
                source_cell=source_cell,
                sample_index=sample_index,
                parent_path=context.water_mask_path,
            )
            viewshed_bool = src.read(1) == 1
            child_shape = tuple(int(value) for value in viewshed_bool.shape)
            row_start, col_start = _validated_viewshed_array_offsets(
                visible=viewshed_bool,
                child_crs=src.crs,
                child_transform=src.transform,
                context=context,
                parent_crs=parent_crs,
                parent_transform=parent_transform,
                parent_shape=parent_raster_shape,
                source_cell=source_cell,
                sample_index=sample_index,
                observer_id=observer_id,
                path=viewshed_path,
                parent_path=context.water_mask_path,
            )
            if child_shape != parent_raster_shape:
                LOGGER.debug(
                    "gdal_viewshed output is cropped grid-aligned window; using offset alignment. "
                    "path=%s child_shape=%s parent_shape=%s row_start=%d col_start=%d",
                    viewshed_path,
                    child_shape,
                    parent_raster_shape,
                    row_start,
                    col_start,
                )

        LOGGER.info(
            "clear_sky_observer_profile batch_id=%s source_h3_cell=%s sample_index=%d "
            "backend=gdal elapsed_seconds=%.3f output_path=%s output_size_mb=%.3f",
            context.batch_id,
            source_cell,
            sample_index,
            elapsed,
            viewshed_path,
            (viewshed_path.stat().st_size / (1024.0 * 1024.0) if viewshed_path.exists() else 0.0),
        )
    finally:
        if not app.run.keep_intermediate_rasters:
            try:
                viewshed_path.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except Exception:
                pass

    return ViewshedWindowResult(
        visible=viewshed_bool,
        y_start=row_start,
        x_start=col_start,
        elapsed_seconds=elapsed,
        metadata=_gdal_result_metadata(
            context=context,
            app=app,
            observer_height_m=observer_height_m,
            target_height_m=target_height_m,
            max_distance_m=max_distance_m,
            curvature_coefficient=curvature_coefficient,
            execution_mode=(
                "cli_retained_debug" if app.run.keep_intermediate_rasters else "cli_fallback"
            ),
            uses_external_process=True,
            uses_temp_raster=not app.run.keep_intermediate_rasters,
        ),
    )


def run_gdal_viewshed_to_bool_array(**arguments: Any) -> ViewshedWindowResult:
    """Ground one observer on a private raster; preserve the shared canopy."""
    context = arguments["context"]
    app = arguments["app"]
    if str(getattr(getattr(app, "viewshed", None), "surface_model", "bare_earth")) != "canopy":
        return _run_gdal_viewshed_dispatch(**arguments)
    from ...prepare.elevation.canopy import isolate_observer_canopy_vrt

    with tempfile.TemporaryDirectory(
        prefix="observer_canopy_", dir=context.analysis_dem_path.parent
    ) as directory:
        surface = isolate_observer_canopy_vrt(
            base_surface_path=context.analysis_dem_path,
            endpoint_dem_path=context.endpoint_dem_path,
            output_path=Path(directory) / "surface.vrt",
            observer_x=arguments["observer_x"],
            observer_y=arguments["observer_y"],
            clearance_radius_m=app.viewshed.observer_canopy_clearance_radius_m,
        )
        return _run_gdal_viewshed_dispatch(
            **{**arguments, "context": replace(context, analysis_dem_path=surface)}
        )


def _run_gdal_viewshed_dispatch(
    *,
    context: BatchContext,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    app: AppConfig,
    source_cell: str,
    sample_index: int,
    observer_id: str,
) -> ViewshedWindowResult:
    """Use in-process GDAL normally, retaining the CLI path as a fallback."""

    arguments = {
        "context": context,
        "observer_x": observer_x,
        "observer_y": observer_y,
        "observer_height_m": observer_height_m,
        "target_height_m": target_height_m,
        "max_distance_m": max_distance_m,
        "curvature_coefficient": curvature_coefficient,
        "app": app,
        "source_cell": source_cell,
        "sample_index": sample_index,
        "observer_id": observer_id,
    }
    if app.run.keep_intermediate_rasters:
        return _run_gdal_viewshed_cli_to_bool_array(**arguments)

    try:
        _load_gdal_python()
    except ImportError as unavailable_error:
        _log_gdal_cli_fallback_once(unavailable_error)
        fallback = _run_gdal_viewshed_cli_to_bool_array(**arguments)
        return replace(
            fallback,
            metadata={
                **fallback.metadata,
                "gdal_execution_mode": "cli_fallback",
                "in_process_fallback_error": (
                    f"{type(unavailable_error).__name__}: {unavailable_error}"
                ),
            },
        )

    try:
        return _run_gdal_viewshed_in_process_to_bool_array(**arguments)
    except Exception as in_process_error:
        LOGGER.warning(
            "In-process GDAL viewshed failed; retrying with gdal_viewshed CLI. "
            "source_h3_cell=%s sample_index=%d observer_id=%s error=%s: %s",
            source_cell,
            sample_index,
            observer_id,
            type(in_process_error).__name__,
            in_process_error,
        )
        try:
            fallback = _run_gdal_viewshed_cli_to_bool_array(**arguments)
        except Exception as cli_error:
            raise RuntimeError(
                "Both in-process GDAL and the gdal_viewshed CLI fallback failed. "
                f"in_process_error={type(in_process_error).__name__}: "
                f"{in_process_error}; cli_error={type(cli_error).__name__}: {cli_error}"
            ) from cli_error
        return replace(
            fallback,
            metadata={
                **fallback.metadata,
                "gdal_execution_mode": "cli_fallback",
                "in_process_fallback_error": (
                    f"{type(in_process_error).__name__}: {in_process_error}"
                ),
            },
        )


def run_xrspatial_viewshed_to_bool_array(
    *,
    context: BatchContext,
    xrspatial_dem_da,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    earth_radius_m: float,
    source_cell: str,
    sample_index: int,
    observer_id: str,
) -> ViewshedWindowResult:
    if xr is None:
        raise ImportError("viewshed.backend='xrspatial' requires xarray-spatial and xarray.")
    xr_viewshed = _load_xrspatial_viewshed()
    if xrspatial_dem_da is None:
        raise ValueError("xrspatial_dem_da is required for viewshed.backend='xrspatial'.")

    with timer(f"xrspatial_viewshed:{source_cell}:{sample_index}") as viewshed_timer:
        dem_window = crop_dem_da_to_observer_window(
            xrspatial_dem_da,
            observer_x=observer_x,
            observer_y=observer_y,
            max_distance_m=max_distance_m,
        )
        nodata_meta = {
            "nodata_filled": bool(dem_window.attrs.get("xrspatial_nodata_filled", False)),
            "nodata_pixel_count": int(dem_window.attrs.get("xrspatial_nodata_pixel_count", 0) or 0),
            "nodata_fill_value_m": dem_window.attrs.get("xrspatial_nodata_fill_value_m"),
        }
        corrected_dem_window = apply_curvature_correction_to_dem_da(
            dem_window,
            observer_x=observer_x,
            observer_y=observer_y,
            curvature_coefficient=curvature_coefficient,
            earth_radius_m=earth_radius_m,
        )

        viewshed_callable = getattr(xr_viewshed, "viewshed", xr_viewshed)
        result, used_native_max_distance = call_xrspatial_viewshed(
            viewshed_callable,
            corrected_dem_window,
            observer_x=observer_x,
            observer_y=observer_y,
            observer_height_m=observer_height_m,
            target_height_m=target_height_m,
            max_distance_m=max_distance_m,
        )

        visible_window = np.asarray(result.values != -1, dtype=bool)
        if not used_native_max_distance:
            visible_window = mask_visible_window_to_max_distance(
                visible_window,
                window_da=corrected_dem_window,
                observer_x=observer_x,
                observer_y=observer_y,
                max_distance_m=max_distance_m,
            )
        y_start, x_start = window_offsets_in_full_grid(
            window_da=corrected_dem_window,
            full_da=xrspatial_dem_da,
        )

    elapsed = float(viewshed_timer["elapsed_seconds"] or 0.0)
    LOGGER.info(
        "clear_sky_observer_profile batch_id=%s source_h3_cell=%s sample_index=%d "
        "backend=xrspatial elapsed_seconds=%.3f observer_id=%s",
        context.batch_id,
        source_cell,
        sample_index,
        elapsed,
        observer_id,
    )
    return ViewshedWindowResult(
        visible=visible_window,
        y_start=y_start,
        x_start=x_start,
        elapsed_seconds=elapsed,
        metadata={
            "backend": "xrspatial",
            "terrain_surface_model": (
                "canopy" if context.aligned_canopy_height_path else "bare_earth"
            ),
            "analysis_surface_path": str(context.analysis_dem_path),
            "endpoint_dem_path": str(context.endpoint_dem_path),
            "aligned_canopy_height_path": (
                str(context.aligned_canopy_height_path)
                if context.aligned_canopy_height_path is not None
                else None
            ),
            "observer_base_surface": "endpoint_dtm",
            "target_base_surface": "water_flattened_endpoint_dem",
            "landcover_used": False,
            "curvature_applied": float(curvature_coefficient) != 0.0,
            "curvature_method": "gdal_formula_dem_precorrection",
            "curvature_coefficient": float(curvature_coefficient),
            "earth_radius_m": float(earth_radius_m),
            "observer_height_m": float(observer_height_m),
            "target_height_m": float(target_height_m),
            "max_distance_m": float(max_distance_m),
            "uses_external_process": False,
            "uses_temp_raster": False,
            "experimental": True,
            "uses_native_max_distance": bool(used_native_max_distance),
            **nodata_meta,
        },
    )


def run_viewshed_backend_to_bool_array(
    *,
    backend: str,
    context: BatchContext,
    xrspatial_dem_da,
    observer_x: float,
    observer_y: float,
    observer_height_m: float,
    target_height_m: float,
    max_distance_m: float,
    curvature_coefficient: float,
    earth_radius_m: float,
    app: AppConfig,
    source_cell: str,
    sample_index: int,
    observer_id: str,
) -> ViewshedWindowResult:
    normalized = normalize_viewshed_backend(backend)
    if normalized == "gdal":
        return run_gdal_viewshed_to_bool_array(
            context=context,
            observer_x=observer_x,
            observer_y=observer_y,
            observer_height_m=observer_height_m,
            target_height_m=target_height_m,
            max_distance_m=max_distance_m,
            curvature_coefficient=curvature_coefficient,
            app=app,
            source_cell=source_cell,
            sample_index=sample_index,
            observer_id=observer_id,
        )
    if normalized == "xrspatial":
        return run_xrspatial_viewshed_to_bool_array(
            context=context,
            xrspatial_dem_da=xrspatial_dem_da,
            observer_x=observer_x,
            observer_y=observer_y,
            observer_height_m=observer_height_m,
            target_height_m=target_height_m,
            max_distance_m=max_distance_m,
            curvature_coefficient=curvature_coefficient,
            earth_radius_m=earth_radius_m,
            source_cell=source_cell,
            sample_index=sample_index,
            observer_id=observer_id,
        )
    raise ValueError(f"Unsupported viewshed backend: {backend}")


# -----------------------------------------------------------------------------
# Cumulative raster -> H3 conversion
# -----------------------------------------------------------------------------
