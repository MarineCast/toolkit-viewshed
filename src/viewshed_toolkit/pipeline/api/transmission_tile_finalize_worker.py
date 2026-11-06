"""Supervised bounded finalization after every source has a validated atomic receipt."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config.transmission_tiles import load_transmission_tile_config
from ..contracts.transmission import digest, payload_hash
from ..contracts.transmission_tile_budget import reserve_tile_workflow_bytes
from .transmission_tile_workflow import checked_store, scoring_identity


def finalize(
    path: str, *, publish: bool = True, checked: tuple[Any, Any, Any] | None = None
) -> dict[str, Any]:
    config = load_transmission_tile_config(path)
    _store, plan, _ = checked if checked is not None else checked_store(config)
    root = Path(config.score_dir)
    generation = json.loads((root / "generation.json").read_text())
    identity_hash = generation["identity_hash"]
    metrics = json.loads((root / "scoring-execution.json").read_text()) if publish else {}
    from ..finalize.transmission_tiles import validate_source_partition

    sources = plan["sources"]
    paths = []
    receipts = {}
    total = 0
    nonempty = []
    if {p.name for p in (root / "sources").iterdir()} != {s["source_h3"] for s in sources}:
        raise ValueError("Missing or unexpected scored source")
    for source in sources:
        folder = root / "sources" / source["source_h3"]
        info = validate_source_partition(folder, identity_hash, source["source_h3"])
        paths.append(folder / "pairs.parquet")
        total += info["pair_count"]
        receipts[source["source_h3"]] = dict(
            receipt_sha256=digest(folder / "receipt.json"),
            info_sha256=digest(folder / "source.json"),
        )
        if info["pair_count"]:
            nonempty.append(source["source_h3"])
    import polars as pl

    final = root / "average_viewability.parquet"
    if final.exists():
        # Explicit resume may encounter atomic final bytes published before the final receipt.
        # Validate every source against its trusted partition; never overwrite or relabel.
        for source, path in zip(sources, paths, strict=True):
            expected = pl.read_parquet(path).sort(["source_type", "source_h3", "target_h3"])
            actual = (
                pl.scan_parquet(final)
                .filter(pl.col("source_h3") == source["source_h3"])
                .collect(engine="streaming")
            )
            if not actual.equals(expected):
                raise ValueError("Unreceipted final pairs differ from completed sources")
        if pl.scan_parquet(final).select(pl.len()).collect().item() != total:
            raise ValueError("Unexpected rows in unreceipted final pairs")
    else:
        reserve_tile_workflow_bytes(config, total * 1024 + 1024**2)
        atomic_sink_parquet(
            pl.concat([pl.scan_parquet(p) for p in paths]).sort(
                ["source_type", "source_h3", "target_h3"]
            ),
            final,
            overwrite=False,
        )
    closing_store, _, _ = checked_store(config)
    closing_store.validate()
    expected_identity = scoring_identity(config, closing_store)
    if generation != dict(
        identity=expected_identity, identity_hash=payload_hash(expected_identity)
    ):
        raise ValueError("Prepared completion or scoring identity changed during finalization")
    manifest = dict(
        contract="viewshed_direct_tile_weights_v1",
        identity_hash=identity_hash,
        final_sha256=digest(root / "average_viewability.parquet"),
        sources=receipts,
        pair_count=total,
        nonempty_sources=nonempty,
        execution=dict(scoring=metrics, finalization="supervised_single_child"),
    )
    reserve_tile_workflow_bytes(config, len(json.dumps(manifest).encode()) + 8192)
    if publish:
        atomic_write_json(root / "complete.json", manifest, overwrite=False)
    return manifest


if __name__ == "__main__":
    finalize(sys.argv[1])
