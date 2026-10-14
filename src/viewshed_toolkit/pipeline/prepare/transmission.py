"""Provisioned-input preparation and outdoor observer designs for direct transmission."""

import json
import math
import resource
import sys
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import h3
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.features import shapes
from rasterio.warp import transform_bounds
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform as project
from shapely.ops import unary_union

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json

from ..contracts.transmission import (
    DTYPES,
    OBSERVER_GROUND_CONTRACT,
    PREPARED_CONTRACT,
    RADIUS,
    digest,
)
from .area.land import bbox_candidate_h3_cells
from .area.sampling import sample_points_in_source_geometry
from .elevation.transmission import extended_grid
from .elevation.transmission_tiles import prepare_tile

MODEL_RECIPE = dict(
    crs="EPSG:32610",
    obstacle_pixel_m=10,
    terrain_pixel_m=30,
    target_water_lattice_m=30,
    observer_height_m=1.7,
    target_height_m=1.0,
    curvature_coefficient=0.85714,
    earth_radius_m=6378137.0,
    beta_per_m=0.002,
    canopy_clearance_m=30,
    building_rasterization="cell_center_max_agl",
    water_surface_m=0.0,
    target_support="partial_h3_in_union_of_observer_reach",
)


def land_sources(bbox: Any, land: Any, water: Any) -> Any:
    """Enumerate intersecting R7 sources and retain their clipped land support."""
    to_m = Transformer.from_crs(4326, 32610, always_xy=True).transform
    domain = project(to_m, box(*bbox))
    sources = []
    for cell in sorted(bbox_candidate_h3_cells(bbox, 7)):
        polygon = project(to_m, Polygon([(x, y) for y, x in h3.cell_to_boundary(cell)]))
        active = polygon.intersection(domain).intersection(land)
        land_fraction = active.area / polygon.area
        water_fraction = polygon.intersection(domain).intersection(water).area / polygon.area
        if land_fraction >= 0.01 and water_fraction <= 0.95:
            sources.append(
                dict(
                    source_h3=cell,
                    land_fraction=land_fraction,
                    water_fraction=water_fraction,
                    source_land=mapping(active),
                )
            )
    if not sources:
        raise ValueError("No eligible land source cells")
    return sources, domain


def load_cache(out: Any, metadata: Any, *, mode: Any = "r") -> Any:
    if (
        metadata.get("contract") != PREPARED_CONTRACT
        or metadata.get("observer_ground_contract") != OBSERVER_GROUND_CONTRACT
    ):
        raise ValueError("Rebuild legacy prepared grids with unflattened observer ground")
    arrays = {
        key: np.lib.format.open_memmap(out / "prepared" / f"{key}.npy", mode=mode) for key in DTYPES
    }
    if any(
        array.shape != tuple(metadata["shape"]) or array.dtype != np.dtype(DTYPES[key])
        for key, array in arrays.items()
    ):
        raise ValueError("Prepared band grid/dtype mismatch")
    grid = Affine(*metadata["transform"])
    if grid.a != 10 or grid.e != -10 or grid.b != 0 or grid.d != 0:
        raise ValueError("Prepared bands require a north-up 10m grid")
    return arrays


