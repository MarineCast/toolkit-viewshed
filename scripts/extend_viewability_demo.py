"""Rebuild bounded demo rasters and water grids from retained regional sources.

Usage: PYTHONPATH=src python scripts/extend_viewability_demo.py CONFIG.json
No acquisition, canonical pipeline, promotion, or modification of prior reports.
"""

import gc
import hashlib
import inspect
import json
import math
import sys
import time
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import polars as pl
import rasterio
from affine import Affine
from benchmark_cell_scores import digest, target_design
from map_average_viewability import PARTS_SQL, make_artifact, plot_site, target_population
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import Point, box
from shapely.ops import unary_union

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit.pipeline.prepare.elevation.buildings import rasterize_building_footprints
from viewshed_toolkit.pipeline.prepare.elevation.canopy import qualify_water_footprints
from viewshed_toolkit.pipeline.providers.global_building_atlas import iter_gba_subset
from viewshed_toolkit.pipeline.weights.rays import DIRECT_RAY_CONTRACT
from viewshed_toolkit.pipeline.weights.transmission import calculate
from viewshed_toolkit.pipeline.weights.vegetation.classified import (
    CLASSIFIED_CANOPY_CONTRACT,
    classified_ray_support,
)

CONTRACT = "extended_retained_viewability_demo_v1"
METRES_PER_MILE = 1609.344


def extended_grid(transform, observers, radius):
    """Pad the entire observer reach on the original nested 30m/10m lattice."""
    if (
        transform.a != 10
        or transform.e != -10
        or transform.b
        or transform.d
        or not math.isfinite(radius)
        or radius <= 0
        or observers.ndim != 2
        or observers.shape[1] != 3
        or not len(observers)
        or not np.isfinite(observers).all()
    ):
        raise ValueError("Finite observers, positive radius and nested 10m grid required")
    lo, hi = observers[:, :2].min(0) - radius, observers[:, :2].max(0) + radius
    c0 = math.floor((lo[0] - transform.c) / 30) - 1
    c1 = math.ceil((hi[0] - transform.c) / 30) + 1
    r0 = math.floor((transform.f - hi[1]) / 30) - 1
    r1 = math.ceil((transform.f - lo[1]) / 30) + 1
    return transform * Affine.translation(c0 * 3, r0 * 3), ((r1 - r0) * 3, (c1 - c0) * 3)


def align(path, grid_shape, transform, *, nodata, dtype):
    with (
        rasterio.open(path) as src,
        WarpedVRT(
            src,
            crs="EPSG:32610",
            transform=transform,
            width=grid_shape[1],
            height=grid_shape[0],
            resampling=Resampling.nearest,
            nodata=nodata,
            dtype=dtype,
            warp_mem_limit=64,
            tolerance=1e-8,
        ) as vrt,
    ):
        return vrt.read(1)


def load_classes(assets, grid_shape, transform):
    """Categorical alignment with near-exact transforms across tile boundaries."""
    result = np.zeros(grid_shape, dtype="uint8")
    for asset in assets:
        layer = align(asset["path"], grid_shape, transform, nodata=0, dtype="uint8")
        if ((result != 0) & (layer != 0) & (result != layer)).any():
            raise ValueError("Conflicting overlapping WorldCover classifications")
        result[layer != 0] = layer[layer != 0]
    return result


def overlay_previous(data, old, original, transform):
    """Preserve the intersecting prior window, including smaller configured reach."""
    col, row = (~transform) * (original.c, original.f)
    row, col = round(row), round(col)
    height, width = old["ground"].shape
    target_height, target_width = data["ground"].shape
    r0, c0 = max(0, row), max(0, col)
    r1, c1 = min(target_height, row + height), min(target_width, col + width)
    if r1 > r0 and c1 > c0:
        for key in data:
            data[key][r0:r1, c0:c1] = old[key][r0 - row : r1 - row, c0 - col : c1 - col]


