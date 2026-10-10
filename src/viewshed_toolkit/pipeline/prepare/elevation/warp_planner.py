"""Bounded native GDAL metadata-clone planning; no source pixels or global raster."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from contextlib import suppress
from importlib import resources
from pathlib import Path
from typing import Any

import psutil
import rasterio

from ...config import AppConfig
from ...config.study import require_shared_owned_output
from ...contracts.components import write_json
from ...contracts.warp import validate_global_warp_chunk_plan
from .cache import cache_fingerprint, input_signature


def planner_source_sha256() -> str:
    return hashlib.sha256(
        resources.files("viewshed_toolkit.resources").joinpath("native_warp_plan.cpp").read_bytes()
    ).hexdigest()


def nodata_identity(value: float | int | None) -> float | int | str | None:
    return "nan" if value is not None and value != value else value


def warp_plan_contract(
    app: AppConfig,
    source_path: Path,
    transform: Any,
    width: int,
    height: int,
    *,
    kind: str,
    schema_version: int = 2,
) -> dict[str, Any]:
    with rasterio.open(source_path) as source:
        if kind not in {"dem", "canopy"}:
            raise ValueError("Warp kind must be dem or canopy")
        expected = {
            "schema_version": schema_version,
            "method": (
                "rasterio_global_gdal_chunks_v1"
                if schema_version == 1
                else "native_gdal_metadata_clone_chunks_v1"
            ),
            "source_sha256": input_signature(source_path)["sha256"],
            "source_bytes": source_path.stat().st_size,
            "transform": list(transform),
            "shape": [height, width],
            "crs": app.viewshed.crs_projected,
            "destination_block_size": app.raster.block_size,
            "rasterio_version": rasterio.__version__,
            "gdal_version": rasterio.__gdal_version__,
            "resampling": "bilinear" if kind == "dem" else "max",
            "transformer_tolerance": 0.125,
            "warp_memory_mib": 64,
            "num_threads": 1,
        }
        if schema_version == 2:
            expected.update(
                source_crs=str(source.crs),
                source_transform=list(source.transform),
                source_shape=[source.height, source.width],
                source_block_size=list(source.block_shapes[0]),
                source_dtype=source.dtypes[0],
                source_nodata=nodata_identity(source.nodata),
                source_mask_flags=sum(int(flag) for flag in source.mask_flag_enums[0]),
                destination_dtype=source.dtypes[0] if kind == "dem" else "float32",
                destination_nodata=nodata_identity(source.nodata) if kind == "dem" else "nan",
                planner_implementation_sha256=planner_source_sha256(),
            )
    return expected


def generate_native_warp_chunk_plan(
    app: AppConfig, source_path: Path, transform: Any, width: int, height: int, *, kind: str
) -> Path:
    """Use an explicitly pinned helper; cap the child at 512 MiB and 60 seconds."""
    if app.batch.native_warp_helper_path is None:
        raise ValueError(
            "A pinned producer plan or native metadata warp helper is required; warp parity is unqualified without it"
        )
    helper = Path(app.batch.native_warp_helper_path)
    if not helper.is_absolute():
        helper = app.config_path.parent / helper
    signature = input_signature(helper)
    if signature["sha256"] != app.batch.native_warp_helper_sha256:
        raise ValueError("Native warp helper SHA256 mismatch")
    info = json.loads(subprocess.check_output([str(helper), "--info"], timeout=5, text=True))
    if info != {
        "gdal_version": rasterio.__gdal_version__,
        "source_code_sha256": planner_source_sha256(),
    }:
        raise ValueError("Native warp helper runtime/source version mismatch")
    expected = warp_plan_contract(app, source_path, transform, width, height, kind=kind)
    fingerprint = cache_fingerprint({"contract": expected, "helper": signature})
    root = app.paths.projected_dem_path.parent / "warp_plans" / kind / fingerprint[:20]
    path = root / "plan.json"
    require_shared_owned_output(app.raw_config, path)
    if path.exists():
        payload = json.loads(path.read_text())
        validate_global_warp_chunk_plan(payload, expected=expected)
        if payload["planner"]["backend_sha256"] != signature["sha256"]:
            raise ValueError("Cached native planner backend mismatch")
        return path
    root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    request_path, native_path = root / f"request-{run_id}.json", root / f"native-{run_id}.json"
    log_path = root / f"native-{run_id}.log"
    write_json(
        request_path,
        {
            "source": str(source_path.resolve()),
            "width": width,
            "height": height,
            "transform": list(transform.to_gdal()),
            "crs": app.viewshed.crs_projected,
            "block_size": app.raster.block_size,
            "resampling": expected["resampling"],
        },
    )
    start, peak, peak_total = time.monotonic(), 0, 0
    parent_monitor = psutil.Process()
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [str(helper), str(request_path), str(native_path)], stdout=log, stderr=log
        )
        monitor = psutil.Process(process.pid)
        try:
            while process.poll() is None:
                with suppress(psutil.NoSuchProcess):
                    child_rss = monitor.memory_info().rss
                    peak = max(peak, child_rss)
                    peak_total = max(peak_total, child_rss + parent_monitor.memory_info().rss)
                if (
                    peak > 512 * 1024**2
                    or peak_total > 768 * 1024**2
                    or time.monotonic() - start > 60
                    or log_path.stat().st_size > 8 * 1024**2
                ):
                    process.terminate()
                    raise RuntimeError(
                        "Native metadata planner stopped at RSS/time/log cap; evidence preserved"
                    )
                time.sleep(0.02)
            if process.returncode != 0:
                raise RuntimeError(
                    f"Native metadata planner failed; preserved diagnostic log: {log_path}"
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    if native_path.stat().st_size > 1024**2:
        raise ValueError("Native metadata plan exceeds bounded receipt size")
    native = json.loads(native_path.read_text())
    if (
        native["gdal_version"] != rasterio.__gdal_version__
        or native["source_code_sha256"] != planner_source_sha256()
    ):
        raise ValueError("Planner output runtime/source identity mismatch")
    if native["native_raster_pixel_reads"] != 0 or native["regional_rasters_materialized"] != 0:
        raise ValueError("Metadata planner read native pixels or materialized a regional raster")
    if (
        input_signature(helper) != signature
        or warp_plan_contract(app, source_path, transform, width, height, kind=kind) != expected
    ):
        raise ValueError("Native planner input/helper changed during planning")
    payload = {
        **expected,
        "chunks": native["chunks"],
        "planner": {
            "backend_sha256": signature["sha256"],
            "native_raster_pixel_reads": 0,
            "regional_rasters_materialized": 0,
            "dummy_read_calls": native["dummy_read_calls"],
            "dummy_read_bytes": native["dummy_read_bytes"],
            "metadata_elapsed_seconds": time.monotonic() - start,
            "peak_child_rss_bytes": peak,
            "peak_process_rss_bytes": peak_total,
        },
    }
    validate_global_warp_chunk_plan(payload, expected=expected)
    write_json(path, payload)
    return path
