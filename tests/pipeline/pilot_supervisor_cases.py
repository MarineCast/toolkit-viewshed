from __future__ import annotations

import json
import sys
import time

import psutil
import pytest

from viewshed_toolkit._internal import pilot
from viewshed_toolkit._internal.pilot import PilotCaps, supervise_pilot


def run(tmp_path, code, caps=None):
    return supervise_pilot(
        [sys.executable, "-c", code],
        staging_root=tmp_path,
        checkpoint_dir=tmp_path / "checkpoint",
        caps=caps,
    )


def test_supervisor_success_keeps_logs_and_counts_native_attempts(tmp_path):
    result = run(
        tmp_path,
        "from viewshed_toolkit._internal.pilot import claim_native_call; claim_native_call('python'); print('retained')",
    )
    assert result["status"] == "completed"
    assert result["native_calls"] == 1
    assert "retained" in (tmp_path / "checkpoint/child.log").read_text()
    assert json.loads((tmp_path / "checkpoint/checkpoint.json").read_text()) == result


def test_wall_time_stops_child_and_preserves_partial_checkpoint(tmp_path):
    result = run(
        tmp_path,
        "import time; print('partial',flush=True); time.sleep(5)",
        PilotCaps(wall_seconds=0.15),
    )
    assert result["status"] == "stopped" and result["reason"] == "wall_time_cap"
    assert result["elapsed_seconds"] < 3
    assert (tmp_path / "checkpoint/child.log").exists()


def test_rss_includes_child_and_stops_allocation(tmp_path):
    baseline = psutil.Process().memory_info().rss
    result = run(
        tmp_path,
        "import time; data=bytearray(100*1024**2); time.sleep(5)",
        PilotCaps(rss_bytes=baseline + 50 * 1024**2),
    )
    assert result["reason"] == "rss_cap"
    assert result["peak_rss_bytes"] > baseline + 50 * 1024**2


def test_disk_cap_counts_existing_task_files_and_preserves_partial(tmp_path):
    (tmp_path / "prior-evidence").write_bytes(b"x" * 4000)
    result = run(
        tmp_path,
        "import os,time; from pathlib import Path; Path(os.environ['TMPDIR'],'partial').write_bytes(b'x'*32000); time.sleep(5)",
        PilotCaps(staging_bytes=16000, disk_poll_seconds=0.02),
    )
    assert result["reason"] == "staging_cap"
    assert (tmp_path / "prior-evidence").stat().st_size == 4000
    assert (tmp_path / "checkpoint/partial").stat().st_size == 32000


def test_preflight_disk_overage_does_not_launch_child(tmp_path):
    (tmp_path / "prior").write_bytes(b"x" * 16000)
    result = run(tmp_path, "raise AssertionError('must not launch')", PilotCaps(staging_bytes=8000))
    assert result["reason"] == "staging_cap" and result["returncode"] is None
    assert not (tmp_path / "checkpoint/child.log").exists()


def test_call_cap_admits_exactly_budget_and_preserves_rejected_attempt(tmp_path):
    result = run(
        tmp_path,
        "from viewshed_toolkit._internal.pilot import claim_native_call; [claim_native_call('retry') for _ in range(3)]",
        PilotCaps(native_calls=2),
    )
    assert result["reason"] == "native_call_cap" and result["native_calls"] == 2
    state = json.loads((tmp_path / "checkpoint/native-calls.json").read_text())
    assert state["rejected_calls"] == 1 and len(state["attempts"]) == 2


def test_interruption_reaps_own_process_and_preserves_checkpoint(tmp_path, monkeypatch):
    original_sleep = pilot.time.sleep

    def interrupt(_):
        monkeypatch.setattr(pilot.time, "sleep", original_sleep)
        raise KeyboardInterrupt

    monkeypatch.setattr(pilot.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, "import time; time.sleep(5)")
    state = json.loads((tmp_path / "checkpoint/checkpoint.json").read_text())
    assert state["status"] == "interrupted" and state["reason"] == "interruption"


def test_timeout_kills_native_descendant_that_ignores_term(tmp_path):
    code = "import subprocess,sys,time,os; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)']); Path(os.environ['TMPDIR'],'pid').write_text(str(p.pid)); time.sleep(20)"
    result = run(tmp_path, code, PilotCaps(wall_seconds=0.3))
    pid = int((tmp_path / "checkpoint/pid").read_text())
    assert result["reason"] == "wall_time_cap"
    assert_stopped(pid)


def test_staging_symlink_rejected_with_preserved_failure(tmp_path):
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(ValueError, match="symlinks"):
        run(tmp_path, "print('never')")
    assert json.loads((tmp_path / "checkpoint/checkpoint.json").read_text())["status"] == "failed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"native_calls": 21},
        {"native_calls": True},
        {"rss_bytes": 769 * 1024**2},
        {"staging_bytes": 3 * 1024**3},
        {"wall_seconds": 2701},
        {"wall_seconds": float("nan")},
        {"poll_seconds": 0},
    ],
)
def test_supervisor_limits_cannot_exceed_reviewed_ceiling(kwargs):
    with pytest.raises(ValueError):
        PilotCaps(**kwargs)


def test_monitoring_denial_fails_closed_with_durable_checkpoint(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("process monitoring denied")

    monkeypatch.setattr(pilot, "_tree_rss", denied)
    with pytest.raises(PermissionError, match="monitoring denied"):
        run(tmp_path, "import time; time.sleep(5)")
    state = json.loads((tmp_path / "checkpoint/checkpoint.json").read_text())
    assert state["status"] == "failed"
    assert state["reason"] == "process monitoring denied"


def test_completed_root_does_not_leave_running_descendant(tmp_path):
    code = "import subprocess,sys,os; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); Path(os.environ['TMPDIR'],'pid').write_text(str(p.pid))"
    result = run(tmp_path, code)
    assert result["status"] == "completed"
    pid = int((tmp_path / "checkpoint/pid").read_text())
    assert_stopped(pid)


def assert_stopped(pid):
    # SIGKILL delivery is asynchronous; permit a bounded scheduler/reaper delay.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.01)
    raise AssertionError(f"Pilot descendant {pid} survived process-group cleanup")
