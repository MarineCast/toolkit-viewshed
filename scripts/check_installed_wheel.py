"""Build a wheel and verify its installation outside the source checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="viewshed-wheel-") as directory:
        scratch = Path(directory)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "VIEWSHED_TOOLKIT_ROOT"}
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(scratch),
                str(ROOT),
            ],
            check=True,
            env=environment,
        )
        wheel = next(scratch.glob("*.whl"))
        venv = scratch / "installed"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
            env=environment,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
            check=True,
            cwd=scratch,
            env=environment,
        )
        probe = """
import json, sys
from pathlib import Path
import viewshed_toolkit
from viewshed_toolkit.resources import common_areas_path, default_config_path
package = Path(viewshed_toolkit.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
resources = [common_areas_path(), default_config_path()]
assert all(p.is_file() and p.resolve().is_relative_to(Path(sys.prefix).resolve()) for p in resources), resources
print(json.dumps({'package': str(package), 'resources': [p.name for p in resources]}))
"""
        result = subprocess.run(
            [str(python), "-c", probe],
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            print(result.stdout, result.stderr, file=sys.stderr)
        result.check_returncode()
        command = venv / (
            "Scripts/viewshed-toolkit.exe" if os.name == "nt" else "bin/viewshed-toolkit"
        )
        help_result = subprocess.run(
            [str(command), "--help"],
            check=True,
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert "usage:" in help_result.stdout.lower()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "python": sys.version.split()[0],
                    "wheel": wheel.name,
                    "installed": json.loads(result.stdout),
                    "cli_help": "passed",
                    "dependencies": "reused system site packages; toolkit itself installed from the wheel",
                }
            )
        )


if __name__ == "__main__":
    main()
