"""Versioned canonical prepared-tile persistence contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any

STORE_CONTRACT = "viewshed_direct_transmission_tile_store_v2"
TILE_SIZE = 384
STORE_CRS = "EPSG:32610"


def canonical_bytes(value: Any) -> bytes:
    """Deterministic, finite JSON metadata; scientific identity is supplied by API."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def metadata_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def tile_name(row: int, col: int) -> str:
    if type(row) is not int or type(col) is not int or min(row, col) < 0:
        raise ValueError("Integer nonnegative tile coordinates required")
    if row % TILE_SIZE or col % TILE_SIZE:
        raise ValueError("Tile coordinates must align with canonical lattice")
    return f"tile-{row}-{col}"


def store_runtime_identity() -> dict[str, Any]:
    from importlib.metadata import version

    from .transmission import runtime_identity

    identity = runtime_identity()
    identity["libraries"]["xarray"] = version("xarray")
    return identity