def prepare(config: Any) -> Any:
    """Prepare once, writing disk-backed bands in 15.36km tiles."""
    out = Path(config["output_dir"])
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use a new empty output directory")
    out.mkdir(parents=True)
    started = time.perf_counter()
    sources = config["inputs"]
    for entry in sources.values():
        if digest(entry["path"]) != entry["sha256"]:
            raise ValueError("Input checksum mismatch")
    inventory = json.loads(Path(sources["canopy_inventory"]["path"]).read_text())
    canopy_assets = inventory["raw_canopy"]
    for entry in canopy_assets:
        if digest(entry["path"]) != entry["sha256"]:
            raise ValueError("Canopy checksum mismatch")
    wc = json.loads(Path(sources["worldcover_manifest"]["path"]).read_text())
    for asset in wc["assets"]:
        if digest(asset["path"]) != asset["sha256"]:
            raise ValueError("WorldCover checksum mismatch")
    acquisition = json.loads(Path(sources["gba_manifest"]["path"]).read_text())
    if acquisition["status"] != "completed_validated" or (
        acquisition["subset_sha256"] != sources["gba_subset"]["sha256"]
    ):
        raise ValueError("Validated building acquisition required")
    source_domain = project(
        Transformer.from_crs(4326, 32610, always_xy=True).transform,
        box(*config["source_bbox_wgs84"]),
    )
    lo = np.array(source_domain.bounds[:2]) - config["max_distance_m"] - 60
    hi = np.array(source_domain.bounds[2:]) + config["max_distance_m"] + 60
    left, bottom = np.floor(lo / 30) * 30
    right, top = np.ceil(hi / 30) * 30
    grid = Affine(10, 0, left, 0, -10, top)
    grid_shape = (int((top - bottom) / 10), int((right - left) / 10))
    bounds = [left, bottom, right, top]
    bounds_ll = transform_bounds(32610, 4326, *bounds, densify_pts=41)
    pixels = math.prod(grid_shape)
    prepared_bytes = (
        pixels * sum(np.dtype(dtype).itemsize for dtype in DTYPES.values()) + len(DTYPES) * 256
    )
    if pixels > config["max_raster_pixels"] or prepared_bytes > config["max_prepared_bytes"]:
        raise ValueError("Prepared raster exceeds configured pixel/disk bounds")
    extent = box(*bounds)
    if not box(*acquisition["study_bbox"]).covers(box(*bounds_ll)):
        raise ValueError("Raster exceeds validated building inventory extent")
    if not box(*wc["bounds_wgs84"]).covers(box(*bounds_ll)):
        raise ValueError("Raster exceeds downloaded WorldCover extent")
    to_m = Transformer.from_crs(4326, 32610, always_xy=True).transform
    geometry = {}
    for layer in ("land_area", "water_area"):
        frame = gpd.read_file(
            sources["coastline"]["path"], layer=layer, bbox=bounds_ll, engine="pyogrio"
        )
        if CRS.from_user_input(frame.crs) != CRS.from_epsg(4326):
            raise ValueError("Provisioned coastline must be EPSG:4326")
        geometry[layer] = unary_union(
            [project(to_m, item) for item in frame.geometry]
        ).intersection(extent)
    land, water = geometry["land_area"], geometry["water_area"]
    gap = extent.difference(land.union(water)).area
    if gap > 1:
        raise ValueError(f"Unmapped coastline support: {gap} square metres")
    source_rows, domain = land_sources(config["source_bbox_wgs84"], land, water)
    atomic_write_json(out / "source_cells.json", source_rows, overwrite=False)
    for key, value in geometry.items():
        gpd.GeoDataFrame({"layer": [key]}, geometry=[value], crs=32610).to_parquet(
            out / f"{key}.parquet"
        )
    (out / "prepared").mkdir()
    arrays = {
        key: np.lib.format.open_memmap(
            out / "prepared" / f"{key}.npy", mode="w+", dtype=dtype, shape=grid_shape
        )
        for key, dtype in DTYPES.items()
    }
    counts = {key: 0 for key in ("worldcover_missing", "canopy_unknown", "roof_unknown")}
    # Each tile has a complete 30m parent lattice. Exact VRT tolerance prevents
    # categorical seams. Terrain uses the previous pilot's bilinear 30m policy.
    with rasterio.Env(GDAL_CACHEMAX=16 * 1024**2):
        for row in range(0, grid_shape[0], 1536):
            if time.perf_counter() - started > config["wall_seconds"]:
                raise ValueError("Preparation wall-time cap exceeded; partial outputs preserved")
            for col in range(0, grid_shape[1], 1536):
                height, width = min(1536, grid_shape[0] - row), min(1536, grid_shape[1] - col)
                local = grid * Affine.translation(col, row)
                local_shape = (height, width)
                tile = prepare_tile(
                    config, canopy_assets, wc["assets"], land, water, local, local_shape
                )
                for key, array in tile.items():
                    arrays[key][row : row + height, col : col + width] = array
                counts["worldcover_missing"] += int((tile["classes"] == 0).sum())
                counts["canopy_unknown"] += int(tile["canopy_unknown"].sum())
                counts["roof_unknown"] += int(tile["building_unknown"].sum())
            for array in arrays.values():
                array.flush()
            print(f"Prepared rows {row + height}/{grid_shape[0]}", flush=True)
    del arrays
    metadata = dict(
        contract=PREPARED_CONTRACT,
        method=config["method"],
        observer_ground_contract=OBSERVER_GROUND_CONTRACT,
        config=config,
        shape=list(grid_shape),
        transform=list(grid)[:6],
        bounds_wgs84=list(bounds_ll),
        source_count=len(source_rows),
        source_land_km2=land.intersection(domain).area / 1e6,
        source_water_km2=water.intersection(domain).area / 1e6,
        geometry_gap_m2=gap,
        counts=counts,
        preparation_seconds=time.perf_counter() - started,
        preparation_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        preparation_rss_kind="OS_process_peak_including_file_backed_pages",
        input_assets={"canopy": canopy_assets, "worldcover": wc["assets"]},
        code_sha256=digest(__file__),
        model_recipe=MODEL_RECIPE,
    )
    atomic_write_json(out / "prepared.json", metadata, overwrite=False)
    print(json.dumps({k: metadata[k] for k in ("source_count", "preparation_seconds", "counts")}))
    return metadata


