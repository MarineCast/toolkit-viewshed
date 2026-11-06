"""Aggregate all delivered land pairs by water target, without rerunning the model."""

from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path

import geopandas as gpd
import h3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely.geometry import box, mapping

from build_documentation_examples import encode
from export_lime_kiln_regional import checksum
from documentation_geometry import projected_viewport

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/examples/region-overview"


def aggregate(table):
    """Maximum over available candidates; missing values never become zero."""
    if not {"source_h3", "target_h3", "weight_static_viewability"} <= set(table.column_names):
        raise ValueError("Missing aggregate inputs")
    for key in ("source_h3", "target_h3"):
        if table[key].null_count:
            raise ValueError("Null pair key")
    values = table["weight_static_viewability"]
    for value in pc.drop_null(values).to_numpy():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid combined weight")
    if (
        table.select(["source_h3", "target_h3"])
        .group_by(["source_h3", "target_h3"])
        .aggregate([])
        .num_rows
        != table.num_rows
    ):
        raise ValueError("Duplicate source-target pair")
    # This table is role-partitioned by the checked land delivery; do not pool roles.
    table = table.append_column("candidate", pa.array([1] * len(table), type=pa.int8()))
    grouped = table.group_by("target_h3").aggregate(
        [
            ("weight_static_viewability", "max"),
            ("weight_static_viewability", "count"),
            ("candidate", "sum"),
        ]
    )
    result = []
    for row in grouped.to_pylist():
        count = row["candidate_sum"]
        valid = row["weight_static_viewability_count"]
        result.append(
            {
                "target_h3": row["target_h3"],
                "maximum_valid_combined_weight": row["weight_static_viewability_max"],
                "candidate_count": count,
                "valid_count": valid,
                "missing_count": count - valid,
                "state": "unavailable" if not valid else "partial" if valid < count else "complete",
            }
        )
    return sorted(result, key=lambda r: r["target_h3"])


def export(delivery):
    receipt = json.loads((delivery / "regional-delivery.json").read_text())
    role = receipt["roles"]["land"]
    name = role["static"]
    if checksum(delivery / name) != receipt["files"][name]["sha256"]:
        raise ValueError("Delivery checksum mismatch")
    table = pq.read_table(
        delivery / name, columns=["source_h3", "target_h3", "weight_static_viewability"]
    )
    if (
        len(table) != role["rows"]
        or pc.count_distinct(table["source_h3"]).as_py() != role["sources"]
    ):
        raise ValueError("Source population mismatch")
    rows = aggregate(table)
    if len(rows) != role["targets"]:
        raise ValueError("Target population mismatch")
    project = Transformer.from_crs(4326, 32610, always_xy=True)
    polygons = []
    for row in rows:
        if h3.get_resolution(row["target_h3"]) != 7:
            raise ValueError("Expected native R7 target")
        polygons.append(
            {
                "id": row["target_h3"],
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            project.transform(lon, lat)
                            for lat, lon in h3.cell_to_boundary(row["target_h3"])
                        ]
                    ],
                },
            }
        )
    points = [xy for f in polygons for xy in f["geometry"]["coordinates"][0]]
    bounds = [
        min(p[0] for p in points) - 15000,
        min(p[1] for p in points) - 15000,
        max(p[0] for p in points) + 15000,
        max(p[1] for p in points) + 15000,
    ]
    meta_path = delivery / role["metadata"]
    if checksum(meta_path) != receipt["files"][role["metadata"]]["sha256"]:
        raise ValueError("Metadata checksum mismatch")
    meta = json.loads(meta_path.read_text())
    coast_path = Path(meta["config"]["paths"]["land_polygon_path"])
    # Broad regional context, with padding for the map's aspect ratio.
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    extents = []
    for width, height in ((960, 920), (480, 670)):
        _, scale = projected_viewport(bounds, width, height, padding=35)
        extents.append((width / (2 * scale) + 10000, height / (2 * scale) + 10000))
    dx, dy = max(p[0] for p in extents), max(p[1] for p in extents)
    context = [cx - dx, cy - dy, cx + dx, cy + dy]
    bbox = Transformer.from_crs(32610, 4326, always_xy=True).transform_bounds(*context)
    coast = gpd.read_file(coast_path, bbox=bbox).to_crs(4326)
    coast.geometry = coast.geometry.make_valid().intersection(box(*bbox))
    coast = coast.to_crs(32610).geometry.union_all().simplify(250, preserve_topology=True)
    data = {
        "contract": "regional_land_maximum_by_target_v1",
        "source_type": "land",
        "aggregation": "maximum valid combined weight per target across all delivered land candidates; not sum, average, occurrence or effort",
        "candidate_pair_count": len(table),
        "source_count": role["sources"],
        "target_count": len(rows),
        "scientific_config_hash": meta["scientific_config_hash"],
        "source_sha256": {
            name: receipt["files"][name]["sha256"],
            role["metadata"]: checksum(meta_path),
        },
        "delivery_receipt_sha256": checksum(delivery / "regional-delivery.json"),
        "rows": rows,
    }
    geometry = {
        "crs": "EPSG:32610",
        "bounds": bounds,
        "cells": polygons,
        "coast": mapping(coast),
        "coast_purpose": "Natural Earth public domain; generalized context simplified to 250 m, not LOS water mask",
        "coast_sha256": {
            p.suffix: checksum(p)
            for p in sorted(coast_path.parent.glob(coast_path.stem + ".*"))
            if p.is_file()
        },
    }
    files = {"targets.json": encode(data), "geometry.json": encode(geometry)}
    files["source.json"] = encode(
        {
            "contract": data["contract"],
            "payload_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
            "source_sha256": data["source_sha256"],
            "checked": "2026-11-06",
            "rights_reference": "../../regional-viewshed-example/#inputs-and-interpretation",
        }
    )
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = export(args.delivery)
    if not args.check:
        OUTPUT.mkdir(exist_ok=True)
    for name, data in files.items():
        if args.check:
            if (OUTPUT / name).read_bytes() != data:
                raise ValueError("Regional overview differs from delivery")
        else:
            (OUTPUT / name).write_bytes(data)
    print(
        "Full regional land aggregation verified"
        if args.check
        else "Full regional land aggregation exported"
    )
