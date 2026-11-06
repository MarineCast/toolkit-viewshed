"""Export a bounded MkDocs example from checksum-verified regional Parquets."""

import argparse
import hashlib
import json
import math
from bisect import bisect_left
from pathlib import Path
from statistics import fmean

import h3
import pyarrow.parquet as pq
from pyproj import Transformer

TARGETS = ("8728d1061ffffff", "8728d1064ffffff", "8728d1a84ffffff", "8728d1b9bffffff")
BINS = (0, 0.01, 0.05, 0.20, 0.50, 1)
STATIC = "weight_static_viewability"
COLUMNS = ("source_h3", STATIC, "weight_terrain", "weight_vegetation", "weight_distance")
OUTPUT = Path(__file__).resolve().parents[1] / "docs/assets/examples/regional/targets.json"


def export(delivery: Path) -> dict:
    manifest = json.loads((delivery / "regional-delivery.json").read_text())
    if (
        manifest["contract"] != "regional_static_viewshed_v1"
        or manifest["study_id"] != "vancouver-island-columbia"
        or manifest["h3_resolution"] != 7
    ):
        raise ValueError("Unexpected regional delivery")
    project = Transformer.from_crs(4326, 32610, always_xy=True)
    latitude, longitude = h3.cell_to_latlng(TARGETS[0])
    east, north = project.transform(longitude, latitude)

    def outline(cell: str) -> str:
        if not h3.is_valid_cell(cell) or h3.get_resolution(cell) != 7:
            raise ValueError(f"Expected an H3 R7 cell: {cell}")
        points = [project.transform(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]
        return "M" + " ".join(f"{(x-east)/100:.1f},{(north-y)/100:.1f}" for x, y in points) + "z"

    targets = {cell: {"outline": outline(cell), "roles": {}} for cell in TARGETS}
    hashes = {}
    for role in ("land", "water"):
        name = manifest["roles"][role]["static"]
        with (delivery / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != manifest["files"][name]["sha256"]:
            raise ValueError(f"Delivery checksum mismatch: {name}")
        hashes[role] = digest
        table = pq.read_table(
            delivery / name,
            columns=["target_h3", *COLUMNS],
            filters=[("target_h3", "in", list(TARGETS))],
        )
        records = table.to_pylist()
        for cell, entry in targets.items():
            rows = sorted(
                (row for row in records if row["target_h3"] == cell),
                key=lambda row: (-row[STATIC], row["source_h3"]),
            )
            sources = {row["source_h3"] for row in rows}
            if not rows or len(sources) != len(rows):
                raise ValueError(f"Missing or duplicate candidate rows for {role} {cell}")
            paths = [[] for _ in BINS]
            for row in rows:
                if not all(math.isfinite(row[key]) and 0 <= row[key] <= 1 for key in COLUMNS[1:]):
                    raise ValueError(f"Invalid support value for {role} {cell}")
                if role == "water" and row["weight_vegetation"] != 1:
                    raise ValueError(f"Expected neutral water vegetation encoding for {cell}")
                if not math.isclose(
                    row[STATIC],
                    row["weight_terrain"] * row["weight_vegetation"],
                    abs_tol=1e-7,
                ):
                    raise ValueError(f"Static composition mismatch for {role} {cell}")
                paths[bisect_left(BINS, row[STATIC])].append(outline(row["source_h3"]))
            entry["roles"][role] = {
                "paths": ["".join(group) for group in paths],
                "count": len(rows),
                "positive": sum(row[STATIC] > 0 for row in rows),
                "mean": fmean(row[STATIC] for row in rows),
                "strongest": [
                    [
                        row[key] if role == "land" or key != "weight_vegetation" else None
                        for key in COLUMNS
                    ]
                    for row in rows[:5]
                ],
            }

    return {
        "schema": "regional_viewshed_example_v1",
        "study_id": manifest["study_id"],
        "validated_at_utc": manifest["validated_at_utc"],
        "source_sha256": hashes,
        "crs": "EPSG:32610",
        "target_h3_resolution": 7,
        "maximum_distance_m": manifest["max_distance_m"],
        "bin_upper_bounds": BINS,
        "source_columns": COLUMNS,
        "targets": targets,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(export(args.delivery), separators=(",", ":")) + "\n"
    if args.check:
        if OUTPUT.read_text() != rendered:
            raise SystemExit("Regional example differs from the validated delivery.")
    else:
        OUTPUT.write_text(rendered)
