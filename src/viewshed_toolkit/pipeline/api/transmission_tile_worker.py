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
from typing import Any

import numpy as np
from affine import Affine
from shapely import from_wkb

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config.transmission_tiles import load_transmission_tile_config
from ..contracts.transmission import DTYPES, digest, payload_hash
from ..contracts.transmission_tile_budget import reserve_tile_workflow_bytes
from ..finalize.transmission_tiles import validate_source_partition
from ..prepare.area.transmission_index import decode_water_index
from ..weights.transmission_store import score_prepared_source
from .transmission_tile_workflow import checked_store, scoring_identity


def _source_sequence(store: Any, pending: Any, config: Any, provider: Any) -> Any:
    """Keep source/census order; batch only adjacent overlapping windows that fit.

    Window buffers are capped at one quarter of the process limit. A conservative
    per-pixel scoring allowance, provider bytes, decoded cache and 512 MiB reserve
    must also fit. The supervising process remains the hard aggregate-RSS bound.
    """
    index = 0
    pixel_bytes = sum(np.dtype(dtype).itemsize for dtype in DTYPES.values())
    cap = config.max_memory_bytes // 4
    while index < len(pending):
        batch = pending[index : index + 1]
        if config.source_batch_size == 2 and config.calculation_mode != "reference":
            pair = pending[index : index + 2]
            if len(pair) == 2:
                windows = [(Affine(*s["window_transform"]), tuple(s["window_shape"])) for s in pair]
                sizes = [shape[0] * shape[1] for _, shape in windows]
                buffers = sum(sizes) * pixel_bytes
                estimated = (
                    buffers
                    + 80 * max(sizes)
                    + provider.points.nbytes
                    + provider.cells.nbytes
                    + config.decoded_cache_bytes
                    + 512 * 1024**2
                )
                overlap = set(map(tuple, pair[0]["required_tile_keys"])) & set(
                    map(tuple, pair[1]["required_tile_keys"])
                )
                if (
                    windows[0] != windows[1]
                    and overlap
                    and buffers <= cap
                    and estimated <= config.max_memory_bytes
                ):
                    store.prefetch_windows(windows, max_bytes=cap)
                    batch = pair
        try:
            yield from batch
        finally:
            store.discard_prefetched()
        index += len(batch)


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
    # Validate resumed partitions before forming the read schedule. Scheduling
    # hints exclude them but never change source order or frozen census membership.
    pending = []
    for source in plan["sources"]:
        cell = source["source_h3"]
        folder = root / "sources" / cell
        if folder.exists():
            validate_source_partition(folder, generation["identity_hash"], cell)
        else:
            pending.append(source)
    store.plan_windows(s["required_tile_keys"] for s in pending)
    # Exactly one pool across all sources; no nested observer/target pools.
    with ThreadPoolExecutor(max_workers=config.observer_workers) as pool:
        store.scoring_pool = pool
        for source in _source_sequence(store, pending, config, provider):
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
