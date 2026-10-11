"""Run real resource supervision in a fresh process with its own RSS budget."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_pilot_supervisor_caps_and_process_cleanup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/pipeline/pilot_supervisor_cases.py",
            f"--basetemp={tmp_path / 'isolated'}",
        ],
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
