"""Durable, append-only performance telemetry for terrain viewshed runs."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ...config import AppConfig, current_process_memory_mb

TELEMETRY_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()


def write_manifest(rows: list[dict[str, Any]], path: Path) -> None:
    """Write the current terrain batch manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def terrain_performance_path(app: AppConfig) -> Path:
    return Path(app.paths.manifest_path).with_suffix(".performance.jsonl")


def source_window_plan_dir(app: AppConfig) -> Path:
    """Return the partition directory for deterministic source-window plans."""

    return Path(app.paths.manifest_path).with_suffix(".source_windows")


def write_source_window_plan(
    app: AppConfig,
    batch_id: str,
    rows: list[dict[str, Any]],
) -> Path:
    """Atomically persist one batch's source observer-window plan."""

    directory = source_window_plan_dir(app)
    safe_batch_id = str(batch_id).replace("/", "_").replace("\\", "_")
    path = directory / f"{safe_batch_id}.parquet"
    temporary = directory / f".{safe_batch_id}.tmp.parquet"
    frame = pd.DataFrame(rows)
    required = {
        "source_h3_cell",
        "row_start",
        "row_end",
        "col_start",
        "col_end",
        "pixel_count",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Source-window plan is missing required columns: {missing}")
    with _WRITE_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
    return path


def append_terrain_performance(
    app: AppConfig,
    event: str,
    **metrics: Any,
) -> Path:
    """Append one JSON record without replacing prior run measurements."""

    path = terrain_performance_path(app)
    payload = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "event": str(event),
        "config_hash": str(app.config_hash),
        "run_name": str(app.run.name),
        "run_version": str(app.run.version),
        "source_type": str(app.source_type),
        "surface_model": str(app.viewshed.surface_model),
        "observer_height_class": app.observer_height_class,
        "process_memory_mb": current_process_memory_mb(),
        **metrics,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str) + "\n"
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
    return path
