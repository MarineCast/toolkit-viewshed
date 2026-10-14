"""Lossless lattice/cache behavior and native tile-preparation parity."""

import sqlite3

import numpy as np
import pytest
import rasterio
from affine import Affine
from shapely.geometry import box

from viewshed_toolkit.pipeline.contracts.transmission import DTYPES
from viewshed_toolkit.pipeline.prepare.elevation.transmission_tiles import (
    CanonicalTileBuilder,
    TileCache,
    prepare_tile,
)


def test_cache_window_reuses_lattice_tiles_and_rejects_shift_and_tampering(tmp_path):
    grid = Affine(10, 0, 0, 0, -10, 600)
    calls = []

    def build(row, col, size):
        calls.append((row, col))
        rr, cc = np.mgrid[row : row + size, col : col + size]
        return {
            name: ((rr + cc) % 2 if dtype == "bool" else rr * 7 + cc).astype(dtype)
            for name, dtype in DTYPES.items()
        }

    cache = TileCache(tmp_path / "cache", {"fixture": 1}, grid, tile_size=6)
    shifted = grid * Affine.translation(3, 3)
    first = cache.window(shifted, (9, 9), build)
    second = cache.window(shifted, (9, 9), build)
    assert len(calls) == 4 and cache.hits == 4
    rr, cc = np.mgrid[3:12, 3:12]
    assert np.array_equal(first["ground"], rr * 7 + cc)
    assert all(np.array_equal(first[name], second[name]) for name in DTYPES)
    with pytest.raises(ValueError, match="lattice"):
        cache.window(grid * Affine.translation(0.1, 0), (6, 6), build)
    path = next((tmp_path / "cache").glob("*.npz"))
    path.write_bytes(b"changed")
    row, col = next(key for key, value in cache.entries.items() if value[0] == path)
    with pytest.raises(ValueError, match="changed"):
        cache.tile(row, col, build)


def test_lru_storage_cap_has_no_scientific_output_cleanup(tmp_path):
    grid = Affine(10, 0, 0, 0, -10, 60)
    preserved = tmp_path / "native.tif"
    preserved.write_bytes(b"native input")
    cache = TileCache(tmp_path / "cache", {"fixture": 1}, grid, max_bytes=13_000, tile_size=6)

    def build(row, col, size):
        return {name: np.zeros((size, size), dtype=dtype) for name, dtype in DTYPES.items()}

    for i in range(10):
        cache.tile(i * 6, 0, build)
    assert cache.evictions > 0 and cache.bytes <= cache.max_bytes
    assert cache.peak_bytes <= cache.max_bytes
    assert preserved.read_bytes() == b"native input"


@pytest.mark.parametrize("policy", ["unverified", "assume_complete"])
def test_native_subtiles_match_dense_preparation_across_shore_and_unknown_canopy(tmp_path, policy):
    grid = Affine(10, 0, 499800, 0, -10, 5370300)
    size = (60, 60)
    rr, cc = np.indices(size)
    terrain = (2 + rr * 0.25 + cc * 0.125).astype("float32")
    canopy = np.where(cc < 31, 15, 0).astype("uint8")
    canopy[10:20, 28:35] = 255
    classes = np.where(cc < 31, 10, 80).astype("uint8")
    paths = {}
    for name, array, nodata in [
        ("terrain", terrain, -9999),
        ("canopy", canopy, 255),
        ("classes", classes, 0),
    ]:
        path = tmp_path / f"{name}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=60,
            width=60,
            count=1,
            dtype=array.dtype,
            transform=grid,
            crs=32610,
            nodata=nodata,
        ) as dst:
            dst.write(array, 1)
        paths[name] = {"path": str(path)}
    subset = tmp_path / "buildings.sqlite"
    with sqlite3.connect(subset) as connection:
        connection.execute(
            "CREATE TABLE buildings (tile,key,source,id,region,height,variance,height_status,geometry_wkb,footprint_license,minx,maxx,miny,maxy)"
        )
    config = {
        "inputs": {"terrain_vrt": paths["terrain"], "gba_subset": {"path": str(subset)}},
        "building_inventory_policy": policy,
    }
    land = box(499700, 5369600, 500104, 5370400)
    water = box(500104, 5369600, 500500, 5370400)
    timings = {}
    dense = prepare_tile(
        config, [paths["canopy"]], [paths["classes"]], land, water, grid, size, timings=timings
    )
    assert set(timings) == {"water", "terrain", "canopy", "buildings", "classes"}
    assert all(value >= 0 for value in timings.values())
    builder = CanonicalTileBuilder(
        config,
        [paths["canopy"]],
        [paths["classes"]],
        land,
        water,
        grid,
        size,
        parent_size=60,
        profile=True,
    )
    merged = {name: np.empty(size, dtype=dtype) for name, dtype in DTYPES.items()}
    for row in (0, 30):
        for col in (0, 30):
            tile = builder(row, col, 30)
            for name in DTYPES:
                merged[name][row : row + 30, col : col + 30] = tile[name]
    assert builder.parent_builds == 1
    assert set(builder.timings) == set(timings)
    for name in DTYPES:
        assert np.array_equal(dense[name], merged[name]), name
    assert (dense["observer_ground"] != dense["ground"]).any()
    assert dense["canopy_unknown"].any()
    assert (
        dense["building_unknown"].all()
        if policy == "unverified"
        else not dense["building_unknown"].any()
    )
    # Valid native zero stays supported; land NoData retains its separate unknown mask.
    assert dense["canopy_unknown"][12, 29]
    assert dense["canopy"][12, 29] == 0
    assert dense["canopy"][30, 40] == 0 and not dense["canopy_unknown"][30, 40]


