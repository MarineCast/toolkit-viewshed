"""Canonical direct-transmission tiles and a bounded, lossless shared tile cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import box

from ...contracts.transmission import DTYPES
from ...providers.global_building_atlas import iter_gba_subset
from .buildings import rasterize_building_footprints
from .canopy import qualify_water_footprints
from .transmission import align, load_classes, validate_native_band


def prepare_tile(
    config: Any,
    canopy_assets: Any,
    class_assets: Any,
    land: Any,
    water: Any,
    grid: Any,
    shape: Any,
    *,
    require_finite_terrain: bool = True,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """The dense and tiled routes share exactly one numerical preparation owner."""
    started = time.perf_counter() if timings is not None else 0.0
    height, width = shape
    if height % 3 or width % 3 or grid.a != 10 or grid.e != -10 or grid.b or grid.d:
        raise ValueError("A nested north-up30m/10m grid is required")
    bounds = rasterio.transform.array_bounds(height, width, grid)
    local_extent = box(*bounds).buffer(20)
    local_water, local_land = water.intersection(local_extent), land.intersection(local_extent)
    qualified = (
        qualify_water_footprints(
            local_water,
            local_land,
            transform=grid,
            shape=shape,
            geometry_crs="EPSG:32610",
            raster_crs=32610,
            supported_mask=np.ones(shape, bool),
        )
        if not local_water.is_empty
        else np.zeros(shape, bool)
    )
    centres = (
        rasterize([(local_water, 1)], out_shape=shape, transform=grid, dtype="uint8").astype(bool)
        if not local_water.is_empty
        else np.zeros(shape, bool)
    )
    if timings is not None:
        now = time.perf_counter()
        timings["water"] = timings.get("water", 0.0) + now - started
        started = now
    with rasterio.open(config["inputs"]["terrain_vrt"]["path"]) as src:
        validate_native_band(src)
        with WarpedVRT(
            src,
            crs=32610,
            transform=grid * Affine.scale(3),
            height=height // 3,
            width=width // 3,
            nodata=np.nan,
            dtype="float32",
            resampling=Resampling.bilinear,
            warp_mem_limit=64,
            tolerance=1e-8,
        ) as vrt:
            ground = vrt.read(1).repeat(3, 0).repeat(3, 1)
    observer_ground = ground.copy()
    ground[centres] = 0
    if require_finite_terrain and not np.isfinite(ground).all():
        raise ValueError("Unresolved nonwater terrain gaps")
    if timings is not None:
        now = time.perf_counter()
        timings["terrain"] = timings.get("terrain", 0.0) + now - started
        started = now
    native = np.full(shape, 255, dtype="uint8")
    for entry in canopy_assets:
        # Compact storage must never truncate an alternate fractional-height source.
        with rasterio.open(entry["path"]) as src:
            validate_native_band(src)
            if src.dtypes[0] != "uint8" or src.nodata != 255:
                raise ValueError("Compact canopy requires native uint8 whole metres with NoData255")
        layer = align(entry["path"], shape, grid, nodata=255, dtype="uint8")
        native[layer != 255] = layer[layer != 255]
    canopy_unknown = (native == 255) & ~qualified
    # Reuse the native byte buffer after freezing its independent missing mask.
    canopy = native
    canopy[native == 255] = 0
    canopy[qualified] = 0
    if timings is not None:
        now = time.perf_counter()
        timings["canopy"] = timings.get("canopy", 0.0) + now - started
        started = now
    buildings = rasterize_building_footprints(
        (
            (feature.geometry_wkb, feature.height_m)
            for feature in iter_gba_subset(
                Path(config["inputs"]["gba_subset"]["path"]),
                transform_bounds(32610, 3857, *bounds),
            )
        ),
        shape=shape,
        transform=grid,
        crs=32610,
        all_touched=False,
        inventory_policy=config["building_inventory_policy"],
    )
    if timings is not None:
        now = time.perf_counter()
        timings["buildings"] = timings.get("buildings", 0.0) + now - started
        started = now
    classes = load_classes(class_assets, shape, grid)
    if timings is not None:
        timings["classes"] = timings.get("classes", 0.0) + time.perf_counter() - started
    return dict(
        ground=ground,
        observer_ground=observer_ground,
        canopy=canopy,
        canopy_unknown=canopy_unknown,
        building=buildings.lower_height,
        building_unknown=buildings.unknown,
        building_present=buildings.present,
        qualified_water=qualified,
        classes=classes,
    )


class CanonicalTileBuilder:
    """Slice cache tiles from the dense owner's fixed warp windows.

    GDAL bilinear downsampling can depend on warp-window dimensions. Canonical
    parents retain the dense owner's1536-pixel partition and partial edge sizes.
    Missing parent terrain stays NaN, never a numeric barrier or known zero.
    """

    def __init__(
        self,
        config: Any,
        canopy_assets: Any,
        class_assets: Any,
        land: Any,
        water: Any,
        global_grid: Any,
        global_shape: Any,
        *,
        parent_size: int = 1536,
        parent_capacity: int = 5,
        profile: bool = False,
    ) -> None:
        if (
            parent_size <= 0
            or parent_size > 1536
            or parent_size % 3
            or not 1 <= parent_capacity <= 5
            or any(n <= 0 or n % 3 for n in global_shape)
        ):
            raise ValueError("Canonical parent grid must align with30m terrain")
        self.config, self.canopy_assets, self.class_assets = config, canopy_assets, class_assets
        self.land, self.water = land, water
        self.global_grid, self.global_shape, self.parent_size = (
            global_grid,
            global_shape,
            parent_size,
        )
        self.parent_capacity = parent_capacity
        self.key: tuple[int, int] | None = None
        self.arrays: Any = None
        self.parent_builds = 0
        self.timings: dict[str, float] | None = {} if profile else None
        self.parents: OrderedDict[tuple[int, int], Any] = OrderedDict()
        self.peak_parent_bytes = 0

    def __call__(self, row: int, col: int, size: int) -> dict[str, Any]:
        if self.parent_size % size or row % size or col % size or row < 0 or col < 0:
            raise ValueError("Cache tile must divide and align with canonical parent")
        key = (
            row // self.parent_size * self.parent_size,
            col // self.parent_size * self.parent_size,
        )
        if key[0] >= self.global_shape[0] or key[1] >= self.global_shape[1]:
            raise ValueError("Cache tile outside canonical domain")
        if key in self.parents:
            self.arrays = self.parents.pop(key)
            self.parents[key] = self.arrays
            self.key = key
        elif key != self.key:
            while len(self.parents) >= self.parent_capacity:
                self.parents.popitem(last=False)
            shape = (
                min(self.parent_size, self.global_shape[0] - key[0]),
                min(self.parent_size, self.global_shape[1] - key[1]),
            )
            self.arrays = prepare_tile(
                self.config,
                self.canopy_assets,
                self.class_assets,
                self.land,
                self.water,
                self.global_grid * Affine.translation(key[1], key[0]),
                shape,
                require_finite_terrain=False,
                timings=self.timings,
            )
            self.key = key
            self.parent_builds += 1
            self.parents[key] = self.arrays
            self.peak_parent_bytes = max(
                self.peak_parent_bytes,
                sum(array.nbytes for entry in self.parents.values() for array in entry.values()),
            )
        rr, cc = row - key[0], col - key[1]
        height = min(size, self.arrays["ground"].shape[0] - rr)
        width = min(size, self.arrays["ground"].shape[1] - cc)
        result = {}
        for name, dtype in DTYPES.items():
            fill = np.nan if name in {"ground", "observer_ground"} else "unknown" in name
            result[name] = np.full((size, size), fill, dtype=dtype)
            result[name][:height, :width] = self.arrays[name][rr : rr + height, cc : cc + width]
        return result


class TileCache:
    """A task-owned LRU; scientific input/output paths are never cleanup roots.

    The caller pins input byte hashes, recipe, geometries, grid and runtime in identity.
    This cache is disposable; it is not a completed scientific-product receipt.
    """

    def __init__(
        self,
        root: Any,
        identity: Any,
        global_grid: Any,
        *,
        max_bytes: int = 384 * 1024**2,
        tile_size: int = 384,
        compressed: bool = False,
        profile: bool = False,
    ) -> None:
        if max_bytes <= 0 or tile_size <= 0 or tile_size % 3:
            raise ValueError("Positive cache bytes and30m-aligned tile size required")
        if global_grid.a != 10 or global_grid.e != -10 or global_grid.b or global_grid.d:
            raise ValueError("North-up10m global lattice required")
        self.global_grid = global_grid
        self.root = Path(root)
        if self.root.exists():
            raise ValueError("Cache root must be new and owned by this task")
        self.root.mkdir(parents=True)
        self.identity = hashlib.sha256(
            json.dumps(identity, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        self.max_bytes, self.tile_size = max_bytes, tile_size
        self.compressed = compressed
        self.timings: dict[str, float] | None = {} if profile else None
        self.entries: OrderedDict[tuple[int, int], tuple[Path, int, str]] = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.evictions = self.peak_bytes = 0

    def _validate(self, arrays: Any) -> None:
        if set(arrays) != set(DTYPES):
            raise ValueError("Canonical tile fields mismatch")
        for name, dtype in DTYPES.items():
            if arrays[name].shape != (self.tile_size, self.tile_size) or arrays[
                name
            ].dtype != np.dtype(dtype):
                raise ValueError("Canonical tile shape/dtype mismatch")
        if (arrays["canopy"] == 255).any():
            raise ValueError(
                "Reserved canopy255 requires zero lower bound and separate missing mask"
            )

    def tile(self, row: int, col: int, builder: Any) -> Any:
        started = time.perf_counter() if self.timings is not None else 0.0
        if row < 0 or col < 0 or row % self.tile_size or col % self.tile_size:
            raise ValueError("Cache tile must align with canonical lattice")
        key = (row, col)
        if key in self.entries:
            path, size, sha = self.entries.pop(key)
            self.entries[key] = (path, size, sha)
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError("Derived cache bytes changed")
            self.hits += 1
            with np.load(path, allow_pickle=False) as loaded:
                arrays = {name: loaded[name] for name in loaded.files}
            self._validate(arrays)
            if self.timings is not None:
                self.timings["read"] = self.timings.get("read", 0.0) + time.perf_counter() - started
            return arrays
        self.misses += 1
        arrays = builder(row, col, self.tile_size)
        self._validate(arrays)
        if self.timings is not None:
            now = time.perf_counter()
            self.timings["build"] = self.timings.get("build", 0.0) + now - started
            started = now
        # Reserve worst-case uncompressed NPY bytes before creating a temporary file.
        reserve = sum(array.nbytes + 512 for array in arrays.values()) + 4096
        if reserve > self.max_bytes:
            raise ValueError("One tile exceeds cache byte cap")
        while self.bytes + reserve > self.max_bytes:
            _, (old, size, _) = self.entries.popitem(last=False)
            old.unlink()
            self.bytes -= size
            self.evictions += 1
        path = self.root / f"{self.identity}-{row}-{col}.npz"
        temporary = path.with_suffix(".part")
        try:
            with temporary.open("xb") as handle:
                writer = np.savez_compressed if self.compressed else np.savez
                writer(handle, **arrays)
                handle.flush()
                os.fsync(handle.fileno())
            size = temporary.stat().st_size
            if size > reserve or self.bytes + size > self.max_bytes:
                raise ValueError("Cache storage reservation exceeded")
            sha = hashlib.sha256(temporary.read_bytes()).hexdigest()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if self.timings is not None:
            self.timings["write"] = self.timings.get("write", 0.0) + time.perf_counter() - started
        self.entries[key] = (path, size, sha)
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        return arrays

    def window(
        self, grid: Any, shape: Any, builder: Any, *, telemetry: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Assemble one source window; optionally report this call's incremental costs.

        Cache and builder are single-producer objects. Returned arrays may be shared
        read-only by scoring threads, but window/tile calls must remain serialized.
        """
        before = (self.hits, self.misses, self.evictions, getattr(builder, "parent_builds", 0))
        cache_before = dict(self.timings or {})
        prepare_before = dict(getattr(builder, "timings", None) or {})
        height, width = shape
        if height % 3 or width % 3 or grid.a != 10 or grid.e != -10 or grid.b or grid.d:
            raise ValueError("Aligned source window required")
        row0 = round((self.global_grid.f - grid.f) / 10)
        col0 = round((grid.c - self.global_grid.c) / 10)
        if hasattr(builder, "global_shape") and (
            row0 < 0
            or col0 < 0
            or row0 + height > builder.global_shape[0]
            or col0 + width > builder.global_shape[1]
        ):
            raise ValueError("Source window outside canonical domain")
        if (
            row0 % 3
            or col0 % 3
            or not np.isclose((self.global_grid.f - grid.f) / 10, row0, rtol=0, atol=1e-8)
            or not np.isclose((grid.c - self.global_grid.c) / 10, col0, rtol=0, atol=1e-8)
        ):
            raise ValueError("Source window shifted off parent lattice")
        arrays = {name: np.empty(shape, dtype=dtype) for name, dtype in DTYPES.items()}
        size = self.tile_size
        for row in range(math.floor(row0 / size) * size, row0 + height, size):
            for col in range(math.floor(col0 / size) * size, col0 + width, size):
                tile = self.tile(row, col, builder)
                r0, c0 = max(row0, row), max(col0, col)
                r1, c1 = min(row0 + height, row + size), min(col0 + width, col + size)
                for name in DTYPES:
                    arrays[name][r0 - row0 : r1 - row0, c0 - col0 : c1 - col0] = tile[name][
                        r0 - row : r1 - row, c0 - col : c1 - col
                    ]
        if telemetry is not None:
            after = (self.hits, self.misses, self.evictions, getattr(builder, "parent_builds", 0))
            telemetry.update(
                dict(
                    zip(
                        ("hits", "misses", "evictions", "parent_builds"),
                        (new - old for old, new in zip(before, after, strict=True)),
                        strict=True,
                    )
                )
            )
            telemetry["cache_seconds"] = {
                key: value - cache_before.get(key, 0.0)
                for key, value in (self.timings or {}).items()
            }
            telemetry["prepare_seconds"] = {
                key: value - prepare_before.get(key, 0.0)
                for key, value in (getattr(builder, "timings", None) or {}).items()
            }
            telemetry["cache_bytes"] = self.bytes
            telemetry["parent_bytes"] = sum(
                array.nbytes
                for entry in getattr(builder, "parents", {}).values()
                for array in entry.values()
            )
        arrays["transform"] = grid
        return arrays
