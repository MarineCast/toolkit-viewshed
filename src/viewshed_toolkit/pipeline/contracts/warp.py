"""Producer receipts for replaying the established global GDAL warp chunks."""

from __future__ import annotations

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
    required = {*expected, "chunks", "producer_reference_sha256"}
    if set(payload) != required or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError(
            "Global warp chunk plan does not match source/grid/method/runtime contract"
        )
    checksum = payload["producer_reference_sha256"]
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(c not in "0123456789abcdef" for c in checksum)
    ):
        raise ValueError("Global warp chunk plan requires producer reference checksum")
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
