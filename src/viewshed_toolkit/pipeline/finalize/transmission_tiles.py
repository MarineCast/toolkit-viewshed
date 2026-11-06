"""Bounded source-by-source acceptance for persistent-tile weight products."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from ..contracts.transmission import digest, payload_hash
from .transmission import validate_pairs


def validate_source_partition(folder: Path, identity_hash: str, source_h3: str) -> dict[str, Any]:
    if folder.is_symlink() or {p.name for p in folder.iterdir()} != {
        "pairs.parquet",
        "source.json",
        "receipt.json",
    }:
        raise ValueError("Unexpected or unsafe source partition")
    receipt = json.loads((folder / "receipt.json").read_text())
    info = dict(json.loads((folder / "source.json").read_text()))
    if receipt != dict(
        identity_hash=identity_hash,
        source_h3=source_h3,
        pairs_sha256=digest(folder / "pairs.parquet"),
        source_sha256=digest(folder / "source.json"),
    ):
        raise ValueError("Source receipt checksum/identity changed")
    table = pl.read_parquet(folder / "pairs.parquet")
    if info["source_h3"] != source_h3 or table.height != info["pair_count"]:
        raise ValueError("Source completion count differs")
    validate_pairs(table, [source_h3] if table.height else [])
    if table.height:
        if info["sampled_result_used"] is not True or table[
            "observer_count"
        ].unique().to_list() != [len(info["observers"])]:
            raise ValueError("Invalid accepted observer/sample support")
    elif info["source_state"] != "no_target_population":
        raise ValueError("Unexpected empty source")
    return info


def validate_tile_weights(
    root: str | Path,
    *,
    expected_identity: str | None = None,
    _manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    manifest = (
        _manifest if _manifest is not None else json.loads((root / "complete.json").read_text())
    )
    generation = json.loads((root / "generation.json").read_text())
    identity_hash = payload_hash(generation["identity"])
    if (
        root.is_symlink()
        or manifest["contract"] != "viewshed_direct_tile_weights_v1"
        or identity_hash != generation["identity_hash"]
        or manifest["identity_hash"] != identity_hash
        or (expected_identity is not None and expected_identity != identity_hash)
    ):
        raise ValueError("Completed tile weights identity changed")
    if digest(root / "average_viewability.parquet") != manifest["final_sha256"]:
        raise ValueError("Completed pair bytes changed")
    if {p.name for p in (root / "sources").iterdir()} != set(manifest["sources"]):
        raise ValueError("Missing or unexpected completed sources")
    total = 0
    for cell, receipt in manifest["sources"].items():
        folder = root / "sources" / cell
        if receipt != dict(
            receipt_sha256=digest(folder / "receipt.json"),
            info_sha256=digest(folder / "source.json"),
        ):
            raise ValueError("Completed source receipt changed")
        total += validate_source_partition(folder, identity_hash, cell)["pair_count"]
    scan = pl.scan_parquet(root / "average_viewability.parquet")
    if total != manifest["pair_count"] or scan.select(pl.len()).collect().item() != total:
        raise ValueError("Final source pair count changed")
    return dict(
        status="complete_scenario",
        pair_count=total,
        source_count=len(manifest["sources"]),
        identity_hash=identity_hash,
        prepared_store_immutable=True,
    )
