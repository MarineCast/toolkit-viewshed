"""Private POSIX subprocess supervision and durable native-call admission."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

CONTROL_ENV = "VIEWSHED_BOUNDED_PILOT_CONTROL"


class PilotCallLimitError(RuntimeError):
    """A native attempt was rejected before launching GDAL."""


def claim_native_call(backend: str) -> None:
    """No-op outside a supervised pilot; count each native attempt, including retries."""
    control = os.environ.get(CONTROL_ENV)
    if control is None:
        return
    import fcntl

    path = Path(control)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        if state["calls"] >= state["call_cap"]:
            state["rejected_calls"] += 1
            state["stop_reason"] = "native_call_cap"
            error = True
        else:
            state["calls"] += 1
            state["attempts"].append({"number": state["calls"], "backend": backend})
            error = False
        _write_checkpoint(path, state)
    if error:
        raise PilotCallLimitError("Bounded pilot native call cap reached before GDAL launch")


@dataclass(frozen=True)
class PilotCaps:
    wall_seconds: float = 2700
    rss_bytes: int = 768 * 1024**2
    staging_bytes: int = 2 * 1024**3
    native_calls: int = 20
    poll_seconds: float = 0.02
    disk_poll_seconds: float = 0.1

    def __post_init__(self) -> None:
        for name, maximum in (
            ("rss_bytes", 768 * 1024**2),
            ("staging_bytes", 2 * 1024**3),
            ("native_calls", 20),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(f"{name} must be a positive integer <= {maximum}")
        for name, float_maximum in (
            ("wall_seconds", 2700),
            ("poll_seconds", 0.1),
            ("disk_poll_seconds", 1),
        ):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= float_maximum
            ):
                raise ValueError(f"{name} must be finite, positive and <= {float_maximum}")


def staging_size(root: Path) -> int:
    """Count every retained file logically; reject links escaping or hiding staging."""
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Pilot staging cannot contain symlinks: {path}")
        for name in files:
            with suppress(FileNotFoundError):
                total += (Path(directory) / name).stat().st_size
    return total


def _write_checkpoint(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _tree_rss(process: psutil.Process, parent: psutil.Process) -> int:
    total = int(parent.memory_info().rss)
    with suppress(psutil.NoSuchProcess):
        members = [process, *process.children(recursive=True)]
        for member in members:
            with suppress(psutil.NoSuchProcess):
                total += int(member.memory_info().rss)
    return total


def _terminate_group(process: subprocess.Popen[Any]) -> None:
    """Stop the session we created, including native children; never clean files."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
    finally:
        # Also stop descendants if reaping is interrupted or the root already exited.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=2)


def supervise_pilot(
    command: list[str],
    *,
    staging_root: Path,
    checkpoint_dir: Path,
    caps: PilotCaps | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Supervise a trusted pilot command; all caps are fail-stop sampled ceilings.

    Peak RSS includes the supervisor and all descendants. Disk accounting covers
    total retained task staging, including prior evidence. Sampling can overshoot
    between polls; this is not an OS quota. Checkpoints and partial files survive.
    """
    if os.name != "posix":
        raise ValueError("Bounded pilot supervision requires POSIX process groups")
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("Pilot requires a nonempty explicit command argument list")
    caps = PilotCaps() if caps is None else caps
    staging_root = staging_root.resolve(strict=True)
    checkpoint_dir = checkpoint_dir.resolve()
    if not checkpoint_dir.is_relative_to(staging_root) or checkpoint_dir == staging_root:
        raise ValueError("Checkpoint must be a new child of task staging")
    checkpoint_dir.mkdir(parents=True, exist_ok=False)
    control, checkpoint = checkpoint_dir / "native-calls.json", checkpoint_dir / "checkpoint.json"
    _write_checkpoint(
        control,
        {
            "call_cap": caps.native_calls,
            "calls": 0,
            "rejected_calls": 0,
            "attempts": [],
            "stop_reason": None,
        },
    )
    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "preflight",
        "reason": None,
        "caps": asdict(caps),
        "command": command,
        "peak_rss_bytes": 0,
        "peak_staging_bytes": 0,
        "elapsed_seconds": 0,
        "returncode": None,
        "native_calls": 0,
    }
    start = time.monotonic()
    parent = psutil.Process()
    process = None
    monitor = None

    def measure() -> None:
        state["elapsed_seconds"] = time.monotonic() - start
        state["peak_rss_bytes"] = max(
            state["peak_rss_bytes"],
            parent.memory_info().rss if monitor is None else _tree_rss(monitor, parent),
        )
        state["peak_staging_bytes"] = max(state["peak_staging_bytes"], staging_size(staging_root))

    def cap_reason() -> str | None:
        if state["elapsed_seconds"] > caps.wall_seconds:
            return "wall_time_cap"
        if state["peak_rss_bytes"] > caps.rss_bytes:
            return "rss_cap"
        if state["peak_staging_bytes"] > caps.staging_bytes:
            return "staging_cap"
        return None

    _write_checkpoint(checkpoint, state)
    try:
        measure()
        state["reason"] = cap_reason()
        if state["reason"] is not None:
            state["status"] = "stopped"
            return state
        child_env = {**os.environ, **(env or {}), CONTROL_ENV: str(control)}
        child_env["TMPDIR"] = str(checkpoint_dir)
        with (checkpoint_dir / "child.log").open("wb") as log:
            process = subprocess.Popen(
                command, env=child_env, stdout=log, stderr=log, start_new_session=True
            )
            monitor = psutil.Process(process.pid)
            state["status"] = "running"
            _write_checkpoint(checkpoint, state)
            next_disk = 0.0
            while process.poll() is None:
                state["elapsed_seconds"] = time.monotonic() - start
                state["peak_rss_bytes"] = max(state["peak_rss_bytes"], _tree_rss(monitor, parent))
                if time.monotonic() >= next_disk:
                    state["peak_staging_bytes"] = max(
                        state["peak_staging_bytes"], staging_size(staging_root)
                    )
                    next_disk = time.monotonic() + caps.disk_poll_seconds
                state["reason"] = cap_reason()
                if state["reason"]:
                    break
                time.sleep(caps.poll_seconds)
            if state["reason"]:
                _terminate_group(process)
            else:
                process.wait()
            state["returncode"] = process.returncode
            measure()
            with control.open() as stream:
                calls = json.load(stream)
            state["native_calls"] = calls["calls"]
            state["reason"] = state["reason"] or calls["stop_reason"] or cap_reason()
            state["status"] = (
                "stopped"
                if state["reason"]
                else "completed" if process.returncode == 0 else "failed"
            )
    except (KeyboardInterrupt, SystemExit):
        state["status"], state["reason"] = "interrupted", "interruption"
        raise
    except Exception as error:
        state["status"], state["reason"] = "failed", str(error)
        raise
    finally:
        state["elapsed_seconds"] = time.monotonic() - start
        with control.open() as stream:
            state["native_calls"] = json.load(stream)["calls"]
        # Persist interruption/failure before cleanup, which can itself be interrupted.
        _write_checkpoint(checkpoint, state)
        if process is not None:
            # Root exit must not exempt surviving descendants from cleanup.
            _terminate_group(process)
    return state
