"""Paths and checksum-backed persistence for standalone distance products."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import polars as pl

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config import AppConfig
from .components import component_root, fingerprint, write_json

DISTANCE_PRODUCT_SCHEMA_VERSION = "distance_products_v1"
PAIR_DISTANCE_ALGORITHM_VERSION = "h3_centroid_pair_distance_v1"
DISTANCE_PROFILE_ALGORITHM_VERSION = "distance_profile_from_pair_product_v1"
PAIR_DISTANCE_SCHEMA = (
    "source_h3",
    "target_h3",
    "source_type",
    "distance_m",
    "distance_km",
)
DISTANCE_PROFILE_SCHEMA = (*PAIR_DISTANCE_SCHEMA, "weight_distance")
DISTANCE_METHOD = {
    "name": "h3_centroid_great_circle",
    "formula": "haversine",
    "coordinate_reference_system": "EPSG:4326",
    "earth_radius_km": 6371.0088,
}
_PROFILE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def package_version() -> str:
    """Return installed version, with a stable source-checkout fallback."""

    try:
        return version("viewshed-toolkit")
    except PackageNotFoundError:
        return "0.1.0+source"


def normalize_source_type(source_type: str) -> str:
    normalized = str(source_type).strip().lower()
    if normalized not in {"land", "water"}:
        raise ValueError("source_type must be land or water")
    return normalized


def normalize_profile_id(profile_id: str) -> str:
    normalized = str(profile_id).strip().lower()
    if not _PROFILE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "profile_id must be 1-64 lowercase letters, numbers, dots, underscores, or hyphens; "
            "it must start with a letter or number"
        )
    return normalized


def distance_product_root(app: AppConfig, source_type: str) -> Path:
    root = component_root(app) / "distance" / normalize_source_type(source_type)
    cleanup_root = app.paths.output_dir.resolve()
    if root.resolve().is_relative_to(cleanup_root):
        raise ValueError(
            "Durable distance products require paths.final_output_dir outside paths.output_dir; "
            "the established complete workflow cleans the working output tree"
        )
    return root


def pair_distance_path(app: AppConfig, source_type: str) -> Path:
    return distance_product_root(app, source_type) / "pair_distances.parquet"


def distance_profile_path(
    pair_path: Path,
    *,
    profile_id: str,
    scientific_identity: str,
) -> Path:
    normalized = normalize_profile_id(profile_id)
    return Path(pair_path).parent / "profiles" / f"{normalized}-{scientific_identity[:12]}.parquet"


def distance_metadata_path(path: Path) -> Path:
    path = Path(path)
    return path.with_suffix(path.suffix + ".json")


def require_contained_path(path: Path, root: Path) -> None:
    resolved_path = Path(path).resolve()
    resolved_root = Path(root).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"Distance product path escapes configured output root: {resolved_path}")


def read_distance_product_record(path: Path) -> dict[str, Any]:
    """Load and validate a distance-product sidecar and output checksum."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Missing distance product: {path}")
    metadata_path = distance_metadata_path(path)
    if not metadata_path.is_file():
        raise ValueError(f"Distance product metadata is missing: {metadata_path}")
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Distance product metadata is unreadable: {metadata_path}") from exc
    if not isinstance(record, dict) or not isinstance(record.get("contract"), dict):
        raise ValueError(f"Distance product metadata has no contract: {metadata_path}")
    contract = record["contract"]
    if record.get("fingerprint") != fingerprint(contract):
        raise ValueError(f"Distance product contract fingerprint mismatch: {metadata_path}")
    if record.get("output_checksum") != checksum_path(path):
        raise ValueError(f"Distance product checksum mismatch: {path}")
    output_root = contract.get("output_root")
    if not isinstance(output_root, str) or not output_root:
        raise ValueError(f"Distance product contract has no output_root: {metadata_path}")
    require_contained_path(path, Path(output_root))
    return record


def distance_product_cache_matches(path: Path, contract: Mapping[str, Any]) -> bool:
    try:
        record = read_distance_product_record(path)
    except (FileNotFoundError, ValueError):
        return False
    return record.get("fingerprint") == fingerprint(contract)


def write_distance_product(
    frame: pl.DataFrame,
    path: Path,
    contract: Mapping[str, Any],
    *,
    overwrite: bool,
    extra_metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically write one validated frame and its checksum-backed sidecar."""

    path = Path(path)
    output_root = contract.get("output_root")
    if not isinstance(output_root, str) or not output_root:
        raise ValueError("Distance product contract requires output_root")
    require_contained_path(path, Path(output_root))
    metadata_path = distance_metadata_path(path)
    if (path.exists() or metadata_path.exists()) and not overwrite:
        raise FileExistsError(
            f"Distance product already exists with a different or invalid contract: {path}. "
            "Use overwrite=True or a different profile identifier."
        )
    atomic_sink_parquet(
        frame.lazy(),
        path,
        overwrite=True,
        metadata={"distance_product_contract": json.dumps(contract, sort_keys=True)},
    )
    record: dict[str, Any] = {
        "contract": dict(contract),
        "fingerprint": fingerprint(contract),
        "output_checksum": checksum_path(path),
        "rows": frame.height,
    }
    if extra_metadata:
        record.update(dict(extra_metadata))
    write_json(metadata_path, record)
    return path


__all__ = [
    "DISTANCE_METHOD",
    "DISTANCE_PRODUCT_SCHEMA_VERSION",
    "DISTANCE_PROFILE_ALGORITHM_VERSION",
    "DISTANCE_PROFILE_SCHEMA",
    "PAIR_DISTANCE_ALGORITHM_VERSION",
    "PAIR_DISTANCE_SCHEMA",
    "distance_metadata_path",
    "distance_product_cache_matches",
    "distance_product_root",
    "distance_profile_path",
    "normalize_profile_id",
    "normalize_source_type",
    "package_version",
    "pair_distance_path",
    "read_distance_product_record",
    "require_contained_path",
    "write_distance_product",
]
