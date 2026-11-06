"""Extract one land source from validated regional delivery; no model or acquisition runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import geopandas as gpd
import h3
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box, mapping
from shapely.ops import transform
from shapely.wkb import loads

from build_documentation_examples import encode
from documentation_geometry import projected_viewport

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/examples/lime-kiln"
SOURCE = "8728d1061ffffff"
FIELDS = {
    "weight_distance": "distance_detection_weight",
    "weight_terrain": "distance_weighted_los_support",
    "weight_vegetation": "vegetation_attenuation",
    "weight_static_viewability": "distance_adjusted_viewability",
}


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export(delivery, observer_validation, source=SOURCE):
    receipt = json.loads((delivery / "regional-delivery.json").read_text())
    if receipt["contract"] != "regional_static_viewshed_v1" or receipt["h3_resolution"] != 7:
        raise ValueError("Unexpected regional delivery contract")
    names = receipt["roles"]["land"]
    hashes = {}
    for kind in ("static", "observation", "metadata"):
        name = names[kind]
        hashes[name] = checksum(delivery / name)
        if hashes[name] != receipt["files"][name]["sha256"]:
            raise ValueError(f"Regional delivery checksum mismatch: {name}")
    meta = json.loads((delivery / names["metadata"]).read_text())
    settings = meta["physical_assumptions"]
    if settings != meta["dem"] or settings["source_type"] != "land":
        raise ValueError("Inconsistent recorded assumptions")
    compact = pq.read_table(
        delivery / names["static"], filters=[("source_h3", "=", source)]
    ).to_pylist()
    observations = pq.read_table(
        delivery / names["observation"], filters=[("source_h3", "=", source)]
    ).to_pylist()
    tables = []
    for rows in (compact, observations):
        indexed = {r["target_h3"]: r for r in rows}
        if not rows or len(indexed) != len(rows):
            raise ValueError("Empty or duplicate source pairs")
        tables.append(indexed)
    if tables[0].keys() != tables[1].keys():
        raise ValueError("Static/observation candidate mismatch")
    for target, row in tables[1].items():
        if row["source_type"] != "land" or row["CONFIG_HASH"] != meta["scientific_config_hash"]:
            raise ValueError("Role/configuration mismatch")
        for field, diagnostic in FIELDS.items():
            value = row[diagnostic]
            if value is None or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Invalid regional weight")
            if tables[0][target][field] != value:
                raise ValueError("Static/observation weight mismatch")
        if not math.isclose(
            row["distance_adjusted_viewability"],
            row["distance_weighted_los_support"] * row["vegetation_attenuation"],
            abs_tol=1e-7,
        ):
            raise ValueError("Static composition mismatch")
    common = {}
    for key in observations[0]:
        if key.isupper() or key == "legacy_static_schema":
            values = {json.dumps(r[key], sort_keys=True) for r in observations}
            if len(values) != 1:
                raise ValueError(f"Mixed lineage/coverage: {key}")
            common[key] = observations[0][key]
    rows = [
        {k: v for k, v in row.items() if k not in common}
        for row in sorted(observations, key=lambda r: r["target_h3"])
    ]
    source_path = Path(meta["config"]["paths"]["source_cells_path"])
    run = source_path.parent
    observers_path = run / "land-observers.parquet"
    validation = json.loads(observer_validation.read_text())
    observer_hash = checksum(observers_path)
    if observer_hash != validation["sha256"]:
        raise ValueError("Retained observer checksum mismatch")
    source_rows = pq.read_table(source_path, filters=[("h3_cell", "=", source)]).to_pylist()
    if len(source_rows) != 1:
        raise ValueError("Expected exactly one source geometry")
    land = loads(source_rows[0]["land_geometry"])
    source_geo = json.loads(pq.ParquetFile(source_path).schema_arrow.metadata[b"geo"])
    land_crs = source_geo["columns"]["land_geometry"]["crs"]
    project = Transformer.from_crs(4326, 32610, always_xy=True).transform
    source_project = Transformer.from_crs(land_crs, 32610, always_xy=True).transform
    land_projected = transform(source_project, land)
    observers = pq.read_table(observers_path, filters=[("source_h3_cell", "=", source)]).to_pylist()
    if not observers or any(r["sample_points_actual"] != len(observers) for r in observers):
        raise ValueError("Observer population mismatch")
    features = []
    for r in sorted(observers, key=lambda r: r["sample_index"]):
        point = Point(project(r["lon"], r["lat"]))
        if not land_projected.buffer(0.01).covers(point):
            raise ValueError("Observer outside retained active land")
        features.append(
            {
                "type": "Feature",
                "id": r["sample_id"],
                "properties": {
                    "source_h3": source,
                    "source_type": "land",
                    "sample_index": r["sample_index"],
                },
                "geometry": mapping(point),
            }
        )
    cells = []
    for cell in sorted({source} | set(tables[0])):
        if h3.get_resolution(cell) != 7:
            raise ValueError("Unexpected cell resolution")
        poly = Polygon([project(lon, lat) for lat, lon in h3.cell_to_boundary(cell)])
        cells.append(
            {"type": "Feature", "id": cell, "properties": {"h3": cell}, "geometry": mapping(poly)}
        )
    points = [xy for f in cells for xy in f["geometry"]["coordinates"][0]]
    bounds = [
        min(p[0] for p in points) - 1800,
        min(p[1] for p in points) - 1800,
        max(p[0] for p in points) + 1800,
        max(p[1] for p in points) + 1800,
    ]
    coast_path = Path(meta["config"]["paths"]["land_polygon_path"])
    inverse = Transformer.from_crs(32610, 4326, always_xy=True)
    # Cover the full desktop/mobile viewport, including aspect-ratio padding.
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    extents = []
    for width, height in ((960, 520), (480, 400)):
        _, scale = projected_viewport(bounds, width, height, padding=24)
        extents.append((width / (2 * scale) + 1000, height / (2 * scale) + 1000))
    dx, dy = max(e[0] for e in extents), max(e[1] for e in extents)
    context_bounds = [cx - dx, cy - dy, cx + dx, cy + dy]
    bbox = inverse.transform_bounds(*context_bounds)
    coast = gpd.read_file(coast_path, bbox=bbox).to_crs(4326)
    coast.geometry = coast.geometry.make_valid().intersection(box(*bbox))
    coast = coast.to_crs(32610)
    coast_geometry = (
        coast.geometry.union_all()
        .intersection(box(*context_bounds))
        .simplify(30, preserve_topology=True)
    )
    center_lat, center_lon = h3.cell_to_latlng(source)
    geometry = {
        "crs": "EPSG:32610",
        "bounds": bounds,
        "source_centroid": list(project(center_lon, center_lat)),
        "cells": {"features": cells},
        "coast": {
            "features": [{"type": "Feature", "geometry": mapping(coast_geometry), "properties": {}}]
        },
        "active_sources": {
            "features": [
                {
                    "type": "Feature",
                    "id": "land:" + source,
                    "geometry": mapping(land_projected),
                    "properties": {"source_type": "land"},
                }
            ]
        },
        "observers": {"features": features},
        "coast_purpose": "Generalized Natural Earth context, clipped and simplified to 30 m for display; not the regional LOS water mask",
    }
    pairs = {
        "source_h3": source,
        "source_type": "land",
        "generation_id": common["GENERATION_ID"],
        "common_metadata": common,
        "rows": rows,
    }
    files = {"source-pairs.json": encode(pairs), "geometry.json": encode(geometry)}
    evidence = {
        "contract": (
            "lime_kiln_regional_excerpt_v1" if source == SOURCE else "regional_source_excerpt_v1"
        ),
        "study_id": receipt["study_id"],
        "validated_at_utc": receipt["validated_at_utc"],
        "generation_id": common["GENERATION_ID"],
        "scientific_config_hash": meta["scientific_config_hash"],
        "source_h3": source,
        "pair_count": len(rows),
        "observer_count": len(observers),
        "physical_assumptions": settings,
        "source_sha256": hashes,
        "delivery_receipt_sha256": checksum(delivery / "regional-delivery.json"),
        "observer_sha256": observer_hash,
        "source_geometry_sha256": checksum(source_path),
        "coast_sha256": {
            p.suffix: checksum(p)
            for p in sorted(coast_path.parent.glob(coast_path.stem + ".*"))
            if p.is_file()
        },
        "coverage": meta["coverage"],
        "vintage_note": "Mixed ETH 2020 and WRI/Meta v6 dated imagery. Recorded canopy_product_year=2020 alone does not describe the composite.",
        "rights": {
            "terrain": "USGS public domain; NRCan Open Government Licence – Canada",
            "canopy": "ETH and WRI/Meta CC BY 4.0; credit Lang, Jetz, Schindler and Wegner (2023), WRI/Meta and Maxar imagery",
            "context": "Natural Earth public domain; generalized context only",
            "water_and_source_support": "Nominal Natural Earth, GSHHG (LGPL), USGS NHD and BC Freshwater Atlas; see regional example source attribution",
        },
        "payload_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
    }
    files["regional-source.json"] = encode(evidence)
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--observer-validation", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if not args.check:
        args.output.mkdir(parents=True, exist_ok=True)
    for name, value in export(args.delivery, args.observer_validation, args.source).items():
        if args.check:
            if (args.output / name).read_bytes() != value:
                raise ValueError(f"Stale regional excerpt: {name}")
        else:
            (args.output / name).write_bytes(value)
    print(
        f"Regional source {args.source} excerpt verified"
        if args.check
        else f"Regional source {args.source} excerpt exported"
    )