@pytest.mark.parametrize("compressed", [False, True])
def test_storage_modes_preserve_exact_fields_and_report_incremental_costs(tmp_path, compressed):
    grid = Affine(10, 0, 0, 0, -10, 600)

    def build(row, col, size):
        rr, cc = np.indices((size, size))
        fields = {
            name: ((rr + cc) % 2 if dtype == "bool" else rr / 7 + cc / 11).astype(dtype)
            for name, dtype in DTYPES.items()
        }
        fields["ground"][0, 0] = np.nan
        fields["observer_ground"][0, 1] = -0.0
        return fields

    cache = TileCache(
        tmp_path / "cache",
        {"fixture": 1},
        grid,
        tile_size=6,
        compressed=compressed,
        profile=True,
    )
    cold, warm = {}, {}
    first = cache.window(grid, (6, 6), build, telemetry=cold)
    second = cache.window(grid, (6, 6), build, telemetry=warm)
    for name in DTYPES:
        assert first[name].tobytes() == second[name].tobytes()
    assert cold["misses"] == 1 and cold["hits"] == 0
    assert warm["hits"] == 1 and warm["misses"] == 0
    assert cold["cache_seconds"]["write"] > 0
    assert warm["cache_seconds"]["read"] > 0
    assert warm["cache_seconds"]["write"] == 0
    assert cache.bytes <= cache.max_bytes
    with pytest.raises(ValueError, match="lattice"):
        cache.tile(1, 0, build)


def test_compressed_cache_incompressible_cap_and_failed_write_cleanup(tmp_path, monkeypatch):
    grid = Affine(10, 0, 0, 0, -10, 600)
    rng = np.random.default_rng(1)

    def build(row, col, size):
        return {
            name: rng.integers(0, 255, (size, size)).astype(dtype) for name, dtype in DTYPES.items()
        }

    cache = TileCache(
        tmp_path / "cache",
        {"fixture": 1},
        grid,
        tile_size=6,
        compressed=True,
        max_bytes=13_000,
    )
    for row in range(0, 60, 6):
        cache.tile(row, 0, build)
    assert cache.evictions > 0 and cache.peak_bytes <= 13_000

    def broken_writer(handle, **arrays):
        handle.write(b"partial")
        raise OSError("synthetic write failure")

    monkeypatch.setattr(np, "savez_compressed", broken_writer)
    with pytest.raises(OSError, match="synthetic"):
        cache.tile(60, 0, build)
    assert not list(cache.root.glob("*.part"))
    assert (60, 0) not in cache.entries


def test_parent_capacity_three_preserves_lru_fields_and_byte_accounting(monkeypatch):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_tiles as module

    def synthetic_prepare(config, canopy, classes, land, water, grid, shape, **kwargs):
        if kwargs["timings"] is not None:
            kwargs["timings"]["terrain"] = kwargs["timings"].get("terrain", 0) + 0.125
        return {name: np.full(shape, grid.c / 60, dtype=dtype) for name, dtype in DTYPES.items()}

    monkeypatch.setattr(module, "prepare_tile", synthetic_prepare)
    grid = Affine(10, 0, 0, 0, -10, 60)
    builder = CanonicalTileBuilder(
        {},
        [],
        [],
        None,
        None,
        grid,
        (6, 30),
        parent_size=6,
        parent_capacity=3,
        profile=True,
    )
    for col in (0, 6, 12, 18):
        tile = builder(0, col, 3)
        assert np.all(tile["ground"] == col / 6)
        assert len(builder.parents) <= 3
    assert set(builder.parents) == {(0, 6), (0, 12), (0, 18)}
    expected = 3 * 6 * 6 * sum(np.dtype(dtype).itemsize for dtype in DTYPES.values())
    assert builder.peak_parent_bytes == expected
    assert (
        sum(array.nbytes for entry in builder.parents.values() for array in entry.values())
        == expected
    )
    assert builder.timings["terrain"] == 0.5
    builder(0, 6, 3)
    assert builder.parent_builds == 4
    builder(0, 0, 3)
    assert builder.parent_builds == 5
    for capacity in (0, 6):
        with pytest.raises(ValueError, match="Canonical"):
            CanonicalTileBuilder({}, [], [], None, None, grid, (6, 6), parent_capacity=capacity)


def test_fractional_native_raster_is_rejected_before_uint8_alignment(tmp_path):
    from viewshed_toolkit.pipeline.prepare.elevation import transmission_tiles as module

    # Reuse the native fixture to reach preparation's CHM encoding guard.
    grid = Affine(10, 0, 499800, 0, -10, 5370300)
    paths = {}
    for name, data, nodata in [
        ("terrain", np.zeros((6, 6), dtype="float32"), -9999),
        ("canopy", np.full((6, 6), 1.25, dtype="float32"), 255),
        ("classes", np.full((6, 6), 30, dtype="uint8"), 0),
    ]:
        p = tmp_path / (name + ".tif")
        with rasterio.open(
            p,
            "w",
            driver="GTiff",
            height=6,
            width=6,
            count=1,
            dtype=data.dtype,
            crs=32610,
            transform=grid,
            nodata=nodata,
        ) as dst:
            dst.write(data, 1)
        paths[name] = {"path": str(p)}
    with pytest.raises(ValueError, match="native uint8 whole metres"):
        module.prepare_tile(
            {
                "inputs": {"terrain_vrt": paths["terrain"], "gba_subset": {"path": "absent"}},
                "building_inventory_policy": "unverified",
            },
            [paths["canopy"]],
            [paths["classes"]],
            box(499700, 5369600, 500500, 5370400),
            box(0, 0, 1, 1),
            grid,
            (6, 6),
        )