def prepare(site, old_path, inventory, dem, assets, acquisition, subset, radius, out):
    with np.load(old_path, allow_pickle=False) as old:
        original = Affine(*old["transform"])
    observers = np.asarray(site["observers"])
    transform, grid_shape = extended_grid(original, observers, radius)
    bounds = rasterio.transform.array_bounds(*grid_shape, transform)
    bounds_ll = transform_bounds(32610, 4326, *bounds, densify_pts=41)
    if not box(*acquisition["study_bbox"]).covers(box(*bounds_ll)):
        raise ValueError("Expanded raster exceeds validated building subset")
    canopy_bounds = unary_union([box(*a["bounds"]) for a in inventory["raw_canopy"]])
    if not canopy_bounds.covers(box(*bounds_ll)):
        raise ValueError("Expanded raster exceeds retained native canopy tiles")
    metadata = site["metadata"]
    water = gpd.read_parquet(metadata["inputs"]["water"]["path"]).to_crs(32610).geometry.union_all()
    land = gpd.read_parquet(metadata["inputs"]["land"]["path"]).to_crs(32610).geometry.union_all()
    extent = box(*bounds)
    water, land = water.intersection(extent), land.intersection(extent)
    qualified = qualify_water_footprints(
        water,
        land,
        transform=transform,
        shape=grid_shape,
        geometry_crs=32610,
        raster_crs=32610,
        supported_mask=np.ones(grid_shape, bool),
    )
    water_centres = rasterize(
        [(water, 1)], out_shape=grid_shape, transform=transform, dtype="uint8"
    ).astype(bool)
    coarse_shape = (grid_shape[0] // 3, grid_shape[1] // 3)
    ground = align(dem, coarse_shape, transform * Affine.scale(3), nodata=np.nan, dtype="float32")
    ground = ground.repeat(3, axis=0).repeat(3, axis=1)
    ground[water_centres] = 0  # Existing declared 0m water convention, not bathymetry.
    if not np.isfinite(ground).all():
        raise ValueError("Expanded terrain contains unresolved nonwater elevation gaps")
    native = np.full(grid_shape, 255, dtype="uint8")
    for entry in inventory["raw_canopy"]:
        if box(*entry["bounds"]).intersects(box(*bounds_ll)):
            layer = align(entry["path"], grid_shape, transform, nodata=255, dtype="uint8")
            valid = layer != 255
            native[valid] = layer[valid]
    canopy = np.where(native == 255, 0, native).astype("float32")
    canopy_unknown = (native == 255) & ~qualified
    canopy[qualified] = 0
    buildings = rasterize_building_footprints(
        (
            (f.geometry_wkb, f.height_m)
            for f in iter_gba_subset(Path(subset), transform_bounds(32610, 3857, *bounds))
        ),
        shape=grid_shape,
        transform=transform,
        crs=32610,
        all_touched=False,
        inventory_policy="assume_complete",
    )
    data = dict(
        ground=ground,
        canopy=canopy,
        canopy_unknown=canopy_unknown,
        building=buildings.lower_height,
        building_unknown=buildings.unknown,
        building_present=buildings.present,
        qualified_water=qualified,
    )
    # Retain exact prior terrain/coast/obstacle values where previously modeled.
    with np.load(old_path, allow_pickle=False) as old:
        overlay_previous(data, old, original, transform)
    cache = out / "rasters"
    cache.mkdir(exist_ok=True)
    # Separate georeferenced bands keep numeric height and unknown state explicit.
    for key, array in data.items():
        path = cache / f"{site['name']}-{key}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=grid_shape[0],
            width=grid_shape[1],
            count=1,
            dtype="uint8" if array.dtype == np.bool_ else array.dtype,
            crs=32610,
            transform=transform,
            compress="deflate",
            tiled=True,
        ) as dst:
            dst.write(array.astype("uint8") if array.dtype == np.bool_ else array, 1)
            dst.update_tags(contract=CONTRACT, radius_m=str(radius), layer=key)
    classes = load_classes(assets, grid_shape, transform)
    data["transform"] = transform
    support = dict(
        shape=list(grid_shape),
        transform=list(transform)[:6],
        bounds_utm=list(bounds),
        bounds_wgs84=list(bounds_ll),
        ground_nonfinite_pixels=int((~np.isfinite(ground)).sum()),
        worldcover_missing_pixels=int((classes == 0).sum()),
        roof_unknown_pixels=int(data["building_unknown"].sum()),
        building_footprints=buildings.footprint_count,
        previous_cache_overlay_sha256=digest(old_path),
        water_surface_m=0,
        building_inventory_policy="assume_complete",
        building_rasterization="cell_center_max_agl",
        reprojection_tolerance_source_pixels=1e-8,
    )
    atomic_write_json(cache / f"{site['name']}.json", support, overwrite=True)
    return data, classes, water, support