def source_window(arrays: Any, grid: Any, geometry: Any, *, radius: Any = RADIUS) -> Any:
    """Read a contiguous source-reach window, retaining the parent lattice."""
    minx, miny, maxx, maxy = geometry.bounds
    window_grid, window_shape = extended_grid(
        grid, np.array([[minx, miny, 0], [maxx, maxy, 0]]), radius
    )
    col, row = (~grid) * (window_grid.c, window_grid.f)
    row, col = round(row), round(col)
    height, width = window_shape
    if (
        row < 0
        or col < 0
        or row + height > arrays["ground"].shape[0]
        or (col + width > arrays["ground"].shape[1])
    ):
        raise ValueError("Source radius exceeds prepared raster")
    data = {k: np.array(a[row : row + height, col : col + width]) for k, a in arrays.items()}
    data["transform"] = window_grid
    return data


def observer_design(source: Any, data: Any, *, samples: Any = 15) -> Any:
    """Deterministic outdoor land samples; roofs are excluded on the 10m grid."""
    geometry = shape(source["source_land"])
    grid = data["transform"]
    c0, r0 = (~grid) * (geometry.bounds[0], geometry.bounds[3])
    c1, r1 = (~grid) * (geometry.bounds[2], geometry.bounds[1])
    c0, r0 = max(0, math.floor(c0)), max(0, math.floor(r0))
    c1, r1 = math.ceil(c1), math.ceil(r1)
    present = data["building_present"][r0:r1, c0:c1]
    if present.any():
        roofs = unary_union(
            [
                shape(g)
                for g, _ in shapes(
                    present.astype("uint8"),
                    mask=present,
                    transform=grid * Affine.translation(c0, r0),
                )
            ]
        )
        geometry = geometry.difference(roofs)
    if geometry.is_empty:
        raise ValueError("Source has no outdoor land support")
    frame = sample_points_in_source_geometry(
        source["source_h3"],
        geometry,
        samples,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=samples,
    ).to_crs(32610)
    xy = np.column_stack([frame.geometry.x, frame.geometry.y])
    rr, cc = rasterio.transform.rowcol(grid, xy[:, 0], xy[:, 1])
    if data["building_present"][rr, cc].any():
        raise ValueError("Observer falls in excluded roof support")
    source_ground = data["observer_ground"][rr, cc]
    if not np.isfinite(source_ground).all():
        raise ValueError("Unresolved unflattened land observer ground")
    return np.column_stack([xy, source_ground]), mapping(geometry)
