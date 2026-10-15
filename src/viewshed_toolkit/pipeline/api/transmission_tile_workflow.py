"""Supported plan/prepare/read-only-score lifecycle for one persistent current tile store."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.workers import run_jobs

from ..config.transmission_tiles import TransmissionTileConfig, load_transmission_tile_config
from ..contracts.transmission import digest, implementation_identity, payload_hash, runtime_identity
from ..contracts.transmission_store import store_runtime_identity
from ..prepare.elevation.transmission_store import PreparedTileStore
from .registry import TRANSMISSION_TILE_STAGES
from .transmission_tiles import run_tile_preparation, verified_inputs


def checked_store(config: TransmissionTileConfig) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    metadata, _ = verified_inputs(config)
    store = PreparedTileStore(config.store_dir, cache_bytes=config.decoded_cache_bytes)
    identity = store.manifest["identity"]
    if (
        identity["code"] != implementation_identity()
        or identity["runtime"] != store_runtime_identity()
        or identity["metadata_sha256"] != config.provisioned_metadata.sha256
        or identity["source_inventory_sha256"] != config.source_inventory.sha256
        or identity["reporting_water_sha256"] != config.reporting_water.sha256
        or identity["grid"] != config.global_transform
        or identity["shape"] != config.global_shape
        or identity["source_policy"] != config.source_policy
        or identity["planning_radius_m"] != config.max_distance_m
    ):
        raise ValueError("Prepared tile store identity changed; do not relabel existing tiles")
    plan = json.loads(store.read_sidecar("plan.json"))
    if plan["identity"] != identity or sorted(plan["required_keys"]) != sorted(
        store.manifest["required_keys"]
    ):
        raise ValueError("Tile plan identity or expected inventory differs")
    # Census IDs are frozen in preparation order, never recomputed after source scheduling.
    if len(plan["census_sources"]) != min(config.census_sources, len(plan["sources"])):
        raise ValueError("Census selection changed; prepare matching plan")
    return store, plan, metadata


def scoring_identity(config: TransmissionTileConfig, store: Any) -> dict[str, Any]:
    return dict(
        contract="viewshed_direct_tile_weights_v1",
        prepared_spec_hash=store.spec_hash,
        preparation_completion_sha256=digest(Path(config.store_dir) / "complete.json"),
        config=config.runtime(),
        code=implementation_identity(),
        runtime=runtime_identity(),
    )


def run_transmission_tiles(
    config: str | Path | TransmissionTileConfig, *, stage: str = "plan", resume: bool = False
) -> dict[str, Any]:
    started = time.monotonic()
    if stage not in TRANSMISSION_TILE_STAGES:
        raise ValueError("Unknown persistent tile workflow stage")
    if not isinstance(config, TransmissionTileConfig):
        config = load_transmission_tile_config(config)
    if stage == "recover-store":
        from ..prepare.elevation.transmission_store import recover_prepared_store
        from .transmission_tiles import plan_transmission_tiles

        plan = plan_transmission_tiles(config)
        return dict(recover_prepared_store(config.store_dir, plan["identity"]))
    if stage == "prepare":
        # Parent bounds aggregate resident memory including its sole preparation child.
        with tempfile.TemporaryDirectory(prefix="viewshed-tile-prepare-") as directory:
            scratch = Path(directory)
            request = scratch / "request.json"
            result = scratch / "result.json"
            payload = config.runtime()
            payload["wall_seconds"] = max(0.1, config.wall_seconds - 10.0)
            request.write_text(json.dumps(payload))
            try:
                metrics = run_jobs(
                    [dict(name="tile-prepare", log=str(scratch / "worker.log"))],
                    1,
                    config.max_memory_bytes,
                    lambda job: [
                        sys.executable,
                        "-m",
                        "viewshed_toolkit.pipeline.api.transmission_tile_preparation_worker",
                        str(request),
                        str(result),
                        "resume" if resume else "new",
                    ],
                    wall_seconds=config.wall_seconds,
                )
            except RuntimeError as exc:
                detail = (scratch / "worker.log").read_text()[-4000:]
                raise RuntimeError(f"{exc}; preparation diagnostic: {detail}") from exc
            value = dict(json.loads(result.read_text()))
            value["execution"] = metrics
            return value
    if stage in {"plan", "validate-store"}:
        return run_tile_preparation(
            config, stage="validate" if stage == "validate-store" else stage, resume=resume
        )
    from ..finalize.transmission_tiles import validate_tile_weights

    store, plan, _ = checked_store(config)
    identity = scoring_identity(config, store)
    root = Path(config.score_dir)
    identity_hash = payload_hash(identity)
    if stage == "validate" or (root / "complete.json").exists():
        store.validate()
        return validate_tile_weights(root, expected_identity=identity_hash)
    if not resume:
        root.mkdir(parents=True, exist_ok=False)
        (root / "sources").mkdir()
        (root / "attempts").mkdir()
        atomic_write_json(root / "score-request.json", config.runtime(), overwrite=False)
        atomic_write_json(
            root / "generation.json",
            dict(identity=identity, identity_hash=identity_hash),
            overwrite=False,
        )
    else:
        if root.is_symlink() or json.loads((root / "generation.json").read_text()) != dict(
            identity=identity, identity_hash=identity_hash
        ):
            raise ValueError("Score resume identity mismatch")
    lock = root / "score.lock"
    with lock.open("x") as handle:
        handle.write(
            json.dumps(
                dict(
                    pid=os.getpid(),
                    identity_hash=identity_hash,
                    recovery="blocked_after_abrupt_death_until_owner_and_workers_are_verified",
                )
            )
        )
    try:
        # One monitored child scores and builds the final proposal. It retains its
        # imports, decoded water geometry, and stat-qualified native checksum cache.
        with tempfile.TemporaryDirectory(prefix="viewshed-tile-final-") as directory:
            proposal_path = Path(directory) / "proposal.json"
            metrics = run_jobs(
                [dict(name="tile-weights-finalize", log=str(root / "worker.log"))],
                1,
                config.max_memory_bytes,
                lambda job: [
                    sys.executable,
                    "-m",
                    "viewshed_toolkit.pipeline.api.transmission_tile_worker",
                    str(root / "score-request.json"),
                    str(proposal_path),
                ],
                wall_seconds=max(0.1, config.wall_seconds - (time.monotonic() - started)),
            )
            proposal = json.loads(proposal_path.read_text())
        metrics["phase_scope"] = "scoring_and_finalization_in_one_child"
        atomic_write_json(root / "scoring-execution.json", metrics, overwrite=True)
        if time.monotonic() - started >= config.wall_seconds:
            raise RuntimeError("Workflow wall-time exhausted before publication")
        return _publish_score_proposal(
            config, proposal, metrics, identity_hash, deadline=started + config.wall_seconds
        )
    finally:
        lock.unlink(missing_ok=True)


def _publish_score_proposal(
    config: TransmissionTileConfig,
    proposal: dict[str, Any],
    metrics: dict[str, Any],
    identity_hash: str,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Accept child evidence against current inputs before the atomic completion marker."""
    from ..contracts.transmission_tile_budget import reserve_tile_workflow_bytes
    from ..finalize.transmission_tiles import validate_tile_weights

    store, plan, _ = checked_store(config)
    store.validate()
    if payload_hash(scoring_identity(config, store)) != identity_hash:
        raise ValueError("Prepared or scoring identity changed before publication")
    if set(proposal["sources"]) != {s["source_h3"] for s in plan["sources"]}:
        raise ValueError("Final proposal differs from prepared source inventory")
    manifest = dict(proposal)
    manifest["execution"] = dict(scoring=metrics, finalization="same_supervised_child")
    root = Path(config.score_dir)
    validate_tile_weights(root, expected_identity=identity_hash, _manifest=manifest)
    reserve_tile_workflow_bytes(config, len(json.dumps(manifest).encode()) + 8192)
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError("Workflow wall-time exhausted before publication")
    atomic_write_json(root / "complete.json", manifest, overwrite=False)
    return validate_tile_weights(root, expected_identity=identity_hash)
