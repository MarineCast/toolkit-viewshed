"""Validation and completion receipts for versioned transmission products."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import h3
import polars as pl

from ..config.transmission import METHOD
from ..contracts.transmission import (
    TRANSMISSION_CONTRACT,
    digest,
    payload_hash,
    prepared_identity,
)


def validate_pairs(table: Any, source_cells: Any) -> Any:
    keys = ["source_type", "source_h3", "target_h3"]
    required = set(keys) | {
        "lower",
        "upper",
        "average_viewability",
        "input_unknown_fraction",
        "out_of_range_fraction",
        "target_water_pixel_count",
        "sampled_target_points",
        "observer_count",
        "weight_terrain",
        "weight_distance",
        "distance_km",
        "above_ground_lower",
        "above_ground_upper",
        "weight_above_ground",
        "above_ground_status",
        "support_status",
        "target_support",
        "method",
    }
    if required - set(table.columns) or (not table.height and source_cells):
        raise ValueError("Missing or empty transmission pair support")
    if (
        len(source_cells) != len(set(source_cells))
        or table.select(keys).null_count().row(0) != (0, 0, 0)
        or table.unique(keys).height != table.height
    ):
        raise ValueError("Invalid pair identity")
    if set(table["source_h3"]) != set(source_cells):
        raise ValueError("Missing or unexpected source partitions")
    for row in table.iter_rows(named=True):
        if (
            row["source_type"] != "land"
            or row["method"] != METHOD
            or row["target_support"] != "partial_h3_in_observer_reach"
        ):
            raise ValueError("Unexpected transmission method/role/support")
        for key in ("source_h3", "target_h3"):
            cell = row[key]
            if not h3.is_valid_cell(cell) or cell != cell.lower() or h3.get_resolution(cell) != 7:
                raise ValueError("Invalid canonical R7 identity")
        for key in ("target_water_pixel_count", "sampled_target_points", "observer_count"):
            if type(row[key]) is not int or row[key] <= 0:
                raise ValueError("Actual positive integer support required")
        if row["sampled_target_points"] > row["target_water_pixel_count"]:
            raise ValueError("Sampled population exceeds census")
        for key in (
            "lower",
            "upper",
            "input_unknown_fraction",
            "out_of_range_fraction",
            "weight_terrain",
            "weight_distance",
        ):
            v = row[key]
            if v is None or isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1:
                raise ValueError("Invalid finite bounded support")
        if not math.isfinite(row["distance_km"]) or row["distance_km"] < 0:
            raise ValueError("Invalid centroid distance")
        unknown = row["input_unknown_fraction"] > 0
        if (
            row["lower"] > row["upper"]
            or not math.isclose(
                row["upper"] - row["lower"], row["input_unknown_fraction"], abs_tol=1e-12
            )
            or ((row["average_viewability"] is None) != unknown)
            or (not unknown and row["average_viewability"] != row["lower"])
            or row["support_status"] != ("unknown" if unknown else "known")
        ):
            raise ValueError("Invalid bounds or hidden unknown support")
        factor = [
            row[k] for k in ("above_ground_lower", "above_ground_upper", "weight_above_ground")
        ]
        state = row["above_ground_status"]
        if row["weight_terrain"] == 0:
            if state != "not_applicable" or factor != [None, None, None]:
                raise ValueError("Zero-baseline factor must remain not applicable")
        else:
            lo, hi, complete = factor
            if (
                state not in {"known", "unknown"}
                or lo is None
                or hi is None
                or not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi <= 1)
            ):
                raise ValueError("Invalid conditional factor bounds")
            if (complete is None) != (state == "unknown") or (
                state == "known" and (complete != lo or hi != lo)
            ):
                raise ValueError("Invalid conditional factor missingness")


def validate_transmission_product(directory: Any) -> dict[str, Any]:
    """Read-only validation; incomplete generations and modified artifacts fail."""
    root = Path(directory)
    manifest: dict[str, Any] = json.loads((root / "manifest.json").read_text())
    if (
        manifest.get("contract") != TRANSMISSION_CONTRACT
        or manifest.get("status") != "complete_scenario"
    ):
        raise ValueError("A complete supported transmission generation is required")
    effective = json.loads((root / "effective-config.json").read_text())
    if payload_hash(effective) != manifest["config_hash"] or effective["method"] != METHOD:
        raise ValueError("Effective configuration changed")
    if prepared_identity(root) != manifest["prepared_hashes"]:
        raise ValueError("Prepared transmission inputs changed")
    for name, sha in manifest["products"].items():
        if digest(root / name) != sha:
            raise ValueError(f"Transmission product changed: {name}")
    sources = json.loads((root / "source_cells.json").read_text())
    table = pl.read_parquet(root / "average_viewability.parquet")
    infos = [json.loads((root / f"partitions/{s['source_h3']}.json").read_text()) for s in sources]
    validate_pairs(
        table,
        [
            s["source_h3"]
            for s, info in zip(sources, infos, strict=True)
            if info["source_state"] == "complete"
        ],
    )
    for source, info in zip(sources, infos, strict=True):
        if info["source_h3"] != source["source_h3"] or info["source_state"] not in {
            "complete",
            "no_target_population",
        }:
            raise ValueError("Invalid source completion")
        count = table.filter(pl.col("source_h3") == source["source_h3"]).height
        if count != info["target_cells"] or (
            info["source_state"] == "no_target_population" and count != 0
        ):
            raise ValueError("Source population/completion mismatch")
    if table.height != manifest["pair_count"] or len(sources) != manifest["source_count"]:
        raise ValueError("Transmission coverage count mismatch")
    return manifest


def validate_legacy_pair_rows(table: Any, source_cells: Any) -> Any:
    """Validate coverage, identity and lower/upper missingness before combining."""
    keys = ["source_type", "source_h3", "target_h3"]
    if table.select(keys).null_count().row(0) != (0, 0, 0) or (
        table.unique(keys).height != table.height
    ):
        raise ValueError("Invalid pair identity")
    if set(table["source_h3"]) != set(source_cells):
        raise ValueError("Missing or unexpected source partitions")
    for row in table.iter_rows(named=True):
        if (
            row["source_type"] != "land"
            or h3.get_resolution(row["source_h3"]) != 7
            or h3.get_resolution(row["target_h3"]) != 7
            or not 0 <= row["lower"] <= row["upper"] <= 1
            or not math.isclose(
                row["upper"] - row["lower"], row["input_unknown_fraction"], abs_tol=1e-12
            )
            or (row["average_viewability"] is None) != (row["input_unknown_fraction"] > 0)
            or (
                row["average_viewability"] is not None
                and row["average_viewability"] != row["lower"]
            )
            or not 0 <= row["out_of_range_fraction"] <= 1
            or row["target_water_pixel_count"] <= 0
        ):
            raise ValueError("Invalid score bounds, missingness or support")
