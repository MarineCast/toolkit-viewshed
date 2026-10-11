"""Bounded, offline exact-point obstruction sensitivity; never promotes products.

Usage: PYTHONPATH=src python scripts/compare_obstruction_points.py CONFIG.json
The config names a retained pilot ZIP, native canopy inventory, pinned GBA subset
and acquisition manifest, land/water polygons, output directory and exact sites.
"""

import base64
import hashlib
import io
import json
from importlib.resources import files
import re
import sys
import zlib
from pathlib import Path
from zipfile import ZipFile

import geopandas as gpd
import h3
import numpy as np
import polars as pl
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject
from shapely.geometry import Point, Polygon, box, mapping, shape as geometry_shape
from shapely.ops import transform as project

from viewshed_toolkit import direct_ray_envelope_visibility, direct_ray_visibility
from viewshed_toolkit.pipeline.prepare.elevation.buildings import (
    BuildingSurface,
    rasterize_building_footprints,
)
from viewshed_toolkit.pipeline.prepare.elevation.canopy import qualify_water_footprints
from viewshed_toolkit.pipeline.prepare.elevation.obstruction import compose_above_ground_surface
from viewshed_toolkit.pipeline.prepare.elevation.terrain import sample_observer_ground_elevation
from viewshed_toolkit.pipeline.providers.global_building_atlas import iter_gba_subset
from viewshed_toolkit.pipeline.weights.obstruction import above_ground_visibility
from viewshed_toolkit.pipeline.weights.rays import DIRECT_ENVELOPE_CONTRACT, DIRECT_RAY_CONTRACT
from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_canopy_visibility


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def counts(values):
    result = {
        k: int((values == v).sum())
        for k, v in (("clear", 1), ("blocked", 0), ("unknown", -1), ("out_of_range", -2))
    }
    total = len(values)
    assert sum(result.values()) == total
    if not total:
        return result | dict(total=0, clear_lower=None, clear_upper=None, complete_mean=None)
    result.update(
        total=total,
        clear_lower=result["clear"] / total,
        clear_upper=(result["clear"] + result["unknown"]) / total,
        complete_mean=None if result["unknown"] else result["clear"] / total,
    )
    return result


