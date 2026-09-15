"""Sampled process-tree RSS and disk footprint, plus stage wall/CPU timing."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil


@contextmanager
def measure_stage(work_dir: Path, *, interval: float = 0.05) -> Iterator[dict[str, Any]]:
    metrics: dict[str, Any] = {
        "peak_rss_bytes": None,
        "rss_scope": "process_tree",
        "rss_sampling_partial": False,
        "peak_work_disk_bytes": None,
        "disk_sampling_partial": False,
        "sample_interval_seconds": interval,
        "status": "failed",
    }
    process = psutil.Process()
    stop = threading.Event()

    def sample() -> None:
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
        try:
            disk = sum(path.stat().st_size for path in work_dir.rglob("*") if path.is_file())
            metrics["peak_work_disk_bytes"] = max(metrics["peak_work_disk_bytes"] or 0, disk)
        except OSError:
            metrics["disk_sampling_partial"] = True

    def monitor() -> None:
        while not stop.wait(interval):
            sample()

    sample()
    initial_disk = metrics["peak_work_disk_bytes"]
    worker = threading.Thread(target=monitor, daemon=True)
    start, cpu = time.perf_counter(), time.process_time()
    worker.start()
    try:
        yield metrics
        metrics["status"] = "complete"
    finally:
        metrics["wall_seconds"] = time.perf_counter() - start
        metrics["parent_cpu_seconds"] = time.process_time() - cpu
        stop.set()
        worker.join()
        sample()
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
