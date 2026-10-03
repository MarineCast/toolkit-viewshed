"""Typed scientific generation identity and four-file integrity contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from .artifacts import FINAL_SCHEMAS, OBSERVATION_GEOMETRY_SCHEMA_VERSION
from .components import fingerprint
from .pairs import LOS_NUMERICAL_TOLERANCE, validate_pair_kernel

GENERATION_CONTRACT = "viewshed_output_set_v2"


def byte_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parquet_metadata(path: Path) -> dict[str, str]:
    metadata = pq.read_metadata(path).metadata  # type: ignore[no-untyped-call]
    return {
        k.decode(): v.decode() for k, v in (metadata or {}).items() if k.startswith(b"orcacast.")
    }


def generation_identity(
    config_hash: str, source_hashes: Mapping[str, str], coverage: Mapping[str, Any]
) -> dict[str, Any]:
    identity = {
        "method": OBSERVATION_GEOMETRY_SCHEMA_VERSION,
        "scientific_config_hash": config_hash,
        "source_hashes": dict(sorted(source_hashes.items())),
        "input_coverage": dict(coverage),
    }
    return {**identity, "generation_id": fingerprint(identity), "contract": GENERATION_CONTRACT}


def validate_observation_geometry(
    path: Path, *, source_type: str, config_hash: str, generation_id: str, compact_path: Path
) -> None:
    frame = pl.scan_parquet(path)
    schema = frame.collect_schema()
    floats = {
        "distance_km",
        "line_of_sight_support",
        "distance_detection_weight",
        "distance_weighted_los_support",
        "vegetation_attenuation",
        "physical_viewability",
        "distance_adjusted_viewability",
    }
    booleans = {"legacy_static_schema", "HISTORICAL_RECONSTRUCTION"}
    expected = {
        name: pl.Float32() if name in floats else pl.Boolean() if name in booleans else pl.String()
        for name in FINAL_SCHEMAS["observation_geometry"]
    }
    if list(schema.items()) != list(expected.items()):
        raise ValueError("Observation geometry ordered columns/dtypes mismatch")
    validate_pair_kernel(frame)
    metadata = parquet_metadata(path)
    required = {
        "orcacast.schema_version": OBSERVATION_GEOMETRY_SCHEMA_VERSION,
        "orcacast.source_type": source_type,
        "orcacast.generation_id": generation_id,
        "orcacast.config_hash": config_hash,
    }
    if any(metadata.get(k) != v for k, v in required.items()):
        raise ValueError("Observation geometry embedded generation metadata mismatch")
    for column, value in [
        ("source_type", source_type),
        ("GENERATION_ID", generation_id),
        ("CONFIG_HASH", config_hash),
    ]:
        if (
            frame.filter(pl.col(column).is_null() | (pl.col(column) != value))
            .limit(1)
            .collect()
            .height
        ):
            raise ValueError("Observation geometry role/generation identity mismatch")
    states = {
        "positive",
        "derived_zero",
        "source_unavailable",
        "not_applicable",
        "no_baseline_support_neutral",
    }
    for name in floats:
        values = pl.col(name)
        bad = values.is_not_null() & (~values.is_finite() | (values < 0))
        if name != "distance_km":
            bad = bad | (values > 1)
        if frame.filter(bad).limit(1).collect().height:
            raise ValueError("Observation geometry invalid scientific value")
    for name in [c for c in schema.names() if c.endswith("_state")]:
        if (
            frame.filter(pl.col(name).is_null() | ~pl.col(name).is_in(states))
            .limit(1)
            .collect()
            .height
        ):
            raise ValueError("Observation geometry invalid state")
    for value, state in [
        ("line_of_sight_support", "line_of_sight_state"),
        ("distance_detection_weight", "distance_detection_state"),
        ("distance_weighted_los_support", "distance_weighted_los_state"),
        ("vegetation_attenuation", "vegetation_state"),
        ("physical_viewability", "physical_viewability_state"),
        ("distance_adjusted_viewability", "distance_adjusted_viewability_state"),
    ]:
        bad = (
            ((pl.col(state) == "positive") & (pl.col(value).is_null() | (pl.col(value) <= 0)))
            | ((pl.col(state) == "derived_zero") & (pl.col(value).is_null() | (pl.col(value) != 0)))
            | ((pl.col(state) == "source_unavailable") & pl.col(value).is_not_null())
        )
        if state == "vegetation_state":
            bad = (
                bad
                | ((pl.col(state) == "not_applicable") & pl.lit(source_type != "water"))
                | (
                    (pl.col(state) == "no_baseline_support_neutral")
                    & (
                        pl.lit(source_type != "land")
                        | (pl.col("distance_weighted_los_support") != 0)
                        | pl.col(value).is_null()
                        | (pl.col(value) != 1)
                    )
                )
            )
            if source_type == "water":
                bad = bad | (pl.col(state) != "not_applicable") | pl.col(value).is_not_null()
        else:
            bad = bad | ~pl.col(state).is_in(["positive", "derived_zero", "source_unavailable"])
        if frame.filter(bad).limit(1).collect().height:
            raise ValueError("Observation geometry value/state mismatch")
    if (
        frame.filter(
            pl.col("distance_weighted_los_support")
            > pl.col("line_of_sight_support") + LOS_NUMERICAL_TOLERANCE
        )
        .limit(1)
        .collect()
        .height
    ):
        raise ValueError("Observation geometry integrated support exceeds unweighted LOS")
    compact = pl.scan_parquet(compact_path)
    compact_schema = {
        name: pl.String() if name in {"source_h3", "target_h3"} else pl.Float32()
        for name in FINAL_SCHEMAS["static_weights"]
    }
    if list(compact.collect_schema().items()) != list(compact_schema.items()):
        raise ValueError("Compact kernel ordered columns/dtypes mismatch")
    for name in compact_schema:
        if name not in {"source_h3", "target_h3"}:
            validate_pair_kernel(compact, weight=name)
    validate_pair_kernel(compact, weight="weight_static_viewability")
    keys = ["source_h3", "target_h3"]
    if (
        frame.select(keys).join(compact.select(keys), on=keys, how="anti").limit(1).collect().height
        or compact.select(keys)
        .join(frame.select(keys), on=keys, how="anti")
        .limit(1)
        .collect()
        .height
    ):
        raise ValueError("Observation geometry and compact pair universes differ")
    compared = frame.join(compact, on=keys, validate="1:1")
    parity = [
        ("distance_adjusted_viewability", "weight_static_viewability"),
        ("distance_detection_weight", "weight_distance"),
        ("distance_weighted_los_support", "weight_terrain"),
    ]
    if source_type == "land":
        parity.append(("vegetation_attenuation", "weight_vegetation"))
    for geometry_value, compact_value in parity:
        if (
            compared.filter(
                pl.col(geometry_value).is_null()
                | ((pl.col(geometry_value) - pl.col(compact_value)).abs() > LOS_NUMERICAL_TOLERANCE)
            )
            .limit(1)
            .collect()
            .height
        ):
            raise ValueError("Observation geometry and compact values differ: " + geometry_value)
    compact_meta = parquet_metadata(compact_path)
    if compact_meta.get("orcacast.generation_id") != generation_id:
        raise ValueError("Mixed compact and geometry generation")


def validate_generation_receipt(
    receipt: Path, paths: Mapping[str, Path], *, config_hash: str
) -> dict[str, Any]:
    try:
        payload = json.loads(receipt.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Missing or invalid viewshed generation receipt") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid viewshed generation receipt structure")
    if (
        payload.get("contract") != GENERATION_CONTRACT
        or payload.get("method") != OBSERVATION_GEOMETRY_SCHEMA_VERSION
        or payload.get("scientific_config_hash") != config_hash
    ):
        raise ValueError("Viewshed generation receipt scientific identity mismatch")
    identity = {
        key: payload.get(key)
        for key in ("method", "scientific_config_hash", "source_hashes", "input_coverage")
    }
    if payload.get("generation_id") != fingerprint(identity):
        raise ValueError("Viewshed generation identity is invalid")
    if set(payload.get("files", {})) != set(paths):
        raise ValueError("Viewshed generation file set mismatch")
    records = payload.get("prepared_sources")
    if not isinstance(records, dict) or any(
        not isinstance(record, dict)
        or record.get("checksum") != payload["source_hashes"].get("prepared:" + name)
        or not isinstance(record.get("path"), str)
        for name, record in records.items()
    ):
        raise ValueError("Viewshed prepared source lineage mismatch")
    for name, path in paths.items():
        record = payload["files"][name]
        if not isinstance(record, dict) or record.get("sha256") != byte_checksum(path):
            raise ValueError(f"Viewshed generation artifact checksum mismatch: {name}")
    for role in ("land", "water"):
        validate_observation_geometry(
            paths[role + "_observation_geometry"],
            source_type=role,
            config_hash=config_hash,
            generation_id=payload["generation_id"],
            compact_path=paths[role],
        )
    return payload