def export_comparison(report, out):
    """Embed every retained target; verify displayed counts against the Parquet."""
    display = dict(report, sites=[])
    forward = Transformer.from_crs(4326, 32610, always_xy=True)
    for site in report["sites"]:
        table = pl.read_parquet(out / (site["name"] + ".parquet"))
        x, y = forward.transform(site["lon"], site["lat"])
        states = {}
        for key, expected in site["results"].items():
            values = table[key].to_numpy()
            assert counts(values) == expected
            states[key] = values.tolist()
        targets = dict(
            dx=(table["target_x_m"] - x).round(2).to_list(),
            dy=(table["target_y_m"] - y).round(2).to_list(),
            h3=table["target_h3"].to_list(),
            states=states,
        )
        land_xy = project(
            lambda a, b, z=None: (np.asarray(a) - x, np.asarray(b) - y),
            project(forward.transform, geometry_shape(site["land"])),
        )
        display["sites"].append(dict(site, targets=targets, land_xy=mapping(land_xy)))
    encoded = json.dumps(display, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    template = (
        files("viewshed_toolkit.resources")
        .joinpath("obstruction_point_comparison.html")
        .read_text()
    )
    (out / "comparison.html").write_text(template.replace("__PAYLOAD__", encoded))


def run(config):
    out = Path(config["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    acquisition = json.loads(Path(config["gba_manifest"]).read_text())
    assert checksum(config["gba_subset"]) == acquisition["subset_sha256"]
    forward = Transformer.from_crs(4326, 32610, always_xy=True)
    back = Transformer.from_crs(32610, 4326, always_xy=True)
    to_gba = Transformer.from_crs(32610, 3857, always_xy=True)
    land = gpd.read_parquet(config["land"]).to_crs(32610).geometry.union_all()
    water = gpd.read_parquet(config["water"]).to_crs(32610).geometry.union_all()
    inventory = json.loads(Path(config["canopy_inventory"]).read_text())["raw_canopy"]
    inputs = {
        k: {"path": config[k], "sha256": checksum(config[k])}
        for k in ("pilot_zip", "land", "water", "gba_subset", "gba_manifest", "canopy_inventory")
    }
    results = []
    with ZipFile(config["pilot_zip"]) as archive:
        for site in config["sites"]:
            source = site["source_h3"]
            asset = json.loads(
                re.search(rb"\]=(.*);", archive.read(f"raster_inputs/{source}.js"))[1]
            )
            arrays = {}
            shape = (asset["height"], asset["width"])
            transform = Affine(*asset["transform"])
            for key in ("dem", "dem_original_delta", "chm_lower", "building", "flags"):
                entry = asset["arrays"][key]
                raw = zlib.decompress(base64.b64decode(entry["zlib_base64"]))
                assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
                arrays[key] = np.frombuffer(
                    raw, dtype="uint8" if entry["dtype"] == "uint8" else "<f4"
                ).reshape(shape)
            stored = pl.read_parquet(
                io.BytesIO(archive.read(f"weights/obstruction/{source}.parquet"))
            )
            cells = sorted(stored["target_h3"].to_list())
            polygons = [
                project(
                    forward.transform,
                    Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]),
                )
                for cell in cells
            ]
            codes = rasterize(
                [(p, i) for i, p in enumerate(polygons)],
                out_shape=shape,
                transform=transform,
                fill=-1,
                dtype="int32",
            )
            codes[(arrays["flags"] & 128) == 0] = -1
            rr, cc = np.where(codes >= 0)
            if len(rr) != int(stored["target_sample_count"].sum()):
                raise ValueError("Retained water-target population mismatch")
            x, y = forward.transform(site["lon"], site["lat"])
            xs, ys = rasterio.transform.xy(transform, rr, cc)
            targets = np.column_stack((xs, ys))
            inside = np.hypot(targets[:, 0] - x, targets[:, 1] - y) <= 5000
            rr, cc, targets = rr[inside], cc[inside], targets[inside]
            target_h3 = [cells[c] for c in codes[rr, cc]]
            oy, ox = map(int, rasterio.transform.rowcol(transform, x, y))
            source_ground = sample_observer_ground_elevation(
                Path(site["projected_ground"]), Path(site["projected_ground"]), x, y
            )
            inputs[site["name"] + "_ground"] = {
                "path": site["projected_ground"],
                "sha256": checksum(site["projected_ground"]),
            }
            flags = arrays["flags"]
            buildings = BuildingSurface(
                arrays["building"], (flags & 8) != 0, (flags & 16) != 0, (flags & 2) != 0, 0, 0, 0
            )
            grid_r, grid_c = np.indices(shape)
            # Same retained coarse clearance convention, including the central cell.
            clear = (
                np.hypot(
                    transform.c + (grid_c + 0.5) * 30 - x, transform.f - (grid_r + 0.5) * 30 - y
                )
                <= 30
            )
            clear[oy, ox] = True
            states = {}
            common = dict(observer_row=oy, observer_col=ox, pixel_size_m=30, max_distance_m=10000)
            for model, scalar in (
                ("legacy_30m_edge", None),
                ("ground_fix_30m_edge", source_ground),
            ):
                for component in ("bare", "canopy", "building", "joint"):
                    canopy = (
                        arrays["chm_lower"] if component in ("canopy", "joint") else np.zeros(shape)
                    )
                    unknown = (
                        (flags & 1) != 0
                        if component in ("canopy", "joint")
                        else np.zeros(shape, bool)
                    )
                    if component in ("bare", "canopy"):
                        values = partial_canopy_visibility(
                            arrays["dem"],
                            canopy,
                            unknown,
                            clearance_mask=clear,
                            observer_ground_elevation_m=scalar,
                            **common,
                        )
                    else:
                        values = above_ground_visibility(
                            arrays["dem"],
                            canopy,
                            unknown,
                            buildings,
                            qualified_water=(flags & 32) != 0,
                            canopy_clearance_mask=clear,
                            grounded_observer=True,
                            observer_ground_elevation_m=scalar,
                            **common,
                        )
                    states[model + ":" + component] = values[rr, cc]
            for component in ("bare", "canopy", "building", "joint"):
                canopy = (
                    arrays["chm_lower"].copy()
                    if component in ("canopy", "joint")
                    else np.zeros(shape)
                )
                unknown = (
                    ((flags & 1) != 0)
                    if component in ("canopy", "joint")
                    else np.zeros(shape, bool)
                )
                canopy[clear] = 0
                unknown[clear] = False
                surface = compose_above_ground_surface(
                    canopy,
                    unknown,
                    (
                        buildings.lower_height
                        if component in ("building", "joint")
                        else np.zeros(shape)
                    ),
                    (
                        buildings.unknown
                        if component in ("building", "joint")
                        else np.zeros(shape, bool)
                    ),
                    qualified_water=(flags & 32) != 0,
                    building_present=(
                        buildings.present
                        if component in ("building", "joint")
                        else np.zeros(shape, bool)
                    ),
                )
                states["direct_30m:" + component] = direct_ray_visibility(
                    arrays["dem"],
                    surface.lower_height,
                    surface.unknown,
                    transform=transform,
                    observer_xy=(x, y),
                    targets_xy=targets,
                    observer_ground_elevation_m=source_ground,
                    max_distance_m=5000,
                )
            # Fixed origin: fine grid is nested in retained 30 m grid, not recentered per site.
            r0, r1 = max(0, oy - 174), min(shape[0], oy + 175)
            c0, c1 = max(0, ox - 174), min(shape[1], ox + 175)
            gt = transform * Affine.translation(c0, r0) * Affine.scale(1 / 3)
            fine_shape = ((r1 - r0) * 3, (c1 - c0) * 3)
            extent = box(gt.c, gt.f + fine_shape[0] * gt.e, gt.c + fine_shape[1] * gt.a, gt.f)
            native = np.full(fine_shape, 255, dtype="uint8")
            used = []
            for entry in inventory:
                with rasterio.open(entry["path"]) as ds:
                    if not box(*ds.bounds).intersects(project(back.transform, extent)):
                        continue
                    assert checksum(entry["path"]) == entry["sha256"]
                    dest = np.full(fine_shape, 255, dtype="uint8")
                    reproject(
                        rasterio.band(ds, 1),
                        dest,
                        src_transform=ds.transform,
                        src_crs=ds.crs,
                        src_nodata=255,
                        dst_transform=gt,
                        dst_crs="EPSG:32610",
                        dst_nodata=255,
                        resampling=Resampling.nearest,
                        warp_mem_limit=32,
                    )
                    native[dest != 255] = dest[dest != 255]
                    used.append(entry)
            assert used
            pure = qualify_water_footprints(
                water,
                land,
                transform=gt,
                shape=fine_shape,
                geometry_crs=32610,
                raster_crs=32610,
                supported_mask=np.ones(fine_shape, bool),
            )
            wm = rasterize([(water, 1)], out_shape=fine_shape, transform=gt, dtype="uint8") == 1
            original = arrays["dem"] + arrays["dem_original_delta"]
            ground = np.repeat(np.repeat(original[r0:r1, c0:c1], 3, 0), 3, 1).copy()
            ground[wm | pure] = 0
            canopy = np.where(native == 255, 0, native).astype("float32")
            cu = (native == 255) & ~pure
            canopy[pure] = 0
            bbox = project(to_gba.transform, extent).bounds
            study = project(
                Transformer.from_crs(4326, 3857, always_xy=True).transform,
                box(*acquisition["study_bbox"]),
            )
            assert study.covers(box(*bbox))
            fine_buildings = rasterize_building_footprints(
                (
                    (f.geometry_wkb, f.height_m)
                    for f in iter_gba_subset(Path(config["gba_subset"]), bbox)
                ),
                shape=fine_shape,
                transform=gt,
                crs=32610,
                all_touched=False,
            )
            fyr, fxc = np.indices(fine_shape)
            fx = gt.c + (fxc + 0.5) * gt.a
            fy = gt.f + (fyr + 0.5) * gt.e
            clearance = np.hypot(fx - x, fy - y) <= 30
            row, col = rasterio.transform.rowcol(gt, x, y)
            clearance[row, col] = True
            canopy[clearance] = 0
            cu[clearance] = False
            zero, known = np.zeros(fine_shape), np.zeros(fine_shape, bool)
            bare = compose_above_ground_surface(
                zero,
                known,
                zero,
                known,
                qualified_water=pure,
                building_present=np.zeros(fine_shape, bool),
            )
            canopy_surface = compose_above_ground_surface(
                canopy,
                cu,
                zero,
                known,
                qualified_water=pure,
                building_present=np.zeros(fine_shape, bool),
            )
            building_surface = compose_above_ground_surface(
                zero,
                known,
                fine_buildings.lower_height,
                fine_buildings.unknown,
                qualified_water=pure,
                building_present=fine_buildings.present,
            )
            joint = compose_above_ground_surface(
                canopy,
                cu,
                fine_buildings.lower_height,
                fine_buildings.unknown,
                qualified_water=pure,
                building_present=fine_buildings.present,
            )
            for component, surface in (
                ("bare", bare),
                ("canopy", canopy_surface),
                ("building", building_surface),
                ("joint", joint),
            ):
                states["native_10m_direct:" + component] = direct_ray_visibility(
                    ground,
                    surface.lower_height,
                    surface.unknown,
                    transform=gt,
                    observer_xy=(x, y),
                    targets_xy=targets,
                    observer_ground_elevation_m=source_ground,
                    max_distance_m=5000,
                )
            for component, upper in (("canopy", canopy_surface), ("joint", joint)):
                floor = compose_above_ground_surface(
                    zero,
                    cu,
                    fine_buildings.lower_height if component == "joint" else zero,
                    fine_buildings.unknown if component == "joint" else known,
                    qualified_water=pure,
                    building_present=fine_buildings.present if component == "joint" else known,
                )
                states["native_10m_envelope:" + component] = direct_ray_envelope_visibility(
                    ground,
                    floor.lower_height,
                    upper.lower_height,
                    upper.unknown,
                    transform=gt,
                    observer_xy=(x, y),
                    targets_xy=targets,
                    observer_ground_elevation_m=source_ground,
                    max_distance_m=5000,
                )
            # Persist complete target population, including unknown, with fixed identity.
            lon, lat = back.transform(targets[:, 0], targets[:, 1])
            table = pl.DataFrame(
                dict(
                    source_h3=[source] * len(rr),
                    viewpoint_id=[site["name"]] * len(rr),
                    target_h3=target_h3,
                    target_row=rr,
                    target_col=cc,
                    target_x_m=targets[:, 0],
                    target_y_m=targets[:, 1],
                    lon=lon,
                    lat=lat,
                    **states,
                )
            )
            assert (
                table.unique(["source_h3", "viewpoint_id", "target_row", "target_col"]).height
                == table.height
            )
            table.write_parquet(out / (site["name"] + ".parquet"))
            clip_water = water.intersection(extent)
            clip_land = land.intersection(extent)
            result = dict(
                **site,
                source_ground_m=source_ground,
                old_source_ground_m=float(arrays["dem"][oy, ox]),
                target_count=len(rr),
                native_canopy_inputs=used,
                observer_on_mapped_land=land.covers(Point(x, y)),
                observer_in_building_cell=bool(fine_buildings.present[row, col]),
                observer_building_height_m=float(fine_buildings.lower_height[row, col]),
                native_source_canopy_m=None if native[row, col] == 255 else int(native[row, col]),
                building_footprint_count=fine_buildings.footprint_count,
                results={k: counts(v) for k, v in states.items()},
                water=mapping(project(back.transform, clip_water)),
                land=mapping(project(back.transform, clip_land)),
            )
            results.append(result)
            print(json.dumps({"site": site["name"], "results": result["results"]}), flush=True)
    report = dict(
        contract="exact_point_obstruction_sensitivity_v1",
        crs="EPSG:32610",
        radius_m=5000,
        native_ray_contract=DIRECT_RAY_CONTRACT,
        canopy_envelope_contract=DIRECT_ENVELOPE_CONTRACT,
        observer_height_m=1.7,
        target_height_m=1.0,
        curvature_coefficient=0.85714,
        earth_radius_m=6378137.0,
        canopy_envelope="zero floor / predicted CHM ceiling; no porosity estimate; modeled bounds, not empirical confidence",
        canopy_resampling="nearest; nominal native 10m ETH2020; no max aggregation",
        terrain_support="retained 30m; repeated onto nested 10m grid; no new precision",
        building_rasterization="10m cell centers, max overlapping heights; absence unknown",
        origin="exact coordinates; grounded source cell; 30m canopy clearance only",
        target_population="same retained 30m water centers inside exact 5km radius",
        scope="unweighted diagnostic; no production promotion or regional rebuild; no field validation",
        states={"1": "clear", "0": "modeled blocked", "-1": "unknown", "-2": "out of range"},
        inputs=inputs,
        pinned_gba_revisions=acquisition["pinned_revisions"],
        sites=results,
    )
    (out / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    export_comparison(report, out)
    return report


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
