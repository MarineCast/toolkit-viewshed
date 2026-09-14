"""Shared Parquet scanning, validation, and atomic persistence."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl
import pyarrow.parquet as pq


def _read_parquet_key_value_metadata(path: Path) -> dict[str, str]:
    raw = pq.read_metadata(str(path)).metadata or {}
    return {key.decode("utf-8"): value.decode("utf-8") for key, value in raw.items()}


def atomic_sink_parquet(
    lf: pl.LazyFrame,
    output_path: Path,
    *,
    overwrite: bool = True,
    metadata: Mapping[str, str] | None = None,
) -> int:
    """Write a LazyFrame atomically and return its persisted row count."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not overwrite:
        expected = dict(metadata or {})
        if expected:
            actual = _read_parquet_key_value_metadata(output_path)
            mismatches = {
                key: {"expected": value, "actual": actual.get(key)}
                for key, value in expected.items()
                if actual.get(key) != value
            }
            if mismatches:
                raise ValueError(
                    "Existing Parquet metadata does not match the requested artifact: "
                    f"path={output_path} mismatches={json.dumps(mismatches, sort_keys=True)}. "
                    "Rebuild with overwrite=True."
                )
        return int(pq.read_metadata(str(output_path)).num_rows)

    tmp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        lf.sink_parquet(str(tmp_path), metadata=dict(metadata or {}))
        tmp_path.replace(output_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return int(pq.read_metadata(str(output_path)).num_rows)


def scan_required(path_or_paths: Path | Sequence[Path], required: Sequence[str]) -> pl.LazyFrame:
    """Scan Parquet input files and select required columns in order."""

    paths = [path_or_paths] if isinstance(path_or_paths, Path) else list(path_or_paths)
    if not paths:
        raise FileNotFoundError("No Parquet paths were supplied.")
    normalized = [Path(path) for path in paths]
    missing_files = [str(path) for path in normalized if not path.exists()]
    if missing_files:
        raise FileNotFoundError("Missing Parquet inputs:\n" + "\n".join(missing_files[:20]))
    frame = pl.scan_parquet([str(path) for path in normalized])
    schema = set(frame.collect_schema().names())
    missing = [column for column in required if column not in schema]
    if missing:
        raise ValueError(f"Missing required columns {missing} in {normalized[0]}")
    return frame.select(list(required))


def validate_parquet_schema(path: Path, columns: Sequence[str]) -> int:
    """Require an exact Parquet schema and return its row count."""

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Missing final artifact: {path}")
    actual = list(pq.read_schema(str(path)).names)
    expected = list(columns)
    if actual != expected:
        raise ValueError(f"Invalid schema for {path}. Expected exactly {expected}; got {actual}.")
    return int(pq.read_metadata(str(path)).num_rows)


__all__ = [
    "atomic_sink_parquet",
    "scan_required",
    "validate_parquet_schema",
]
