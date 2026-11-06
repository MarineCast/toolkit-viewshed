"""Sampled process-tree RSS and disk footprint, plus stage wall/CPU timing."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil


@contextmanager
def measure_stage(
    work_dir: Path, *, interval: float = 0.05, disk_interval: float = 5.0
) -> Iterator[dict[str, Any]]:
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("RSS sampling interval must be finite and positive")
    if not math.isfinite(disk_interval) or disk_interval <= 0:
        raise ValueError("Disk sampling interval must be finite and positive")
    metrics: dict[str, Any] = {
        "peak_rss_bytes": None,
        "rss_scope": "process_tree",
        "rss_sampling_partial": False,
        "peak_work_disk_bytes": None,
        "disk_sampling_partial": False,
        "sample_interval_seconds": interval,
        "disk_sample_interval_seconds": disk_interval,
        "status": "failed",
    }
    process = psutil.Process()
    stop = threading.Event()

    def sample_rss() -> None:
        processes = [process]
        try:
            processes.extend(process.children(recursive=True))
        except (OSError, psutil.Error):
            metrics["rss_scope"] = "parent_only"
            metrics["rss_sampling_partial"] = True
        rss = 0
        observed = False
        for child in processes:
            try:
                rss += child.memory_info().rss
                observed = True
            except (OSError, psutil.Error):
                metrics["rss_sampling_partial"] = True
        if observed:
            metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"] or 0, rss)

    def sample_disk() -> None:
        try:
            disk = sum(path.stat().st_size for path in work_dir.rglob("*") if path.is_file())
            metrics["peak_work_disk_bytes"] = max(metrics["peak_work_disk_bytes"] or 0, disk)
        except OSError:
            metrics["disk_sampling_partial"] = True

    def monitor_rss() -> None:
        while not stop.wait(interval):
            sample_rss()

    def monitor_disk() -> None:
        while not stop.wait(disk_interval):
            sample_disk()

    start, cpu = time.perf_counter(), time.process_time()
    sample_rss()
    sample_disk()
    initial_disk = metrics["peak_work_disk_bytes"]
    workers = [
        threading.Thread(target=monitor_rss, daemon=True),
        threading.Thread(target=monitor_disk, daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        yield metrics
        metrics["status"] = "complete"
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        sample_rss()
        sample_disk()
        metrics["wall_seconds"] = time.perf_counter() - start
        metrics["parent_cpu_seconds"] = time.process_time() - cpu
        metrics["peak_additional_work_disk_bytes"] = (
            max(0, metrics["peak_work_disk_bytes"] - initial_disk)
            if initial_disk is not None and metrics["peak_work_disk_bytes"] is not None
            else None
        )
        for count, rate in (
            ("rows", "rows_per_second"),
            ("source_cells", "source_cells_per_second"),
            ("pairs", "pairs_per_second"),
        ):
            metrics[rate] = (
                metrics.get(count, 0) / metrics["wall_seconds"] if count in metrics else None
            )
