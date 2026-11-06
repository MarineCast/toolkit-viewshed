"""Supported direct-transmission orchestration over provisioned inputs only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit._internal.workers import run_jobs

from ..config.transmission import TransmissionConfig, load_transmission_config
from ..contracts.transmission import (
    TRANSMISSION_CONTRACT,
    digest,
    implementation_identity,
    input_identity,
    payload_hash,
    prepared_identity,
    runtime_identity,
    verify_pins,
)
from ..finalize.transmission import validate_pairs, validate_transmission_product
from ..prepare.transmission import MODEL_RECIPE, prepare
from .registry import TRANSMISSION_STAGES


def run_transmission(
    config: str | Path | TransmissionConfig, *, stage: str = "run"
) -> dict[str, Any]:
    """Build a new versioned scenario generation; no implicit acquisition or overwrite."""
    if stage not in TRANSMISSION_STAGES:
        raise ValueError("Unknown transmission stage")
    cfg = config if isinstance(config, TransmissionConfig) else load_transmission_config(config)
    effective = cfg.runtime()
    root = Path(cfg.output_dir).resolve()
    effective["output_dir"] = str(root)
    for asset in effective["inputs"].values():
        asset["path"] = str(Path(asset["path"]).resolve())
    if stage == "validate":
        result = validate_transmission_product(root)
        if result["config_hash"] != payload_hash(effective):
            raise ValueError("Requested configuration differs from generation")
        return result
    pins, code = input_identity(effective), implementation_identity()
    runtime = runtime_identity()
    fingerprint = payload_hash(effective)
    if stage in {"prepare", "run"}:
        if root.exists():
            raise ValueError("Use a new transmission output directory; no overwrite")
        prepare(effective)
        verify_pins(pins)
        if code != implementation_identity() or runtime != runtime_identity():
            raise ValueError("Implementation changed during preparation")
        atomic_write_json(root / "effective-config.json", effective, overwrite=False)
        atomic_write_json(
            root / "model_and_grid.json",
            dict(
                method=cfg.method,
                recipe=MODEL_RECIPE,
                max_distance_m=cfg.max_distance_m,
                source_samples=cfg.source_samples,
                target_stride=cfg.target_stride,
                qualification=cfg.qualification.model_dump(),
            ),
            overwrite=False,
        )
        receipt = dict(
            contract=TRANSMISSION_CONTRACT,
            status="prepared",
            config_hash=fingerprint,
            input_hashes=pins,
            code_hashes=code,
            runtime=runtime,
            prepared_hashes=prepared_identity(root),
        )
        atomic_write_json(root / "preparation-manifest.json", receipt, overwrite=False)
        if stage == "prepare":
            return receipt
    else:
        receipt = json.loads((root / "preparation-manifest.json").read_text())
        if (
            receipt.get("contract") != TRANSMISSION_CONTRACT
            or receipt["config_hash"] != fingerprint
            or (
                receipt["runtime"] != runtime
                or receipt["input_hashes"] != pins
                or receipt["code_hashes"] != code
                or receipt["prepared_hashes"] != prepared_identity(root)
            )
        ):
            raise ValueError("Prepared generation identity changed; rebuild in a new directory")
    if any(
        (root / name).exists()
        for name in ("partitions", "manifest.json", "average_viewability.parquet")
    ):
        raise ValueError("Use fresh scoring outputs; failed/old outputs are preserved")
    metrics = run_jobs(
        [
            dict(name=f"worker-{i}", index=i, log=str(root / f"worker-{i}.log"))
            for i in range(cfg.workers)
        ],
        cfg.workers,
        int(cfg.memory_budget_gib * 1024**3),
        lambda job: [
            sys.executable,
            "-m",
            "viewshed_toolkit.pipeline.api.transmission_worker",
            str(root / "effective-config.json"),
            str(job["index"]),
        ],
        wall_seconds=cfg.wall_seconds,
    )
    sources = json.loads((root / "source_cells.json").read_text())
    names = [f'partitions/{s["source_h3"]}.parquet' for s in sources]
    table = pl.concat([pl.read_parquet(root / name) for name in names]).sort(
        ["source_type", "source_h3", "target_h3"]
    )
    infos = [json.loads((root / name).with_suffix(".json").read_text()) for name in names]
    validate_pairs(
        table,
        [
            s["source_h3"]
            for s, info in zip(sources, infos, strict=True)
            if info["source_state"] == "complete"
        ],
    )
    if any(info["sampled_result_used"] is False for info in infos):
        raise ValueError("Sampling census gate rejected the approximation; partitions preserved")
    for source, info in zip(sources, infos, strict=True):
        rows = table.filter(pl.col("source_h3") == source["source_h3"])
        if info["source_state"] == "no_target_population":
            if rows.height != 0 or info["target_cells"] != 0:
                raise ValueError("Empty source population mismatch")
            continue
        if (
            info["source_h3"] != source["source_h3"]
            or rows.height != info["target_cells"]
            or rows["observer_count"].unique().to_list() != [len(info["observers"])]
        ):
            raise ValueError("Source completion/support mismatch")
    verify_pins(pins)
    if (
        implementation_identity() != code
        or prepared_identity(root) != receipt["prepared_hashes"]
        or payload_hash(json.loads((root / "effective-config.json").read_text())) != fingerprint
    ):
        raise ValueError("Inputs, effective configuration or code changed during scoring")
    atomic_sink_parquet(table.lazy(), root / "average_viewability.parquet", overwrite=False)
    products = {
        name: digest(root / name)
        for name in [
            *names,
            *[str(Path(n).with_suffix(".json")) for n in names],
            "average_viewability.parquet",
        ]
    }
    manifest = dict(
        contract=TRANSMISSION_CONTRACT,
        status="complete_scenario",
        method=cfg.method,
        config_hash=fingerprint,
        input_hashes=pins,
        code_hashes=code,
        prepared_hashes=receipt["prepared_hashes"],
        runtime=runtime,
        products=products,
        source_count=len(sources),
        pair_count=table.height,
        target_count=table["target_h3"].n_unique(),
        empty_target_sources=sum(info["source_state"] == "no_target_population" for info in infos),
        execution=metrics,
        source_scope="land_inside_requested_rectangle_excluding_rasterized_roofs",
        target_support="partial_h3_water_centres_in_union_of_observer_reach",
        qualification=cfg.qualification.model_dump(),
        empirical_status="uncalibrated_scenario",
        datum_status=(
            "unqualified"
            if cfg.qualification.terrain_vertical_reference in {"unknown", "unverified"}
            or (
                cfg.qualification.terrain_vertical_reference
                != cfg.qualification.water_vertical_reference
            )
            else "declared_common_reference"
        ),
        missingness="complete transmission and above-ground factors nullable; bounds retain unknown paths",
        distance="integrated once into terrain; centroid weight is diagnostic only",
        census_checked_sources=sum(info["sampling_error"] is not None for info in infos),
        outside_candidate_policy="no row means outside source-specific in-reach population, not an observed LOS result",
    )
    atomic_write_json(root / "manifest.json", manifest, overwrite=False)
    return validate_transmission_product(root)
