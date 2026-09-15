"""Run acquisition through validated regional components, aggregates, and report evidence."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from pathlib import Path

from report_case_study import build_report
from check_case_study_inputs import check_inputs

from viewshed_toolkit import load_app_config, run_component_stage, run_components
from viewshed_toolkit._internal.performance import measure_stage
from viewshed_toolkit.pipeline.contracts.components import component_root, write_json
from viewshed_toolkit.pipeline.config.case_study import CaseStudyConfig
from viewshed_toolkit.pipeline.api.registry import component_plan
from viewshed_toolkit.pipeline.config.paths import resolve_path
from viewshed_toolkit.pipeline.prepare.area.case_study import prepare_case_geometry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--compute-only",
        action="store_true",
        help="Require prepared rasters and lookup; resume weight stages",
    )
    parser.add_argument(
        "--source-type",
        choices=("land", "water"),
        help="Run one source role; defer combined report",
    )
    parser.add_argument(
        "--report-builder", type=Path, help="Portable HTML deliver_portable_artifact.mjs"
    )
    parser.add_argument(
        "--prepare-only", action="store_true", help="Acquire and prepare all inputs without LOS"
    )
    args = parser.parse_args()
    if args.prepare_only and args.compute_only:
        parser.error("--prepare-only and --compute-only are mutually exclusive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = load_app_config(args.config)
    study = CaseStudyConfig.model_validate(app.raw_config["case_study"])
    root = resolve_path(study.data_directory, app.config_path.parent)
    (root / "cache").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HYRIVER_CACHE_NAME", str(root / "cache" / "http.sqlite"))
    run_component_stage(app, "resolve-area")
    if not args.compute_only:
        prepare_case_geometry(app)
    if args.prepare_only:
        for stage in component_plan(("build-chm-weights", "build-distance-weights")):
            if stage.endswith("-weights"):
                continue
            logging.info("START preparation %s", stage)
            run_component_stage(app, stage)
            logging.info("COMPLETE preparation %s", stage)
        check_inputs(args.config)
        return
    for role in ((args.source_type,) if args.source_type else ("land", "water")):
        if not args.compute_only:
            run_components(app, source_type=role, run_id=f"{study.name}-{app.config_hash}")
            continue
        measurements = []
        for stage in (
            "build-dem-weights",
            "build-chm-weights",
            "build-distance-weights",
            "compose-static-weights",
            "finalize",
            "validate",
            "export-maps",
        ):
            logging.info("START %s %s", role, stage)
            with measure_stage(app.paths.output_dir) as metrics:
                output = run_component_stage(app, stage, source_type=role)
            measurements.append({"stage": stage, "output": str(output), **metrics})
            write_json(
                component_root(app) / "manifests" / f"case-study-{role}.json",
                {
                    "config_hash": app.config_hash,
                    "resolved_config": app.raw_config,
                    "source_type": role,
                    "stages": measurements,
                },
            )
            logging.info("COMPLETE %s %s", role, stage)
    if args.source_type:
        return
    check_inputs(args.config)
    artifact = build_report(args.config)
    print(artifact)
    if args.report_builder:
        output = resolve_path(study.analysis_directory, app.config_path.parent) / "report.html"
        subprocess.run(
            ["node", str(args.report_builder), "--input", str(artifact), "--output", str(output)],
            check=True,
        )
        print(output)


if __name__ == "__main__":
    main()
