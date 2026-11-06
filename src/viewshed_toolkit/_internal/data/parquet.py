"""Shared Parquet scanning, validation, and atomic persistence."""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Mapping, Sequence

import polars as pl
import pyarrow.parquet as pq
import pyarrow as pa


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


def atomic_project_parquet(
    source_path: Path,
    outputs: Mapping[Path, Sequence[str]],
    *,
    batch_size: int = 65_536,
    batch_observer: Callable[[pl.DataFrame], None] | None = None,
) -> int:
    """Project one bounded input stream into staged outputs, with rollback on failure.

    The observer can reduce diagnostics during the same pass. It must not mutate
    the input. All writers finish before any previous output is replaced.
    """
    source_path = Path(source_path).resolve()
    destinations = {Path(path).resolve(): list(columns) for path, columns in outputs.items()}
    if batch_size < 1 or not destinations or len(destinations) != len(outputs):
        raise ValueError("Projection requires positive batch_size and distinct output paths")
    if source_path in destinations:
        raise ValueError("Projection output must not replace its source")
    before = source_path.stat()
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    temporary: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    promoted: set[Path] = set()
    rows = 0
    try:
        with ExitStack() as stack:
            reader = stack.enter_context(pq.ParquetFile(source_path))
            writers = {}
            for path, columns in destinations.items():
                if not columns or len(columns) != len(set(columns)):
                    raise ValueError("Projection columns must be nonempty and unique")
                schema = pa.schema([reader.schema_arrow.field(name) for name in columns])
                path.parent.mkdir(parents=True, exist_ok=True)
                staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary[path] = staged
                writers[path] = stack.enter_context(
                    pq.ParquetWriter(staged, schema, compression="zstd")
                )
            for batch in reader.iter_batches(batch_size=batch_size):
                table = pa.Table.from_batches([batch])
                if batch_observer is not None:
                    batch_observer(pl.from_arrow(batch))
                for path, columns in destinations.items():
                    writers[path].write_table(table.select(columns))
                rows += batch.num_rows
        after = source_path.stat()
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError(f"Input changed during Parquet projection: {source_path}")
        for path, columns in destinations.items():
            if validate_parquet_schema(temporary[path], columns) != rows:
                raise ValueError(f"Incomplete projected output: {path}")
        for path in destinations:
            if path.exists():
                backup = path.with_name(f".{path.name}.{uuid.uuid4().hex}.backup")
                path.replace(backup)
                backups[path] = backup
        for path, staged in temporary.items():
            staged.replace(path)
            promoted.add(path)
    except Exception:
        for path in promoted:
            path.unlink(missing_ok=True)
        for path, backup in backups.items():
            backup.replace(path)
        raise
    finally:
        # A failed restore must leave its backup available for recovery.
        for path in temporary.values():
            path.unlink(missing_ok=True)
    for path in backups.values():
        path.unlink(missing_ok=True)
    return rows


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
    "atomic_project_parquet",
    "scan_required",
    "validate_parquet_schema",
]
