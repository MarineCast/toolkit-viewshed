"""Producer receipts for replaying the established global GDAL warp chunks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from affine import Affine
from shapely.geometry import box
from shapely.ops import unary_union


@dataclass(frozen=True)
class GlobalWarpChunkPlan:
    transform: Affine
    width: int
    height: int
    chunks: tuple[tuple[int, int, int, int], ...]


def validate_global_warp_chunk_plan(
    payload: dict[str, Any], *, expected: dict[str, Any]
) -> GlobalWarpChunkPlan:
    """Reject changed science/options, inputs, grids, gaps and overlapping chunks.

    Geometry alone cannot prove GDAL generated the recorded partition. A pinned,
    reviewed producer receipt is required; arbitrary administrative tiles are
    not interchangeable with the original warp's processing chunks.
    """
    version = expected.get("schema_version", 1)
    if version not in {1, 2}:
        raise ValueError("Unsupported global warp chunk schema")
    required = {*expected, "chunks", "producer_reference_sha256" if version == 1 else "planner"}
    if set(payload) != required or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError(
            "Global warp chunk plan does not match source/grid/method/runtime contract"
        )
    if version == 1:
        checksum = payload["producer_reference_sha256"]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise ValueError("Global warp chunk plan requires producer reference checksum")
    else:
        planner = payload["planner"]
        keys = {
            "backend_sha256",
            "native_raster_pixel_reads",
            "regional_rasters_materialized",
            "dummy_read_calls",
            "dummy_read_bytes",
            "metadata_elapsed_seconds",
            "peak_child_rss_bytes",
            "peak_process_rss_bytes",
        }
        if not isinstance(planner, dict) or set(planner) != keys:
            raise ValueError("Native planner receipt must prove metadata-clone scope")
        integer_fields = keys - {"backend_sha256", "metadata_elapsed_seconds"}
        if any(type(planner[key]) is not int or planner[key] < 0 for key in integer_fields):
            raise ValueError("Native planner counters must be nonnegative integers")
        elapsed = planner["metadata_elapsed_seconds"]
        if type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Native planner elapsed time must be finite and nonnegative")
        if planner["native_raster_pixel_reads"] or planner["regional_rasters_materialized"]:
            raise ValueError("Native planner receipt must prove metadata-clone scope")
        checksum = planner["backend_sha256"]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise ValueError("Native planner backend checksum invalid")
        if (
            planner["peak_child_rss_bytes"] > 512 * 1024**2
            or planner["peak_process_rss_bytes"] > 768 * 1024**2
            or planner["metadata_elapsed_seconds"] > 61
        ):
            raise ValueError("Native planner resource receipt exceeds caps")
    height, width = expected["shape"]
    items = payload["chunks"]
    if not isinstance(items, list) or not items or len(items) > 10000:
        raise ValueError("Global warp chunk plan requires a bounded nonempty chunk list")
    chunks = []
    for item in items:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or any(type(value) is not int for value in item)
        ):
            raise ValueError("Global warp chunks must contain four integer pixel coordinates")
        x, y, w, h = item
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError("Global warp chunk outside destination grid")
        chunks.append((x, y, w, h))
    union = unary_union([box(x, y, x + w, y + h) for x, y, w, h in chunks])
    total = sum(w * h for _, _, w, h in chunks)
    if total != width * height or union.area != total:
        raise ValueError(
            "Global warp chunks must cover the grid exactly once without gaps or overlap"
        )
    return GlobalWarpChunkPlan(
        transform=Affine(*expected["transform"][:6]),
        width=width,
        height=height,
        chunks=tuple(sorted(chunks, key=lambda chunk: (chunk[1], chunk[0]))),
    )
