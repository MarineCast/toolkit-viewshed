"""Incremental strict quality gates without reformatting unrelated legacy modules."""

import subprocess
import sys
from pathlib import Path

PATTERNS = (
    "src/viewshed_toolkit/_internal/performance.py",
    "src/viewshed_toolkit/pipeline/*/components.py",
    "src/viewshed_toolkit/pipeline/api/acquisition.py",
    "src/viewshed_toolkit/pipeline/api/regional.py",
    "src/viewshed_toolkit/pipeline/config/datasets.py",
    "src/viewshed_toolkit/pipeline/config/case_study.py",
    "src/viewshed_toolkit/pipeline/contracts/distance.py",
    "src/viewshed_toolkit/pipeline/prepare/area/case_study.py",
    "src/viewshed_toolkit/pipeline/finalize/aggregate.py",
    "src/viewshed_toolkit/pipeline/prepare/datasets.py",
    "src/viewshed_toolkit/pipeline/prepare/area/target_cells.py",
    "src/viewshed_toolkit/pipeline/providers/*.py",
    "src/viewshed_toolkit/pipeline/weights/distance/products.py",
    "src/viewshed_toolkit/pipeline/finalize/composition.py",
    "src/viewshed_toolkit/pipeline/visualization/component_maps.py",
)


def main() -> None:
    sources = sorted({str(path) for pattern in PATTERNS for path in Path().glob(pattern)})
    files = [
        *sources,
        "tests/pipeline/test_component_workflow.py",
        "tests/pipeline/test_case_study.py",
        "tests/pipeline/test_distance_products.py",
        __file__,
    ]
    subprocess.run(
        ["ruff", "check", "--select", "E,F,I,UP,B,SIM,PERF,RUF", "--ignore", "E501", *files],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "black", "--check", *files], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--ignore-missing-imports",
            *sources,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
