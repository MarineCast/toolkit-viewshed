"""Incremental strict quality gates without reformatting unrelated legacy modules."""

import subprocess
import sys
from pathlib import Path

PATTERNS = (
    "src/viewshed_toolkit/_internal/performance.py",
    "src/viewshed_toolkit/pipeline/*/components.py",
    "src/viewshed_toolkit/pipeline/api/acquisition.py",
    "src/viewshed_toolkit/pipeline/api/regional.py",
    "src/viewshed_toolkit/pipeline/api/preflight.py",
    "src/viewshed_toolkit/pipeline/api/service.py",
    "src/viewshed_toolkit/pipeline/api/registry.py",
    "src/viewshed_toolkit/pipeline/contracts/provenance.py",
    "src/viewshed_toolkit/pipeline/contracts/service.py",
    "src/viewshed_toolkit/pipeline/cli/compute.py",
    "src/viewshed_toolkit/pipeline/config/datasets.py",
    "src/viewshed_toolkit/pipeline/config/case_study.py",
    "src/viewshed_toolkit/pipeline/contracts/distance.py",
    "src/viewshed_toolkit/pipeline/contracts/generation.py",
    "src/viewshed_toolkit/pipeline/contracts/lineage.py",
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
        "tests/pipeline/test_scientific_diagnostics.py",
        "tests/pipeline/test_generation_integrity.py",
        "tests/pipeline/test_documentation_bundle.py",
        "tests/pipeline/test_demo_entry_points.py",
        "tests/pipeline/test_documentation_geometry.py",
        "tests/pipeline/test_documentation_walkthrough.py",
        "scripts/documentation_geometry.py",
        "scripts/check_documentation_links.py",
        "scripts/run_san_juan_demo.py",
        "scripts/plot_san_juan_demo.py",
        "scripts/docs_hooks.py",
        "scripts/build_documentation_examples.py",
        "scripts/render_documentation_examples.py",
        "scripts/render_documentation_walkthrough.py",
        "scripts/check_documentation_browser.py",
        "scripts/check_installed_wheel.py",
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
