"""Bounded owned-worker process execution; never acquires or deletes products."""

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

THREAD_LIMITS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "POLARS_MAX_THREADS",
    "GDAL_NUM_THREADS",
)


def owned_worker_rss(pids: Any) -> Any:
    """Read only this parent and its directly launched workers; no PID inventory.

    The audited demo worker uses native threads, not descendant processes.
    Live unreadable RSS fails instead of silently disabling the memory limit.
    """
    total = 0
    for pid in [os.getpid(), *pids]:
        try:
            total += psutil.Process(pid).memory_info().rss
        except psutil.NoSuchProcess:
            continue
    return total


def run_jobs(
    jobs: Any, workers: Any, memory_bytes: Any, command: Any, *, wall_seconds: Any = None
) -> Any:
    """Bound concurrent children, stop on failure/cap, and preserve diagnostic files."""
    if (
        not isinstance(workers, int)
        or isinstance(workers, bool)
        or workers < 1
        or not isinstance(memory_bytes, int)
        or memory_bytes <= 0
    ):
        raise ValueError("Positive integer workers and memory cap required")
    environment = dict(os.environ)
    environment.update(dict.fromkeys(THREAD_LIMITS, "1"))
    pending = list(jobs)
    active: dict[str, Any] = {}
    completed: list[str] = []
    started, peak, maximum = time.perf_counter(), 0, 0
    try:
        while pending or active:
            if wall_seconds is not None and time.perf_counter() - started > wall_seconds:
                raise RuntimeError("Owned-worker wall-time cap exceeded; partial outputs preserved")
            while pending and len(active) < workers:
                job = pending.pop(0)
                log = Path(job["log"]).open("wb")  # noqa: SIM115 - lifetime spans worker polls
                try:
                    process = subprocess.Popen(
                        command(job), stdout=log, stderr=subprocess.STDOUT, env=environment
                    )
                except BaseException:
                    log.close()
                    raise
                active[job["name"]] = process, log, job
                maximum = max(maximum, len(active))
                print(f"Started {job['name']} (pid {process.pid})", flush=True)
            peak = max(peak, owned_worker_rss([process.pid for process, _, _ in active.values()]))
            if peak > memory_bytes:
                raise RuntimeError(
                    f"Owned-worker memory cap exceeded: {peak} > {memory_bytes}; "
                    "partial outputs preserved"
                )
            for name, (process, log, job) in list(active.items()):
                status = process.poll()
                if status is None:
                    continue
                log.close()
                del active[name]
                if status:
                    raise RuntimeError(f"Worker {name} failed with exit {status}; see {job['log']}")
                completed.append(name)
                print(f"Completed {name}", flush=True)
            if active:
                time.sleep(0.1)
    finally:
        for process, _, _ in active.values():
            if process.poll() is None:
                process.terminate()
        for process, log, _ in active.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
    return dict(
        wall_seconds=time.perf_counter() - started,
        peak_rss_bytes=peak,
        rss_scope="parent_and_owned_workers",
        rss_sample_interval_seconds=0.1,
        requested_workers=workers,
        maximum_concurrent_workers=maximum,
        memory_budget_bytes=memory_bytes,
        worker_thread_limits=dict.fromkeys(THREAD_LIMITS, 1),
        completed=completed,
    )
