"""Bounded projected DEM windows on the exact established virtual regional grid."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import rasterio
from rasterio.crs import CRS
from rasterio.windows import Window, from_bounds

from viewshed_toolkit._internal.geo.raster import update_geotiff_profile

from ...config import AppConfig
from ...config.study import require_shared_owned_output, with_study_config
from .cache import cache_fingerprint, input_signature
from .terrain import validate_batch_guardrails


@with_study_config
def ensure_projected_dem_window(app: AppConfig, bounds: tuple[float, float, float, float]) -> Path:
    """Stream one aligned window; never materialize the full regional DEM.

    Only already aligned projected inputs qualify. Cross-CRS virtual warps can
    differ from the established global warp because of GDAL chunk-dependent
    approximation/resampling; rejecting them preserves the scientific contract.
    """
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise ValueError("Projected window bounds must be finite")
    with rasterio.open(app.paths.regional_dem_path) as source:
        transform = source.transform
        if (
            source.crs != CRS.from_user_input(app.viewshed.crs_projected)
            or transform.b != 0
            or transform.d != 0
            or transform.a != app.viewshed.dem_resolution_m
            or transform.e != -app.viewshed.dem_resolution_m
        ):
            raise ValueError(
                "Windowed DEM requires an already aligned projected source; "
                "cross-CRS/resolution warp parity is unqualified"
            )
        width, height = source.width, source.height
        window = from_bounds(*bounds, transform).round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, width, height))
        if window.width <= 0 or window.height <= 0:
            raise ValueError("Projected window does not intersect the regional grid")
        pixels = int(window.width * window.height)
        validate_batch_guardrails(
            batch_id="projected_dem_window",
            total_pixels=pixels,
            estimated_memory_mb=pixels * 32 / 1024**2,
            max_batch_aoi_pixels=app.batch.max_batch_aoi_pixels,
            max_estimated_batch_memory_mb=app.batch.max_estimated_batch_memory_mb,
        )
        identity = {
            "algorithm": "projected_dem_aligned_native_window_v1",
            "source": input_signature(app.paths.regional_dem_path),
            "global_grid": {
                "transform": list(transform),
                "shape": [height, width],
                "crs": app.viewshed.crs_projected,
            },
            "window": [window.col_off, window.row_off, window.width, window.height],
            "resampling": "none_exact_source_window",
        }
        fingerprint = cache_fingerprint(identity)
        path = (
            app.paths.projected_dem_path.parent
            / "projected_dem_windows"
            / fingerprint[:20]
            / "projected_dem.tif"
        )
        require_shared_owned_output(app.raw_config, path)
        receipt_path = path.with_suffix(".tif.metadata.json")
        if path.exists() != receipt_path.exists():
            raise ValueError("Incomplete projected DEM window; preserve evidence before retry")
        if path.exists() and receipt_path.exists():
            cached_receipt = json.loads(receipt_path.read_text())
            if cached_receipt.get("identity") == identity and cached_receipt.get(
                "output"
            ) == input_signature(path):
                return path
            raise ValueError("Stale projected DEM window; use a new immutable source/grid identity")
        path.parent.mkdir(parents=True, exist_ok=True)
        profile = update_geotiff_profile(
            source.profile,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
            crs=app.viewshed.crs_projected,
            transform=rasterio.windows.transform(window, transform),
            width=int(window.width),
            height=int(window.height),
        )
        with (
            rasterio.Env(GDAL_CACHEMAX=64 * 1024**2),
            rasterio.open(path, "w", **profile) as destination,
        ):
            for _, block in destination.block_windows(1):
                virtual_window = Window(
                    window.col_off + block.col_off,
                    window.row_off + block.row_off,
                    block.width,
                    block.height,
                )
                destination.write(source.read(1, window=virtual_window), 1, window=block)
            destination.update_tags(
                algorithm_version=identity["algorithm"], cache_fingerprint=fingerprint
            )
    if input_signature(app.paths.regional_dem_path) != identity["source"]:
        raise ValueError("Source DEM changed during window reprojection")
    receipt: dict[str, Any] = {"identity": identity, "output": input_signature(path)}
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return path
