"""Selective component orchestration with durable manifests and telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from viewshed_toolkit._internal.performance import measure_stage

from ..config import AppConfig, load_app_config
from ..config.datasets import CompositionConfig, DatasetsConfig
from ..config.paths import bbox_from_config
from ..contracts.components import (
    component_path,
    component_root,
    input_checksums,
    validate_pairs,
    write_json,
)
from ..finalize.composition import compose_components
from ..prepare.datasets import prepare_dataset
from ..providers import get_provider
from ..weights.components import build_chm_component, build_dem_component, build_distance_component
from .acquisition import download_dataset
from .registry import COMPONENT_STAGES, component_plan
from .stages import build_land_cells, prepare_source_target_lookup


def run_component_stage(
    config: str | Path | AppConfig,
    stage: str,
    *,
    source_type: str = "land",
    overwrite: bool = False,
) -> Path:
    """Execute exactly one stage; missing dependencies are errors, never implicit runs."""
    app = config if isinstance(config, AppConfig) else load_app_config(config)
    if stage not in COMPONENT_STAGES:
        raise ValueError(f"Unknown component stage: {stage}")
    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be land or water")
    root = component_root(app)
    if stage == "resolve-area":
        if "case_study" in app.raw_config:
            from ..config.case_study import CaseStudyConfig
            from ..config.paths import resolve_path

            study = CaseStudyConfig.model_validate(app.raw_config["case_study"])
            resolve_path(study.analysis_directory, app.config_path.parent).mkdir(
                parents=True, exist_ok=True
            )
            resolve_path(study.data_directory, app.config_path.parent).mkdir(
                parents=True, exist_ok=True
            )
        datasets = DatasetsConfig.model_validate(app.raw_config.get("datasets", {}))
        CompositionConfig.model_validate(app.raw_config.get("composition", {}))
        for settings in (datasets.dem, datasets.chm):
            get_provider(settings.provider)
        path = root / "geometry" / "area.json"
        write_json(
            path,
            {
                "name": app.region.name,
                "bbox": list(bbox_from_config(app.raw_config)),
                "config_hash": app.config_hash,
            },
        )
        return path
    if stage.startswith("download-"):
        dataset = stage.removeprefix("download-")
        download_dataset(app, dataset, overwrite=overwrite)
        return root / "inputs" / dataset / "download.json"
    if stage.startswith("prepare-"):
        return prepare_dataset(app, stage.removeprefix("prepare-"), overwrite=overwrite)
    if stage == "build-source-cells":
        from ..contracts.components import cache_matches, provenance, record_product

        contract = provenance(
            app,
            "land_source_cells_v1",
            {"land": app.paths.land_polygon_path, "water": app.paths.water_polygon_path},
        )
        if not overwrite and cache_matches(app.paths.land_h3_path, contract):
            return app.paths.land_h3_path
        build_land_cells(app.config_path, overwrite=True)
        record_product(app.paths.land_h3_path, contract)
        return app.paths.land_h3_path
    if stage == "build-source-target-lookup":
        prepare_source_target_lookup(app.config_path, overwrite=overwrite)
        from ..contracts.artifacts import final_artifact_paths_from_raw

        return final_artifact_paths_from_raw(
            app.raw_config, app.config_path.parent
        ).source_target_lookup
    if stage == "build-target-cells":
        from ..prepare.area.target_cells import build_target_cells

        return build_target_cells(app, overwrite=overwrite)
    if stage == "export-maps":
        from ..finalize.composition import validate_composed
        from ..visualization.component_maps import export_component_map

        validate_composed(app, source_type)
        for factor in ("terrain", "distance", *(("canopy",) if source_type == "land" else ())):
            export_component_map(app, source_type=source_type, factor=factor)
        return export_component_map(app, source_type=source_type)
    functions = {
        "build-dem-weights": build_dem_component,
        "build-chm-weights": build_chm_component,
        "build-distance-weights": build_distance_component,
        "compose-static-weights": compose_components,
    }
    if stage in functions:
        return functions[stage](app, source_type=source_type, overwrite=overwrite)
    path = component_path(app, "static", source_type)
    from ..finalize.composition import validate_composed

    validate_composed(app, source_type)
    if stage == "finalize":
        from ..contracts.components import provenance, write_component

        output = root / "final" / f"{source_type}_static_weights.parquet"
        return write_component(
            pl.read_parquet(path),
            output,
            provenance(app, "final_static_component_v1", {"composition": path}),
            weights=(
                "weight_terrain",
                "weight_vegetation",
                "weight_distance",
                "weight_static_viewability",
            ),
        )
    frame = pl.read_parquet(path)
    validate_pairs(
        frame,
        ("weight_terrain", "weight_vegetation", "weight_distance", "weight_static_viewability"),
    )
    if (
        not (
            frame["weight_static_viewability"]
            - frame["weight_terrain"] * frame["weight_vegetation"]
        )
        .abs()
        .le(1e-7)
        .all()
    ):
        raise ValueError("Static component formula does not agree")
    report = root / "manifests" / f"validation-{source_type}.json"
    from ..contracts.components import cache_matches, provenance

    final = root / "final" / f"{source_type}_static_weights.parquet"
    if not cache_matches(
        final, provenance(app, "final_static_component_v1", {"composition": path})
    ):
        raise ValueError("Final artifact missing, stale, or corrupt; run finalize")
    write_json(
        report,
        {
            "valid": True,
            "rows": frame.height,
            "checksums": input_checksums({"static": path}),
            "config_hash": app.config_hash,
        },
    )
    return report


def run_components(
    config: str | Path | AppConfig,
    *,
    target: str = "all",
    source_type: str = "land",
    run_id: str = "components",
    overwrite: bool = False,
) -> dict[str, Path]:
    app = config if isinstance(config, AppConfig) else load_app_config(config)
    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be land or water")
    targets = {
        "dem": "build-dem-weights",
        "chm": "build-chm-weights",
        "distance": "build-distance-weights",
        "all": "export-maps",
    }
    if target not in targets:
        raise ValueError(f"Unknown build target: {target}")
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a simple filename identifier")
    plan = component_plan((targets[target],))
    manifest = component_root(app) / "manifests" / f"{run_id}-{source_type}.json"
    identity = {"config_hash": app.config_hash, "stages": list(plan), "source_type": source_type}
    if manifest.exists() and not overwrite:
        previous = json.loads(manifest.read_text())
        if previous.get("identity") != identity:
            raise FileExistsError(
                "Manifest belongs to another logical run; use a new run_id or overwrite=True"
            )
    results: dict[str, Path] = {}
    measurements: list[dict[str, Any]] = []
    try:
        for stage in plan:
            with measure_stage(app.paths.output_dir) as metrics:
                metrics["stage"] = stage
                measurements.append(metrics)
                output = run_component_stage(
                    app, stage, source_type=source_type, overwrite=overwrite
                )
                results[stage] = output
                if output.suffix == ".parquet":
                    import pyarrow.parquet as pq

                    frame = pl.scan_parquet(output)
                    metrics["rows"] = pq.read_metadata(output).num_rows  # type: ignore[no-untyped-call]
                    if "source_h3" in pq.read_schema(output).names:  # type: ignore[no-untyped-call]
                        metrics["source_cells"] = (
                            frame.select(pl.col("source_h3").n_unique()).collect().item()
                        )
                        metrics["pairs"] = metrics["rows"]
    finally:
        write_json(
            manifest,
            {
                "identity": identity,
                "resolved_config": app.raw_config,
                "outputs": {k: str(v) for k, v in results.items()},
                "checksums": input_checksums(results),
                "metrics": measurements,
                "complete": len(results) == len(plan),
            },
        )
    return results
