"""Durable component paths, strict pair validation, and checksum-backed provenance."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

import h3
import polars as pl

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config import AppConfig
from ..config.paths import bbox_from_config
from .artifacts import final_artifact_paths_from_raw

PAIR_KEYS = ["source_h3", "target_h3"]
COMPONENT_VERSION = "static_components_v1"


def component_root(app: AppConfig) -> Path:
    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    return paths.land_static_weights.parent / "components"


def component_path(app: AppConfig, product: str, source_type: str = "land") -> Path:
    if product not in {"dem", "chm", "distance", "static"}:
        raise ValueError(f"Unknown component: {product}")
    if source_type not in {"land", "water"}:
        raise ValueError(f"Unknown source type: {source_type}")
    return component_root(app) / "weights" / source_type / f"{product}_weights.parquet"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def input_checksums(paths: Mapping[str, Path]) -> dict[str, str]:
    result = {}
    for name, path in sorted(paths.items()):
        if not path.exists():
            raise FileNotFoundError(path)
        if path.suffix.lower() == ".shp":
            members = sorted(path.parent.glob(path.stem + ".*"))
            result[name] = fingerprint({member.name: checksum_path(member) for member in members})
        else:
            result[name] = checksum_path(path)
    return result


def provenance(app: AppConfig, algorithm: str, inputs: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "schema_version": COMPONENT_VERSION,
        "algorithm_version": algorithm,
        "software_version": version("viewshed-toolkit"),
        "config_hash": app.config_hash,
        "datasets": app.raw_config.get("datasets", {}),
        "bbox": list(bbox_from_config(app.raw_config)),
        "crs": app.viewshed.crs_projected,
        "resolution_m": app.viewshed.dem_resolution_m,
        "source_h3_resolution": app.h3.source_resolution,
        "target_h3_resolution": app.h3.target_resolution,
        "inputs": input_checksums(inputs),
    }


def cache_matches(path: Path, contract: Mapping[str, Any]) -> bool:
    try:
        metadata = json.loads(path.with_suffix(path.suffix + ".json").read_text())
        return bool(
            metadata["fingerprint"] == fingerprint(contract)
            and metadata["output_checksum"] == checksum_path(path)
        )
    except (OSError, ValueError, KeyError):
        return False


def record_product(path: Path, contract: Mapping[str, Any], **extra: Any) -> None:
    write_json(
        path.with_suffix(path.suffix + ".json"),
        {
            **contract,
            **extra,
            "fingerprint": fingerprint(contract),
            "output_checksum": checksum_path(path),
        },
    )


def validate_pairs(frame: pl.DataFrame, weights: tuple[str, ...] = ()) -> None:
    missing = set([*PAIR_KEYS, *weights]) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing component columns: {sorted(missing)}")
    if frame.select(
        pl.any_horizontal([pl.col(key).is_null() | (pl.col(key) == "") for key in PAIR_KEYS]).any()
    ).item():
        raise ValueError("Null or empty source-target key")
    if frame.select(PAIR_KEYS).is_duplicated().any():
        raise ValueError("Duplicate source-target pair")
    for column in weights:
        values = frame[column]
        if values.null_count() or not values.is_finite().all() or not values.is_between(0, 1).all():
            raise ValueError(f"{column} must contain finite observed weights in [0, 1]")


def write_component(
    frame: pl.DataFrame, path: Path, contract: Mapping[str, Any], *, weights: tuple[str, ...]
) -> Path:
    validate_pairs(frame, weights)
    for column, resolution_key in (
        ("source_h3", "source_h3_resolution"),
        ("target_h3", "target_h3_resolution"),
    ):
        for cell in frame[column].unique():
            if not h3.is_valid_cell(cell) or h3.get_resolution(cell) != contract[resolution_key]:
                raise ValueError(f"Invalid H3 identifier or resolution in {column}: {cell}")
    atomic_sink_parquet(
        frame.sort(PAIR_KEYS).lazy(),
        path,
        metadata={
            "component_contract": json.dumps(contract, sort_keys=True),
        },
    )
    record_product(path, contract, rows=frame.height)
    return path
