"""Bounded process parallelism across independent retained land-source demos.

Usage: PYTHONPATH=src python scripts/parallel_viewability_demo.py CONFIG.json
Original demo configuration plus workers and memory_budget_gb. No acquisition,
canonical pipeline, overwrite, cleanup, or production promotion.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import polars as pl
import psutil
from benchmark_cell_scores import digest

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

CONTRACT = "parallel_retained_viewability_demo_v1"
THREAD_LIMITS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "POLARS_MAX_THREADS",
    "GDAL_NUM_THREADS",
)


def owned_worker_rss(pids):
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


def run_jobs(jobs, workers, memory_bytes, command):
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
    pending, active, completed = list(jobs), {}, []
    started, peak, maximum = time.perf_counter(), 0, 0
    try:
        while pending or active:
            while pending and len(active) < workers:
                job = pending.pop(0)
                log = Path(job["log"]).open("wb")
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


def combine(outputs, destination):
    """Validate complete source results before writing any combined pair table."""
    summaries = [json.loads((path / "average_viewability.json").read_text()) for path in outputs]
    rows, sites = [], []
    for summary, path in zip(summaries, outputs, strict=True):
        if summary["contract"] != "extended_retained_viewability_demo_v1":
            raise ValueError("Qualified extended demo worker result required")
        if (
            summary["radius_m"] != summaries[0]["radius_m"]
            or summary["sources"] != summaries[0]["sources"]
            or summary["model"] != summaries[0]["model"]
        ):
            raise ValueError("Workers used inconsistent scientific inputs or configuration")
        table = pl.read_parquet(path / "average_viewability.parquet")
        if table.to_dicts() != summary["pairs"] or len(summary["sites"]) != 1:
            raise ValueError("Worker pair table or source count mismatch")
        science = dict(summary)
        signature = science.pop("scientific_signature")
        if hashlib.sha256(json.dumps(science, sort_keys=True).encode()).hexdigest() != signature:
            raise ValueError("Worker scientific signature mismatch")
        for row in summary["pairs"]:
            if (
                not all(row.get(key) for key in ("source_type", "source_h3", "target_h3"))
                or not 0 <= row["lower"] <= row["upper"] <= 1
                or not math.isclose(
                    row["upper"] - row["lower"], row["input_unknown_fraction"], abs_tol=1e-12
                )
                or (row["average_viewability"] is None) != (row["input_unknown_fraction"] > 0)
            ):
                raise ValueError("Worker bounds or missingness mismatch")
        rows.extend(summary["pairs"])
        sites.extend(summary["sites"])
    table = pl.DataFrame(rows).sort(["source_type", "source_h3", "target_h3"])
    if table.unique(["source_type", "source_h3", "target_h3"]).height != table.height:
        raise ValueError("Duplicate source-target pairs across workers")
    atomic_sink_parquet(table.lazy(), destination, overwrite=False)
    return table.to_dicts(), sites, summaries


def run(config):
    required = {
        "trial_report",
        "terrain_raster",
        "output_dir",
        "radius_miles",
        "workers",
        "memory_budget_gb",
    }
    if set(config) != required:
        raise ValueError("Demo configuration plus workers and memory_budget_gb required")
    workers, budget = config["workers"], config["memory_budget_gb"]
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 4:
        raise ValueError("Bounded worker count must be in [1,4]")
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or not math.isfinite(budget)
        or budget <= 0
    ):
        raise ValueError("Positive finite memory_budget_gb required")
    trial = json.loads(Path(config["trial_report"]).read_text())
    names = [site["name"] for site in trial["sites"]]
    if (
        not names
        or len(set(names)) != len(names)
        or not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names)
    ):
        raise ValueError("Unique safe source names required")
    out = Path(config["output_dir"])
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use a new empty parallel output directory")
    out.mkdir(parents=True, exist_ok=True)
    jobs = []
    for name in names:
        child = {k: v for k, v in config.items() if k not in ("workers", "memory_budget_gb")}
        child["sites"] = [name]
        child["output_dir"] = str(out / "sources" / name)
        path = out / f"{name}-config.json"
        atomic_write_json(path, child, overwrite=False)
        jobs.append(dict(name=name, config=str(path), log=str(out / f"{name}.log")))
    worker_script = Path(__file__).with_name("extend_viewability_demo.py")
    metrics = run_jobs(
        jobs,
        min(workers, len(jobs)),
        int(budget * 1024**3),
        lambda job: [sys.executable, str(worker_script), job["config"]],
    )
    rows, sites, summaries = combine(
        [out / "sources" / name for name in names], out / "average_viewability.parquet"
    )
    report = dict(
        contract=CONTRACT,
        config=config,
        execution=metrics,
        sites=sites,
        pairs=rows,
        worker_scientific_signatures={
            name: summary["scientific_signature"]
            for name, summary in zip(names, summaries, strict=True)
        },
        code_checksums={
            "parallel_viewability_demo.py": digest(__file__),
            "extend_viewability_demo.py": digest(worker_script),
        },
        no_downloads_or_publication=True,
    )
    atomic_write_json(out / "parallel_run.json", report, overwrite=False)
    print(json.dumps(metrics), flush=True)
    return report


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
