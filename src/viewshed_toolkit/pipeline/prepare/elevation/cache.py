"""Shared cache identities for prepared elevation rasters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import rasterio

from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file


def input_signature(path: Path) -> dict[str, str | int]:
    """Return the immutable-on-read identity used by raster caches."""

    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "sha256": checksum_unchanged_file(resolved, raw_bytes=True),
        "path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def cache_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def raster_cache_matches(path: Path, expected_fingerprint: str) -> bool:
    try:
        with rasterio.open(path) as source:
            return source.tags().get("cache_fingerprint") == expected_fingerprint
    except (OSError, rasterio.errors.RasterioError):
        return False
