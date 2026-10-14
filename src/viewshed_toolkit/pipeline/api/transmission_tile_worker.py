"""One supervised source process reads immutable tiles and publishes atomic source evidence."""

from __future__ import annotations

import gc
import json
import math
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from affine import Affine
from shapely import from_wkb

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config.transmission_tiles import load_transmission_tile_config
from ..contracts.transmission import digest, payload_hash
from ..contracts.transmission_tile_budget import reserve_tile_workflow_bytes
from ..finalize.transmission_tiles import validate_source_partition
from ..prepare.area.transmission_index import decode_water_index
from ..weights.transmission_store import score_prepared_source
from .transmission_tile_workflow import checked_store, scoring_identity


def worker(path: str, proposal_path: str | None = None) -> None:
    config = load_transmission_tile_config(path)
    store, plan, metadata = checked_store(config)
    root = Path(config.score_dir)
    generation = json.loads((root / "generation.json").read_text())
    expected = scoring_identity(config, store)
    if generation != dict(identity=expected, identity_hash=payload_hash(expected)):
        raise ValueError("Score generation or prepared completion identity changed")
    water = from_wkb(store.read_sidecar("reporting-water.wkb"))
    provider = decode_water_index(
        store.read_sidecar("water-index.npz"), water, Affine(*config.global_transform)
    )
    expected_sources = {s["source_h3"] for s in plan["sources"]}
    if {p.name for p in (root / "sources").iterdir()} - expected_sources:
        raise ValueError("Unexpected existing score source")
    # Exactly one pool across all sources; no nested observer/target pools.
    with ThreadPoolExecutor(max_workers=config.observer_workers) as pool:
        store.scoring_pool = pool
        for source in plan["sources"]:
            cell = source["source_h3"]
            folder = root / "sources" / cell
            if folder.exists():
                validate_source_partition(folder, generation["identity_hash"], cell)
                continue
            frame, info = score_prepared_source(
                store,
                source,
                water,
                provider,
                config,
                census=cell in plan["census_sources"],
                water_class_policy=metadata["water_class_policy"],
                canopy_gap_policy=metadata.get("canopy_gap_policy", "retain_unknown"),
                canopy_gap_qualified=metadata.get("canopy_gap_policy") == "assume_zero_acquired",
            )
            # Conservative encoded-table allowance plus exact observer metadata bytes.
            reserve_tile_workflow_bytes(
                config,
                math.ceil(frame.estimated_size()) * 4 + len(json.dumps(info).encode()) + 1024**2,
            )
            attempt = root / "attempts" / (cell + "-" + uuid.uuid4().hex)
            attempt.mkdir()
            info["pair_count"] = frame.height
            atomic_sink_parquet(frame.lazy(), attempt / "pairs.parquet", overwrite=False)
            atomic_write_json(attempt / "source.json", info, overwrite=False)
            atomic_write_json(
                attempt / "receipt.json",
                dict(
                    identity_hash=generation["identity_hash"],
                    source_h3=cell,
                    pairs_sha256=digest(attempt / "pairs.parquet"),
                    source_sha256=digest(attempt / "source.json"),
                ),
                overwrite=False,
            )
            validate_source_partition(attempt, generation["identity_hash"], cell)
            os.rename(attempt, folder)
            del frame
            gc.collect()
    closing = checked_store(config)
    reserve_tile_workflow_bytes(config, 0)
    if proposal_path is not None:
        from .transmission_tile_finalize_worker import finalize

        proposal = finalize(path, publish=False, checked=closing)
        atomic_write_json(Path(proposal_path), proposal, overwrite=False)


if __name__ == "__main__":
    worker(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
