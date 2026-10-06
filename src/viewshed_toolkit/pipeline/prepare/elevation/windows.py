"""Bounded projected DEM windows on the exact established virtual regional grid."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import Window, from_bounds

from viewshed_toolkit._internal.geo.raster import update_geotiff_profile

from ...config import AppConfig
from ...config.study import require_shared_owned_output, with_study_config
from ...contracts.warp import GlobalWarpChunkPlan, validate_global_warp_chunk_plan
from .cache import cache_fingerprint, input_signature
from .terrain import validate_batch_guardrails


def _load_chunk_plan(
    app: AppConfig, source: Any, transform: Any, width: int, height: int
) -> tuple[GlobalWarpChunkPlan, dict[str, str | int]]:
    if app.batch.warp_chunk_plan_path is None:
        raise ValueError(
            "Cross-CRS/resolution windowing requires a pinned producer global warp chunk plan; "
            "warp parity is unqualified without it"
        )
    path = Path(app.batch.warp_chunk_plan_path)
    if not path.is_absolute():
        path = app.config_path.parent / path
    signature = input_signature(path)
    if signature["sha256"] != app.batch.warp_chunk_plan_sha256:
        raise ValueError("Global warp chunk plan SHA256 mismatch")
    expected = {
        "schema_version": 1,
        "method": "rasterio_global_gdal_chunks_v1",
        "source_sha256": input_signature(app.paths.regional_dem_path)["sha256"],
        "source_bytes": app.paths.regional_dem_path.stat().st_size,
        "transform": list(transform),
        "shape": [height, width],
        "crs": app.viewshed.crs_projected,
        "destination_block_size": app.raster.block_size,
        "rasterio_version": rasterio.__version__,
        "gdal_version": rasterio.__gdal_version__,
        "resampling": "bilinear",
        "transformer_tolerance": 0.125,
        "warp_memory_mib": 64,
        "num_threads": 1,
    }
    plan = validate_global_warp_chunk_plan(json.loads(path.read_text()), expected=expected)
    return plan, signature


@with_study_config
def ensure_projected_dem_window(app: AppConfig, bounds: tuple[float, float, float, float]) -> Path:
    """Stream one aligned window; never materialize the full regional DEM.

    Already aligned inputs are copied exactly. Other inputs replay pinned
    producer global warp chunks, keeping complete source context, unchanged
    bilinear resampling and the established approximate-transform tolerance.
    Only chunks intersecting the requested window are computed and cropped.
    No arbitrary tile, halo or VRT block is substituted for a producer chunk.
    """
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise ValueError("Projected window bounds must be finite")
    with rasterio.open(app.paths.regional_dem_path) as source:
        aligned = (
            source.crs == CRS.from_user_input(app.viewshed.crs_projected)
            and source.transform.b == 0
            and source.transform.d == 0
            and source.transform.a == app.viewshed.dem_resolution_m
            and source.transform.e == -app.viewshed.dem_resolution_m
        )
        plan = None
        plan_signature = None
        if aligned:
            transform, width, height = source.transform, source.width, source.height
        else:
            transform, width, height = calculate_default_transform(
                source.crs,
                app.viewshed.crs_projected,
                source.width,
                source.height,
                *source.bounds,
                resolution=app.viewshed.dem_resolution_m,
            )
            plan, plan_signature = _load_chunk_plan(app, source, transform, width, height)
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
        selected_chunks = []
        if plan is not None:
            for x, y, w, h in plan.chunks:
                chunk = Window(x, y, w, h)
                if not rasterio.windows.intersect(chunk, window):
                    continue
                validate_batch_guardrails(
                    batch_id="global_warp_context_chunk",
                    total_pixels=w * h,
                    estimated_memory_mb=w * h * 32 / 1024**2 + 64,
                    max_batch_aoi_pixels=app.batch.max_batch_aoi_pixels,
                    max_estimated_batch_memory_mb=app.batch.max_estimated_batch_memory_mb,
                )
                selected_chunks.append(chunk)
        identity = {
            "algorithm": "projected_dem_original_warp_chunks_window_v2",
            "source": input_signature(app.paths.regional_dem_path),
            "global_grid": {
                "transform": list(transform),
                "shape": [height, width],
                "crs": app.viewshed.crs_projected,
            },
            "window": [window.col_off, window.row_off, window.width, window.height],
            "resampling": "none_exact_source_window" if aligned else "bilinear",
            "producer_chunk_plan": plan_signature,
            "computed_chunks": [list(chunk.flatten()) for chunk in selected_chunks],
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
            if aligned:
                for _, block in destination.block_windows(1):
                    source_window = Window(
                        window.col_off + block.col_off,
                        window.row_off + block.row_off,
                        block.width,
                        block.height,
                    )
                    destination.write(source.read(1, window=source_window), 1, window=block)
            else:
                for chunk in selected_chunks:
                    # The original source band is kept intact: GDAL computes
                    # its original halo/context for the complete producer chunk.
                    values = np.empty((int(chunk.height), int(chunk.width)), dtype=source.dtypes[0])
                    reproject(
                        source=rasterio.band(source, 1),
                        destination=values,
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=source.nodata,
                        dst_nodata=source.nodata,
                        dst_transform=rasterio.windows.transform(chunk, transform),
                        dst_crs=app.viewshed.crs_projected,
                        resampling=Resampling.bilinear,
                        tolerance=0.125,
                        warp_mem_limit=64,
                        num_threads=1,
                    )
                    owned = chunk.intersection(window)
                    row, col = int(owned.row_off - chunk.row_off), int(
                        owned.col_off - chunk.col_off
                    )
                    target = Window(
                        owned.col_off - window.col_off,
                        owned.row_off - window.row_off,
                        owned.width,
                        owned.height,
                    )
                    destination.write(
                        values[row : row + int(owned.height), col : col + int(owned.width)],
                        1,
                        window=target,
                    )
                    del values
            destination.update_tags(
                algorithm_version=identity["algorithm"], cache_fingerprint=fingerprint
            )
    if (
        plan_signature is not None
        and input_signature(Path(str(plan_signature["path"]))) != plan_signature
    ):
        raise ValueError("Producer chunk plan changed during window reprojection")
    if input_signature(app.paths.regional_dem_path) != identity["source"]:
        raise ValueError("Source DEM changed during window reprojection")
    receipt: dict[str, Any] = {"identity": identity, "output": input_signature(path)}
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return path
