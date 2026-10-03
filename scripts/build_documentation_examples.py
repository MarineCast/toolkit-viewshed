"""Export validated real outputs; --check uses only the standard library, offline."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

VERSION = "san_juan_lessons_v1"
METRICS = (
    "line_of_sight_support",
    "physical_viewability",
    "distance_detection_weight",
    "distance_weighted_los_support",
    "vegetation_attenuation",
    "distance_adjusted_viewability",
)


def encode(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(value))


def read(path):
    def invalid(value):
        raise ValueError(f"Nonfinite JSON: {value}")

    return json.loads(path.read_text(), parse_constant=invalid)


def load_pairs(output):
    data = read(output / "pairs.json")
    return [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]


def check(output):
    """Integrity and internal scientific assertions; not a raw-data reproduction."""
    manifest = read(output / "manifest.json")
    if manifest["export_contract"] != VERSION:
        raise ValueError("Unsupported export contract")
    for name, checksum in manifest["files"].items():
        path = (output / name).resolve()
        if not path.is_relative_to(output.resolve()) or digest(path.read_bytes()) != checksum:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    identity = {key: manifest[key] for key in ("export_contract", "generation_id", "files")}
    if manifest["bundle_id"] != digest(encode(identity)):
        raise ValueError("Bundle identity mismatch")
    pairs = load_pairs(output)
    indexed = {}
    for pair in pairs:
        key = (pair["source_type"], pair["source_h3"], pair["target_h3"])
        if key in indexed or pair["id"] in indexed:
            raise ValueError("Duplicate pair identity")
        indexed[key] = pair
        indexed[pair["id"]] = pair
        for metric in METRICS:
            value = pair[metric]
            if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("Invalid scientific value")
        if (
            abs(
                pair["distance_adjusted_viewability"]
                - pair["distance_weighted_los_support"]
                * (pair["vegetation_attenuation"] if pair["source_type"] == "land" else 1)
            )
            > 1e-6
        ):
            raise ValueError("Combined support disagrees with production formula")
        if pair["distance_weighted_los_support"] > pair["line_of_sight_support"] + 1e-6:
            raise ValueError("Integrated support exceeds unweighted support")
        if pair["source_type"] == "water" and pair["vegetation_state"] != "not_applicable":
            raise ValueError("Water canopy applicability is invalid")
    indexes = read(output / "indexes.json")
    for direction, field in (("forward", "source_h3"), ("inverse", "target_h3")):
        expected = {}
        for pair in pairs:
            expected.setdefault(pair["source_type"] + ":" + pair[field], []).append(pair["id"])
        if indexes[direction] != expected:
            raise ValueError("Forward/inverse indexes disagree with canonical pairs")
    cells = {feature["id"] for feature in read(output / "cells.geojson")["features"]}
    for pair in pairs:
        if pair["source_h3"] not in cells or pair["target_h3"] not in cells:
            raise ValueError("Pair references missing cell geometry")
    lessons = read(output / "lessons.json")
    for lesson in lessons:
        for assertion in lesson["assertions"]:
            pair = indexed[assertion["pair_id"]]
            if pair[assertion["field"]] != assertion["value"]:
                raise ValueError("Lesson assertion disagrees with authoritative exported row")
    profiles = read(output / "profiles.json")
    observers = {f["id"] for f in read(output / "observer-samples.geojson")["features"]}
    for profile in profiles:
        if profile["pair_id"] not in indexed or profile["observer_id"] not in observers:
            raise ValueError("Profile references missing actual observation design")
        if profile["kind"] != "explanatory_sampled_profile_not_engine_or_cell_diagnostic":
            raise ValueError("Profile diagnostic status is invalid")
    data_bytes = sum(
        len(gzip.compress((output / name).read_bytes(), mtime=0))
        for name in manifest["files"]
        if name.endswith((".json", ".geojson"))
    )
    total = sum((output / name).stat().st_size for name in manifest["files"])
    if data_bytes > 2 * 1024**2 or total > 5 * 1024**2:
        raise ValueError("Bundle exceeds documentation performance budget")
    return {
        "valid": True,
        "pairs": len(pairs),
        "gzip_data_bytes": data_bytes,
        "initial_bundle_bytes": total,
        "bundle_id": manifest["bundle_id"],
    }


def _export(config, output):
    # Native dependencies are intentionally absent from the offline path.
    import geopandas as gpd
    import h3
    import numpy as np
    import polars as pl
    import rasterio
    from pyproj import Transformer
    from shapely.geometry import Point, box, mapping, shape
    from shapely.ops import transform

    from viewshed_toolkit import load_app_config
    from viewshed_toolkit.pipeline.config import apply_source_type_policy
    from viewshed_toolkit.pipeline.config.paths import bbox_from_config
    from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
    from viewshed_toolkit.pipeline.contracts.generation import (
        byte_checksum,
        validate_generation_receipt,
    )
    from viewshed_toolkit.pipeline.finalize.final_artifacts import (
        materialize_static_viewability_outputs,
        static_scientific_config_hash,
    )
    from viewshed_toolkit.pipeline.prepare.area.raster_stack import ensure_canonical_raster_stack
    from viewshed_toolkit.pipeline.prepare.area.sampling import prepare_source_samples
    from viewshed_toolkit.pipeline.prepare.elevation.canopy import isolate_observer_canopy_surface
    from viewshed_toolkit.pipeline.weights.canopy_visibility import make_terrain_variant_app
    from viewshed_toolkit.pipeline.weights.terrain.gdal import (
        _load_terrain_source_cells,
        _projected_water_target_samples_for_app,
        _water_terrain_domains_for_app,
        expected_partition_metadata,
        partition_metadata_matches,
    )

    start = time.monotonic()
    app = load_app_config(config)
    paths = final_artifact_paths(config)
    outputs = materialize_static_viewability_outputs(config)
    receipts = {
        "land": outputs["land_static_weights"],
        "water": outputs["water_static_weights"],
        **{
            role + "_observation_geometry": outputs[role + "_observation_geometry"]
            for role in ("land", "water")
        },
    }
    receipt = validate_generation_receipt(
        paths.final_output_dir / "viewshed-generation.json",
        receipts,
        config_hash=static_scientific_config_hash(app.raw_config),
    )
    if "rasters" not in receipt["input_coverage"]:
        raise ValueError("Real teaching export requires a retained input coverage audit")
    if receipt["method"] != "4.0.0-research":
        raise ValueError("Teaching export requires directly observed unweighted canopy LOS")
    dual = pl.read_parquet(paths.dual_surface_factors).to_dicts()
    canopy_k = {(p["source_h3"], p["target_h3"]): p["weight_canopy_los_raw"] for p in dual}
    pairs = []
    for role in ("land", "water"):
        geometry = pl.read_parquet(outputs[role + "_observation_geometry"]).sort(
            "source_h3", "target_h3"
        )
        for number, row in enumerate(geometry.to_dicts()):
            if (
                row["legacy_static_schema"]
                or row["line_of_sight_support"] is None
                or row["physical_viewability"] is None
            ):
                raise ValueError(
                    "Legacy/unavailable geometry cannot be published as new teaching evidence"
                )
            pair = {
                key: value
                for key, value in row.items()
                if key in METRICS
                or key.endswith("_state")
                or key in {"source_type", "source_h3", "target_h3", "distance_km"}
            }
            pair.update(
                id=f"{role}:{row['source_h3']}:{row['target_h3']}",
                scenario="baseline",
                artifact_row={
                    "file": outputs[role + "_observation_geometry"].name,
                    "keys": [role, row["source_h3"], row["target_h3"]],
                    "sorted_row": number,
                },
            )
            pair["canopy_distance_weighted_los_support"] = (
                canopy_k[(row["source_h3"], row["target_h3"])]
                if role == "land"
                else row["distance_weighted_los_support"]
            )
            pairs.append(pair)
    for role, surface in (("land", "bare_earth"), ("land", "canopy"), ("water", "bare_earth")):
        variant = (
            make_terrain_variant_app(
                app,
                surface_model=surface,
                terrain_dir=app.paths.output_dir / "land/terrain_surfaces" / surface,
            )
            if role == "land"
            else apply_source_type_policy(app, role)
        )
        expected = expected_partition_metadata(variant)
        for source in sorted({p["source_h3"] for p in pairs if p["source_type"] == role}):
            partition = (
                variant.paths.partitioned_visibility_dir
                / f"source={role}"
                / f"source_h3_cell={source}.parquet"
            )
            if not partition_metadata_matches(partition, expected):
                raise ValueError(
                    "Prepared inputs or sampling no longer match validated real partitions; rebuild before export"
                )
    # Freeze reproducible selections once at export time, not on page load.
    land_pairs = [p for p in pairs if p["source_type"] == "land"]
    candidates = [p for p in land_pairs if p["source_h3"] == "8728d1072ffffff"] or land_pairs
    reduced = next(
        (
            p
            for p in candidates
            if p["distance_weighted_los_support"] > 0.02
            and 0.05 < p["vegetation_attenuation"] < 0.8
        ),
        None,
    )
    if reduced is None:
        reduced = next(
            (
                p
                for p in land_pairs
                if p["distance_weighted_los_support"] > 0.02
                and 0.05 < p["vegetation_attenuation"] < 0.8
            ),
            None,
        )
    if reduced is None:
        raise ValueError("No supported matched canopy reduction case in this generation")
    source = reduced["source_h3"]
    family = [p for p in land_pairs if p["source_h3"] == source]
    clear = sorted(
        [p for p in family if p["line_of_sight_support"] >= 0.99 and p["distance_km"] > 0.5],
        key=lambda p: p["distance_km"],
    )
    if len(clear) < 2:
        clear = sorted(
            [
                p
                for p in land_pairs
                if p["line_of_sight_support"] >= 0.99 and p["distance_km"] > 0.5
            ],
            key=lambda p: p["distance_km"],
        )
    near, far = clear[0], clear[-1]
    blocked = next(
        (p for p in family if p["line_of_sight_support"] == 0 and p["distance_km"] < 4), None
    )
    if blocked is None:
        blocked = next(
            p for p in land_pairs if p["line_of_sight_support"] == 0 and p["distance_km"] < 4
        )
    unaffected = next(
        p
        for p in land_pairs
        if p["distance_weighted_los_support"] > 0.02 and p["vegetation_attenuation"] > 0.98
    )
    inverse = [
        p
        for p in land_pairs
        if p["target_h3"] == reduced["target_h3"] and p["distance_adjusted_viewability"] > 0
    ]
    mixed = next(
        (p for p in pairs if p["source_type"] == "water" and p["source_h3"] == source), None
    )
    selected = [reduced, near, far, blocked, unaffected]
    lesson_specs = [
        ("inputs", [reduced], "Real inputs, not sightings"),
        ("samples", [reduced], "Actual deterministic observer design, not a centroid substitute"),
        (
            "distance",
            [near, far],
            "Unweighted bare support exceeds 0.5; this is not a controlled final-weight comparison",
        ),
        ("terrain", [near, blocked], "Positive versus zero bare LOS, independent of attenuation"),
        (
            "canopy",
            [reduced, unaffected],
            "Matched populations; a reduced pair and nearly unaffected pair",
        ),
        ("combined", [reduced, blocked], "Positive and modeled-zero support"),
        (
            "inverse",
            [reduced, *inverse[:3]],
            "Same target and canonical pair values in both directions",
        ),
    ]
    lessons = [
        {
            "id": ident,
            "scenario": "baseline",
            "pair_ids": list(dict.fromkeys(p["id"] for p in items)),
            "rationale": rationale,
            "assertions": [
                {"pair_id": p["id"], "field": field, "value": p[field]}
                for p in items
                for field in (
                    "line_of_sight_support",
                    "physical_viewability",
                    "distance_adjusted_viewability",
                )
            ],
        }
        for ident, items, rationale in lesson_specs
    ]
    ids = sorted({p[k] for p in pairs for k in ("source_h3", "target_h3")})
    sources = {
        role: {p["source_h3"] for p in pairs if p["source_type"] == role}
        for role in ("land", "water")
    }
    targets = {p["target_h3"] for p in pairs}
    cells = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": cell,
                "properties": {
                    "h3": cell,
                    "roles": [role for role in sources if cell in sources[role]],
                    "target": cell in targets,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon, lat]
                            for lat, lon in [
                                *h3.cell_to_boundary(cell),
                                h3.cell_to_boundary(cell)[0],
                            ]
                        ]
                    ],
                },
            }
            for cell in ids
        ],
    }
    observers, active = [], []
    projected_observers = {}
    to_projected = Transformer.from_crs(4326, app.viewshed.crs_projected, always_xy=True)
    to_wgs84 = Transformer.from_crs(app.viewshed.crs_projected, 4326, always_xy=True)
    for role in ("land", "water"):
        variant = apply_source_type_policy(app, role)
        cells_frame = _load_terrain_source_cells(variant)
        samples, _ = prepare_source_samples(variant, cells_frame, sorted(sources[role]))
        for row in samples.to_dict("records"):
            ident = f"{role}:{row['source_h3_cell']}:{row['sample_index']}"
            point = row["geometry"]
            observers.append(
                {
                    "type": "Feature",
                    "id": ident,
                    "properties": {
                        "source_h3": row["source_h3_cell"],
                        "source_type": role,
                        "sample_index": row["sample_index"],
                    },
                    "geometry": mapping(point),
                }
            )
            projected_observers.setdefault(role + ":" + row["source_h3_cell"], []).append(
                (ident, transform(to_projected.transform, point))
            )
        for row in cells_frame.to_dict("records"):
            if row["h3_cell"] in sources[role]:
                geom = row.get(
                    "land_geometry" if role == "land" else "water_geometry", row["geometry"]
                )
                active.append(
                    {
                        "type": "Feature",
                        "id": role + ":" + row["h3_cell"],
                        "properties": {"source_type": role, "source_h3": row["h3_cell"]},
                        "geometry": mapping(geom),
                    }
                )
    water_domain = _water_terrain_domains_for_app(app).water_domain
    stack = ensure_canonical_raster_stack(app, include_canopy=True)
    # Prepared, aligned surfaces and actual water pixel centers; no synthetic profile terrain.
    with (
        rasterio.open(stack.endpoint_dem_path) as dem,
        rasterio.open(stack.water_mask_path) as water,
        rasterio.open(stack.aligned_canopy_path) as chm,
    ):
        ground = dem.read(1, masked=True)
        trees = chm.read(1, masked=True)
        water_values = water.read(1) == 1
        support = []
        target_point = {}
        for cell in sorted({p["target_h3"] for p in selected}):
            polygon = transform(
                to_projected.transform,
                shape(next(f["geometry"] for f in cells["features"] if f["id"] == cell)),
            )
            minx, miny, maxx, maxy = polygon.bounds
            r0, c0 = dem.index(minx, maxy)
            r1, c1 = dem.index(maxx, miny)
            points = []
            for row in range(max(0, r0 - 1), min(dem.height, r1 + 2)):
                for col in range(max(0, c0 - 1), min(dem.width, c1 + 2)):
                    x, y = dem.xy(row, col)
                    point = Point(x, y)
                    if water_values[row, col] and polygon.covers(point):
                        points.append(point)
                        lon, lat = to_wgs84.transform(x, y)
                        support.append(
                            {
                                "type": "Feature",
                                "id": f"land:{cell}:{row}:{col}",
                                "properties": {
                                    "target_h3": cell,
                                    "source_type": "land",
                                    "kind": "raster_water_pixel_center",
                                    "pixel_area_m2": abs(dem.transform.a * dem.transform.e),
                                },
                                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                            }
                        )
            if points:
                target_point[cell] = points[len(points) // 2]
            for i, point in enumerate(
                _projected_water_target_samples_for_app(
                    apply_source_type_policy(app, "water"),
                    cell,
                    water_domain=water_domain,
                    max_samples=app.raw_config["water_viewing"]["land_mask"][
                        "target_samples_per_cell"
                    ],
                )
            ):
                lon, lat = to_wgs84.transform(point.x, point.y)
                support.append(
                    {
                        "type": "Feature",
                        "id": f"water:{cell}:{i}",
                        "properties": {
                            "target_h3": cell,
                            "source_type": "water",
                            "kind": "opaque_land_mask_target_sample",
                        },
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    }
                )
        profiles = []
        for pair in selected:
            observer_id, observer = projected_observers["land:" + pair["source_h3"]][0]
            endpoint = target_point.get(pair["target_h3"])
            if endpoint is None:
                continue
            distance = observer.distance(endpoint)
            count = max(2, math.ceil(distance / 50) + 1)
            coords = [
                (
                    observer.x + (endpoint.x - observer.x) * t,
                    observer.y + (endpoint.y - observer.y) * t,
                )
                for t in np.linspace(0, 1, count)
            ]
            with tempfile.TemporaryDirectory() as temp:
                surface = isolate_observer_canopy_surface(
                    base_surface_path=stack.base_canopy_surface_path,
                    endpoint_dem_path=stack.endpoint_dem_path,
                    output_path=Path(temp) / "surface.tif",
                    observer_x=observer.x,
                    observer_y=observer.y,
                    clearance_radius_m=app.viewshed.observer_canopy_clearance_radius_m,
                )
                with rasterio.open(surface) as canopy:
                    z_ground = [float(v[0]) for v in dem.sample(coords)]
                    z_surface = [float(v[0]) for v in canopy.sample(coords)]
            eye, end = (
                z_ground[0] + app.viewshed.observer_eye_height_m,
                z_ground[-1] + app.viewshed.target_height_m,
            )
            samples = [
                {
                    "distance_m": distance * i / (count - 1),
                    "ground_m": a,
                    "canopy_surface_m": b,
                    "ray_m": eye
                    + (end - eye) * i / (count - 1)
                    - app.viewshed.curvature_coefficient
                    * (distance * i / (count - 1))
                    * (distance - distance * i / (count - 1))
                    / (2 * app.viewshed.earth_radius_m),
                }
                for i, (a, b) in enumerate(zip(z_ground, z_surface, strict=True))
            ]
            profiles.append(
                {
                    "pair_id": pair["id"],
                    "observer_id": observer_id,
                    "endpoint": list(to_wgs84.transform(endpoint.x, endpoint.y)),
                    "kind": "explanatory_sampled_profile_not_engine_or_cell_diagnostic",
                    "units": "m above modeled sea-level datum",
                    "analysis_resolution_m": app.viewshed.dem_resolution_m,
                    "display_step_m": 50,
                    "samples": samples,
                    "ground_intersects_ray": any(s["ground_m"] > s["ray_m"] for s in samples[1:-1]),
                }
            )
        stride = 4
        grid = {
            "crs": str(dem.crs),
            "affine": list(dem.transform)[:6],
            "source_shape": list(ground.shape),
            "display_stride": stride,
            "analysis_resolution_m": app.viewshed.dem_resolution_m,
            "display_resolution_m": app.viewshed.dem_resolution_m * stride,
            "units": "m",
            "ground": [
                [None if np.ma.is_masked(v) else float(v) for v in row]
                for row in ground[::stride, ::stride]
            ],
            "canopy_height": [
                [None if np.ma.is_masked(v) else float(v) for v in row]
                for row in trees[::stride, ::stride]
            ],
            "water": water_values[::stride, ::stride].astype(int).tolist(),
        }
    indexes = {direction: {} for direction in ("forward", "inverse")}
    for pair in pairs:
        for direction, field in (("forward", "source_h3"), ("inverse", "target_h3")):
            indexes[direction].setdefault(pair["source_type"] + ":" + pair[field], []).append(
                pair["id"]
            )
    output.mkdir(parents=True, exist_ok=True)
    for name, data in {
        "pairs.json": {
            "columns": [
                "id",
                "scenario",
                "source_type",
                "source_h3",
                "target_h3",
                "distance_km",
                "canopy_distance_weighted_los_support",
                *METRICS,
                *[key for key in pairs[0] if key.endswith("_state")],
            ],
            "row_trace": {
                "key_columns": ["source_type", "source_h3", "target_h3"],
                "artifact_files": {
                    role: outputs[role + "_observation_geometry"].name for role in ("land", "water")
                },
                "raw_canopy_kernel_artifact": paths.dual_surface_factors.name,
            },
            "rows": [
                [
                    p[key]
                    for key in [
                        "id",
                        "scenario",
                        "source_type",
                        "source_h3",
                        "target_h3",
                        "distance_km",
                        "canopy_distance_weighted_los_support",
                        *METRICS,
                        *[key for key in pairs[0] if key.endswith("_state")],
                    ]
                ]
                for p in pairs
            ],
        },
        "cells.geojson": cells,
        "observer-samples.geojson": {"type": "FeatureCollection", "features": observers},
        "active-source-geometry.geojson": {"type": "FeatureCollection", "features": active},
        "target-support.geojson": {"type": "FeatureCollection", "features": support},
        "coverage.json": receipt["input_coverage"],
        "profiles.json": profiles,
        "lessons.json": lessons,
        "indexes.json": indexes,
        "inputs/display-grid.json": grid,
        "validation.json": {
            "artifact_rows_validated": len(pairs),
            "four_file_receipt_validated": True,
            "compact_geometry_parity_tolerance": 1e-6,
            "profile_status": "explanatory, not engine diagnostics",
            "robustness": "sampling/resolution/clearance sensitivity not yet run",
        },
    }.items():
        write(output / name, data)
    land = gpd.read_file(app.paths.land_polygon_path, bbox=tuple(bbox_from_config(app.raw_config)))
    bounds = [-123.30, 48.39, -122.80, 48.71]
    land.geometry = land.geometry.make_valid().intersection(box(*bounds))
    write(output / "inputs/coast.geojson", json.loads(land[["geometry"]].to_json()))
    # Publication derivatives retain source rights; raw regional data stay ignored.
    manifest = {
        "export_contract": VERSION,
        "method": receipt["method"],
        "generation_id": receipt["generation_id"],
        "config_hash": receipt["scientific_config_hash"],
        "source_hashes": receipt["source_hashes"],
        "prepared_input_sha256": {
            "ground": byte_checksum(app.paths.regional_dem_path),
            "canopy": byte_checksum(app.paths.canopy_height_path),
        },
        "artifacts": receipt["files"],
        "code_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_vintages": [
            {key: record.get(key) for key in ("source_year", "source_version", "observed_sha256")}
            for record in read(paths.final_output_dir / "components/inputs/chm/download.json")[
                "assets"
            ]
        ],
        "crs": app.viewshed.crs_projected,
        "native_resolution_m": {"dem": 30, "canopy": 10},
        "analysis_resolution_m": app.viewshed.dem_resolution_m,
        "assumptions": {
            key: app.raw_config[key]
            for key in (
                "viewshed",
                "h3",
                "water_viewing",
                "distance_weight",
                "source_target_lookup",
            )
        },
        "target_support_scope": "Actual support for curated lesson targets only; all candidate pair records are retained",
        "candidate_universe": {
            "extent": "configured source bbox plus 6 km target buffer, 5 km centroid candidate cutoff",
            "bbox_wgs84": bounds,
            "pairs": len(pairs),
            "cells": len(ids),
        },
        "defaults": {
            "pair_id": reduced["id"],
            "source_type": "land",
            "mixed_role_pair_id": mixed["id"] if mixed else None,
            "scenario": "baseline",
        },
        "rights": [
            {
                "source": "USGS 3DEP",
                "license": "public domain",
                "url": "https://www.usgs.gov/3d-elevation-program",
            },
            {
                "source": "ETH Global Canopy Height 2020; Lang, Jetz, Schindler and Wegner (2023)",
                "license": "CC BY 4.0",
                "url": "https://langnico.github.io/globalcanopyheight/",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "processing": "bounded COG windows, max resampling, observer-specific clearance, LOS and derived display grids",
            },
            {
                "source": "Natural Earth 1:10m land",
                "license": "public domain",
                "url": "https://www.naturalearthdata.com/about/terms-of-use/",
            },
        ],
        "limitations": [
            "Static physical support; not sighting probability or actual observer activity",
            "100 m analysis and generalized coastline; not fine shoreline accuracy",
            "Missing canopy uses zero height, not observed absence of trees",
            "Profiles sample real surfaces but are not engine rays or cell aggregate diagnostics",
            "Curated cases are not regional statistics",
        ],
        "files": {
            str(p.relative_to(output)): digest(p.read_bytes())
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "manifest.json"
        },
    }
    manifest["bundle_id"] = digest(
        encode({key: manifest[key] for key in ("export_contract", "generation_id", "files")})
    )
    write(output / "manifest.json", manifest)
    result = check(output)
    print(json.dumps({**result, "export_seconds": round(time.monotonic() - start, 2)}))


def export(config, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".lesson-export-") as temporary:
        stage = Path(temporary) / "bundle"
        _export(config, stage)
        check(stage)
        backup = Path(temporary) / "previous"
        if output.exists():
            output.replace(backup)
        try:
            stage.replace(output)
        except Exception:
            if backup.exists():
                backup.replace(output)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/san_juan_demo.yaml"))
    parser.add_argument("--output", type=Path, default=Path("docs/assets/examples/san-juan"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(args.output)))
    else:
        export(args.config, args.output)


if __name__ == "__main__":
    main()
