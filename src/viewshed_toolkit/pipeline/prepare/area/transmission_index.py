"""Lossless persisted30m water lattice and integer H3 dictionary, separate from10m layers."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

import h3
import numpy as np
from affine import Affine

from .transmission import WaterTargetProvider


def encode_water_index(water: Any, grid: Affine) -> bytes:
    provider = WaterTargetProvider(water, grid)
    handle = io.BytesIO()
    np.savez_compressed(
        handle,
        points=provider.points,
        cells=provider.cells,
        identity=np.frombuffer(
            json.dumps(provider.identity, sort_keys=True).encode(), dtype=np.uint8
        ),
    )
    return handle.getvalue()


def decode_water_index(payload: bytes, water: Any, grid: Affine) -> WaterTargetProvider:
    with np.load(io.BytesIO(payload), allow_pickle=False) as stored:
        if set(stored.files) != {"points", "cells", "identity"}:
            raise ValueError("Unexpected water index fields")
        points, cells, identity_bytes = stored["points"], stored["cells"], stored["identity"]
    if identity_bytes.ndim != 1 or identity_bytes.dtype != np.uint8:
        raise ValueError("Invalid water index identity encoding")
    identity = json.loads(identity_bytes.tobytes())
    expected = dict(
        geometry_sha256=hashlib.sha256(water.wkb).hexdigest(),
        geometry_crs="EPSG:32610",
        grid=list(grid)[:6],
        h3_resolution=7,
        population="strict_geometry_contains_30m_centers_v1",
    )
    if identity != expected or grid.a != 10 or grid.e != -10 or grid.b or grid.d:
        raise ValueError("Water index identity/grid mismatch")
    if (
        points.dtype != np.int32
        or points.ndim != 2
        or points.shape[1] != 3
        or cells.ndim != 1
        or cells.dtype.kind != "U"
    ):
        raise ValueError("Lossless integer water points and string H3 dictionary required")
    if len(cells) != len(set(cells.tolist())) or any(
        not h3.is_valid_cell(c) or h3.get_resolution(c) != 7 for c in cells
    ):
        raise ValueError("Invalid H3 dictionary")
    if len(points) and ((points[:, 2] < 0).any() or (points[:, 2] >= len(cells)).any()):
        raise ValueError("Water dictionary codes outside declared range")
    if len(points) > 1:
        dr = np.diff(points[:, 0].astype(np.int64))
        dc = np.diff(points[:, 1].astype(np.int64))
        if ((dr < 0) | ((dr == 0) & (dc <= 0))).any():
            raise ValueError("Water lattice must retain unique row/column order")
    provider = object.__new__(WaterTargetProvider)
    provider.grid = grid
    provider.coarse = grid * Affine.scale(3)
    provider.identity = identity
    provider.points = points
    provider.cells = cells
    points.flags.writeable = False
    cells.flags.writeable = False
    return provider
