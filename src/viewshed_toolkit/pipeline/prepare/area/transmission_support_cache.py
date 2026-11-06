"""Bounded exact support masks with retained geometry identity and uncached fallback."""

import math
from collections import OrderedDict
from typing import Any

import numpy as np
from affine import Affine
from shapely import contains_xy


class SupportMaskCache:
    def __init__(self, water: Any, grid: Any, radius: Any, max_bytes: Any = 64 * 1024**2) -> None:
        self.water = water
        self.grid = grid
        self.radius = radius
        self.base = water.buffer((radius + 60) / math.cos(math.pi / 256), quad_segs=64)
        self.entries: OrderedDict[Any, Any] = OrderedDict()
        self.bytes = 0
        self.cap = max_bytes
        self.hits = self.misses = 0

    def mask(self, grid: Any, dims: Any, active: Any, water: Any, radius: Any) -> Any:
        if water is not self.water or radius != self.radius:
            from .transmission import required_support_mask

            return required_support_mask(grid, dims, active, water, radius)
        if not self.base.covers(active.buffer(60)):
            from .transmission import required_support_mask

            return required_support_mask(grid, dims, active, water, radius)
        row = round((self.grid.f - grid.f) / 10)
        col = round((grid.c - self.grid.c) / 10)
        out = np.empty(dims, bool)
        for r in range(row // 384 * 384, row + dims[0], 384):
            for c in range(col // 384 * 384, col + dims[1], 384):
                key = (r, c)
                if key in self.entries:
                    a = self.entries.pop(key)
                    self.entries[key] = a
                    self.hits += 1
                else:
                    g = self.grid * Affine.translation(c, r)
                    rr = np.arange(384)[:, None]
                    cc = np.arange(384)[None, :]
                    x, y = g * (cc + 0.5, rr + 0.5)
                    a = contains_xy(self.base, x, y)
                    a.flags.writeable = False
                    self.entries[key] = a
                    self.bytes += a.nbytes
                    self.misses += 1
                    while self.bytes > self.cap:
                        _, old = self.entries.popitem(last=False)
                        self.bytes -= old.nbytes
                r0, c0 = max(row, r), max(col, c)
                r1, c1 = min(row + dims[0], r + 384), min(col + dims[1], c + 384)
                out[r0 - row : r1 - row, c0 - col : c1 - col] = a[r0 - r : r1 - r, c0 - c : c1 - c]
        return out
