"""Concurrent source isolation, memory/failure stops and deterministic joins."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import polars as pl
import pytest


@pytest.fixture
def parallel(monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "parallel_demo", scripts / "parallel_viewability_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workers_overlap_without_nested_threads(parallel, tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,json,sys,time\n"
        "started=time.time();time.sleep(.8)\n"
        "json.dump(dict(started=started,ended=time.time(),threads=os.environ['OMP_NUM_THREADS']),open(sys.argv[1],'w'))\n"
    )
    jobs = [dict(name=n, log=str(tmp_path / f"{n}.log")) for n in ["A", "B", "C"]]
    metrics = parallel.run_jobs(
        jobs,
        2,
        8 * 1024**3,
        lambda job: [sys.executable, str(worker), str(tmp_path / f"{job['name']}.json")],
    )
    results = [json.loads((tmp_path / f"{n}.json").read_text()) for n in ["A", "B", "C"]]
    assert results[0]["started"] < results[1]["ended"]
    assert results[1]["started"] < results[0]["ended"]
    assert results[2]["started"] >= min(r["ended"] for r in results[:2])
    assert all(r["threads"] == "1" for r in results)
    assert metrics["maximum_concurrent_workers"] == 2 and len(metrics["completed"]) == 3
    assert 0 < metrics["peak_rss_bytes"] < metrics["memory_budget_bytes"]


def test_memory_cap_stops_running_worker(parallel, tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("import time;time.sleep(30)\n")
    with pytest.raises(RuntimeError, match="memory cap exceeded"):
        parallel.run_jobs(
            [dict(name="A", log=str(tmp_path / "a.log"))],
            1,
            1,
            lambda job: [sys.executable, str(worker)],
        )


def test_worker_failure_prevents_success(parallel, tmp_path):
    with pytest.raises(RuntimeError, match="exit 7"):
        parallel.run_jobs(
            [dict(name="A", log=str(tmp_path / "a.log"))],
            1,
            8 * 1024**3,
            lambda job: [sys.executable, "-c", "raise SystemExit(7)"],
        )


def test_failed_worker_stops_its_running_peer(parallel, tmp_path, monkeypatch):
    processes = []
    launch = parallel.subprocess.Popen

    def record(*args, **kwargs):
        process = launch(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(parallel.subprocess, "Popen", record)
    jobs = [dict(name=name, log=str(tmp_path / f"{name}.log")) for name in ["A", "B"]]
    with pytest.raises(RuntimeError, match="exit 7"):
        parallel.run_jobs(
            jobs,
            2,
            8 * 1024**3,
            lambda job: [
                sys.executable,
                "-c",
                "raise SystemExit(7)" if job["name"] == "A" else "import time;time.sleep(30)",
            ],
        )
    assert len(processes) == 2 and all(process.poll() is not None for process in processes)


def write_result(path, source, *, upper=0.5):
    path.mkdir()
    row = dict(
        source_type="land",
        source_h3=source,
        target_h3="water",
        lower=0.3,
        upper=upper,
        input_unknown_fraction=0.2,
        average_viewability=None,
    )
    d = dict(
        contract="extended_retained_viewability_demo_v1",
        radius_m=24140.16,
        sources={},
        model={},
        pairs=[row],
        sites=[dict(site=source)],
    )
    d["scientific_signature"] = hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
    (path / "average_viewability.json").write_text(json.dumps(d))
    pl.DataFrame([row]).write_parquet(path / "average_viewability.parquet")


def test_combination_is_order_independent_and_preserves_unknown(parallel, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_result(a, "source-a")
    write_result(b, "source-b")
    first, _, _ = parallel.combine([a, b], tmp_path / "first.parquet")
    second, _, _ = parallel.combine([b, a], tmp_path / "second.parquet")
    assert first == second
    assert all(
        r["average_viewability"] is None and r["input_unknown_fraction"] == 0.2 for r in first
    )


def test_duplicate_or_invalid_pairs_never_write_combined_table(parallel, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_result(a, "same")
    write_result(b, "same")
    with pytest.raises(ValueError, match="Duplicate"):
        parallel.combine([a, b], tmp_path / "duplicate.parquet")
    assert not (tmp_path / "duplicate.parquet").exists()
    c = tmp_path / "c"
    write_result(c, "other", upper=0.9)
    with pytest.raises(ValueError, match="bounds or missingness"):
        parallel.combine([a, c], tmp_path / "invalid.parquet")
    assert not (tmp_path / "invalid.parquet").exists()


@pytest.mark.parametrize("workers", [0, True, 5])
def test_invalid_workers_fail_before_io(parallel, workers):
    with pytest.raises(ValueError, match="worker count"):
        parallel.run(
            dict(
                trial_report="missing",
                terrain_raster="missing",
                output_dir="unused",
                radius_miles=15,
                workers=workers,
                memory_budget_gb=6,
            )
        )