def run(config):
    required = {"trial_report", "terrain_raster", "output_dir", "radius_miles"}
    if not required <= set(config) or set(config) - required - {"sites"}:
        raise ValueError(
            "Required trial_report, terrain_raster, output_dir, radius_miles; optional sites"
        )
    miles = config["radius_miles"]
    if isinstance(miles, bool) or not isinstance(miles, (int, float)) or not 0 < miles <= 15:
        raise ValueError("Bounded demo radius must be in (0,15] miles")
    radius = miles * METRES_PER_MILE
    label = f"{miles:g} miles ({radius/1000:.2f} km)"
    trial_path = Path(config["trial_report"])
    trial = json.loads(trial_path.read_text())
    if trial["contract"] != "bounded_worldcover_canopy_trial_v1":
        raise ValueError("Retained classified canopy trial required")
    available = {site["name"] for site in trial["sites"]}
    selected_sites = config.get("sites", [site["name"] for site in trial["sites"]])
    if (
        not isinstance(selected_sites, list)
        or not selected_sites
        or not all(isinstance(name, str) for name in selected_sites)
        or len(set(selected_sites)) != len(selected_sites)
        or not set(selected_sites) <= available
    ):
        raise ValueError("sites must select unique known retained sites")
    out = Path(config["output_dir"])
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use a new empty demo output directory")
    out.mkdir(parents=True, exist_ok=True)
    inputs = trial["sites"][0]["metadata"]["inputs"]
    inventory = json.loads(Path(inputs["canopy_inventory"]["path"]).read_text())
    acquisition = json.loads(Path(inputs["gba_manifest"]["path"]).read_text())
    if acquisition["status"] != "completed_validated":
        raise ValueError("Validated building acquisition required")
    sources = {
        k: v
        for k, v in inputs.items()
        if k in ("land", "water", "gba_subset", "gba_manifest", "canopy_inventory")
    }
    sources.update({f"canopy_{i}": a for i, a in enumerate(inventory["raw_canopy"])})
    sources.update({f"worldcover_{i}": a for i, a in enumerate(trial["worldcover"]["assets"])})
    for entry in sources.values():
        if digest(entry["path"]) != entry["sha256"]:
            raise ValueError("Retained source checksum mismatch")
    sources["terrain"] = dict(
        path=config["terrain_raster"], sha256=digest(config["terrain_raster"])
    )
    if digest(inputs["gba_subset"]["path"]) != acquisition["subset_sha256"]:
        raise ValueError("Building acquisition/subset mismatch")
    benchmark = json.loads(Path(trial["config"]["benchmark_report"]).read_text())
    old_dir = Path(benchmark["config"]["input_dir"])
    all_rows, sites, svgs = [], [], {}
    for site in trial["sites"]:
        if site["name"] not in selected_sites:
            continue
        name = site["name"]
        old_path = old_dir / f"{name}.npz"
        if digest(old_path) != trial["prepared_checksums"][name]["npz"]:
            raise ValueError("Retained prior cache checksum mismatch")
        observers = np.asarray(site["observers"])
        print(f"Preparing {name} {label} rasters", flush=True)
        preparation_started = time.perf_counter()
        data, classes, water, support = prepare(
            site,
            old_path,
            inventory,
            config["terrain_raster"],
            trial["worldcover"]["assets"],
            acquisition,
            inputs["gba_subset"]["path"],
            radius,
            out,
        )
        targets, rr, cc, inside = target_population(
            water, data["transform"], data["ground"].shape, observers, max_distance_m=radius
        )
        if not inside.all():
            raise ValueError("Expanded raster does not cover every target")
        lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(
            targets[:, 0], targets[:, 1]
        )
        cells, codes = np.unique(
            np.asarray([h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)]),
            return_inverse=True,
        )
        prep_seconds = time.perf_counter() - preparation_started
        selected, weights = target_design(rr, cc, codes, 2, census_threshold=1000)
        print(
            f"{name}: {len(cells)} grids, {len(targets)} water points; calculating full reference",
            flush=True,
        )
        reference, full_s = calculate(
            data, classes, observers, targets, codes, np.ones(len(targets)), cells, radius
        )
        fast, fast_s = calculate(
            data, classes, observers, targets[selected], codes[selected], weights, cells, radius
        )
        error = np.concatenate(
            [np.abs(reference[k] - fast[k]) for k in ("lower", "upper", "unknown")]
        )
        accepted = error.max() <= 0.10 and np.quantile(error, 0.95) <= 0.05
        result = fast if accepted else reference
        counts = np.bincount(codes, minlength=len(cells))
        rows = [
            dict(
                site=name,
                source_type="land",
                source_h3=site["source_h3"],
                target_h3=str(cell),
                lower=float(result["lower"][i]),
                upper=float(result["upper"][i]),
                average_viewability=result["score"][i],
                input_unknown_fraction=float(result["unknown"][i]),
                uncovered_fraction=0.0,
                reference_lower=float(reference["lower"][i]),
                reference_upper=float(reference["upper"][i]),
                reference_unknown_fraction=float(reference["unknown"][i]),
                out_of_range_fraction=float(result["out_of_range"][i]),
                target_water_pixel_count=int(counts[i]),
                obstacle_cache_coverage=1.0,
                observer_count=len(observers),
                max_distance_m=radius,
                target_scope=(
                    "30m water centres in union of observer reach; "
                    "partial boundary H3 water portions"
                ),
                model_contract=CLASSIFIED_CANOPY_CONTRACT,
                beta_per_m=0.002,
                water_class_policy="zero_if_missing",
                sampling="fast" if accepted else "full",
            )
            for i, cell in enumerate(cells)
        ]

        total_pixels = len(targets)

        def mean(key, site_rows=rows, total=total_pixels):
            return sum(r[key] * r["target_water_pixel_count"] for r in site_rows) / total

        info = dict(
            site=name,
            target_cells=len(cells),
            target_water_pixels=len(targets),
            cache_missing_pixels=0,
            preparation_seconds=prep_seconds,
            full_seconds=full_s,
            fast_seconds=fast_s,
            maximum_sampling_error=float(error.max()),
            mean_lower=mean("lower"),
            mean_upper=mean("upper"),
            mean_unknown=mean("input_unknown_fraction"),
            mean_uncovered=0.0,
            raster_support=support,
        )
        sites.append(info)
        envelope = unary_union([Point(x, y).buffer(radius, quad_segs=128) for x, y, _ in observers])
        svgs[name] = plot_site(site, rows, water, envelope, out, distance_label=label)
        all_rows.extend(rows)
        print(json.dumps(info), flush=True)
        del data, classes
        gc.collect()
    table = pl.DataFrame(all_rows).sort(["source_type", "source_h3", "target_h3"])
    if table.unique(["source_type", "source_h3", "target_h3"]).height != table.height:
        raise ValueError("Duplicate source-target identity")
    atomic_sink_parquet(table.lazy(), out / "average_viewability.parquet", overwrite=True)
    parts = pl.SQLContext(target_scores=table).execute(PARTS_SQL).collect().to_dicts()
    artifact = make_artifact(table.to_dicts(), sites, svgs, parts, distance_label=label)
    artifact["manifest"]["title"] += f" · {miles:g} miles"
    artifact["manifest"]["blocks"][0]["body"] = "# " + artifact["manifest"]["title"]
    summary = dict(
        contract=CONTRACT,
        config=config,
        radius_m=radius,
        sources=sources,
        previous_report_sha256=digest(trial_path),
        sites=sites,
        pairs=table.to_dicts(),
        input_model=trial["model"],
        model={
            **trial["model"],
            "contract": CLASSIFIED_CANOPY_CONTRACT,
            "direct_ray_contract": DIRECT_RAY_CONTRACT,
        },
        worldcover=trial["worldcover"],
        code_checksums={
            Path(p).name: digest(p)
            for p in [__file__, inspect.getfile(plot_site), inspect.getfile(classified_ray_support)]
        },
        scope=(
            "All 30m water centres within requested radius of any saved observer; "
            "complete raster extent; each observer has its own cutoff; same full denominator; "
            "curvature active; no additional distance decay"
        ),
        downloads_or_publication=False,
    )
    summary["scientific_signature"] = hashlib.sha256(
        json.dumps(summary, sort_keys=True).encode()
    ).hexdigest()
    atomic_write_json(out / "average_viewability.json", summary, overwrite=True)
    atomic_write_json(out / "artifact.json", artifact, overwrite=True)
    return summary


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
