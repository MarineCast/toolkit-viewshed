from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from viewshed_toolkit._internal.artifacts import ArtifactRef, RunManifest, checksum_path
from viewshed_toolkit._internal.data import StageResult, ValidationReport

from ..config import AppConfig, load_app_config
from ..contracts import (
    DEFAULT_WORKFLOW_IDENTITY,
    WorkflowIdentity,
    workflow_identity_from_config,
)
from ..contracts.artifacts import final_artifact_paths_from_raw
from ..contracts.components import input_checksums
from ..contracts.provenance import validate_run_id
from ..contracts.service import partial_run_inputs
from .pipeline import DEFAULT_STAGES, execute_stages

STAGES = DEFAULT_STAGES


@dataclass(frozen=True)
class ViewshedRequest:
    config: Path
    stages: tuple[str, ...] = STAGES
    run_id: str = "viewshed"
    force: bool = False
    resume: bool = False
    identity: WorkflowIdentity | None = None
    study_config: Path | None = None


def _stage_signature(
    config_hash: str,
    stages: Iterable[str],
    *,
    identity: WorkflowIdentity = DEFAULT_WORKFLOW_IDENTITY,
) -> str:
    payload = json.dumps(
        {
            "config_hash": config_hash,
            "stages": list(stages),
            "workflow_identity": {
                "workflow": identity.workflow,
                "dataset_id": identity.dataset_id,
                "producer": identity.producer,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_stage_outputs(request: ViewshedRequest) -> tuple[Path, ...]:
    """Return only the durable artifacts owned by the requested stages."""

    app = load_app_config(request.config)
    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    outputs: list[Path] = []
    compact_complete_run = tuple(request.stages) == STAGES
    for stage in request.stages:
        if compact_complete_run and stage not in {
            "finalize-viewshed-lookups",
            "export-static-maps",
        }:
            continue
        if stage == "download-data":
            outputs.extend([app.paths.regional_dem_path, app.paths.canopy_height_path])
        elif stage == "build-land-cells":
            outputs.append(app.paths.land_h3_path)
        elif stage == "prepare-source-target-lookup":
            outputs.append(paths.source_target_lookup)
        elif stage == "build-distance-weights":
            outputs.extend([paths.distance_weights, paths.ocean_distance_weights])
        elif stage == "build-vegetation-path-weights":
            outputs.append(paths.ocean_vegetation_weights)
        elif stage == "build-dual-surface-canopy-weights":
            outputs.extend(
                [
                    paths.target_water_area,
                    paths.terrain_weights,
                    paths.canopy_los_weights,
                    paths.vegetation_weights,
                    paths.dual_surface_factors,
                ]
            )
        elif stage == "terrain-weight":
            outputs.extend(
                [
                    paths.target_water_area,
                    paths.ocean_terrain_weights,
                    paths.ocean_source_target_clear_sky,
                ]
            )
        elif stage == "finalize-viewshed-lookups":
            outputs.extend(
                [
                    paths.land_static_weights,
                    paths.water_static_weights,
                    paths.land_observation_geometry,
                    paths.water_observation_geometry,
                ]
            )
        elif stage == "export-static-maps":
            from ..visualization.data import static_map_output_paths

            map_paths = static_map_output_paths(app)
            outputs.extend(
                [
                    map_paths.selected_html,
                    map_paths.land_aggregate_html,
                    map_paths.water_aggregate_html,
                    map_paths.manifest,
                    map_paths.selected_values,
                    map_paths.land_target_aggregate_values,
                    map_paths.land_source_aggregate_values,
                    map_paths.water_target_aggregate_values,
                    map_paths.water_source_aggregate_values,
                ]
            )
    return tuple(dict.fromkeys(Path(path).resolve() for path in outputs))


def _artifact_refs(
    paths: Iterable[Path],
    *,
    request: ViewshedRequest,
    config_hash: str,
    identity: WorkflowIdentity = DEFAULT_WORKFLOW_IDENTITY,
) -> tuple[ArtifactRef, ...]:
    return tuple(
        ArtifactRef(
            kind="domain",
            dataset_id=identity.dataset_id,
            path=path,
            producer=identity.producer,
            run_id=request.run_id,
            config_hash=config_hash,
            checksum=checksum_path(path),
            file_count=1,
        )
        for path in paths
    )


def _resume_refs(
    manifest_path: Path,
    *,
    request: ViewshedRequest,
    config_hash: str,
    stage_signature: str,
    expected_paths: tuple[Path, ...],
) -> tuple[ArtifactRef, ...] | None:
    if not manifest_path.exists():
        return None
    existing = json.loads(manifest_path.read_text())
    if existing.get("config_hash") != config_hash:
        return None
    if existing.get("stage_signature") != stage_signature:
        return None
    if existing.get("run_id") != request.run_id:
        return None
    refs = tuple(ArtifactRef.from_dict(item) for item in existing.get("outputs", ()))
    if {item.path.resolve() for item in refs} != set(expected_paths):
        return None
    if not all(ref.path.exists() and ref.checksum == checksum_path(ref.path) for ref in refs):
        return None
    return refs


def _manifest_identity_matches_run(
    manifest_path: Path,
    *,
    request: ViewshedRequest,
    config_hash: str,
    stage_signature: str,
) -> bool:
    """Return whether an existing manifest belongs to this exact logical run.

    A normal non-forced invocation is intentionally allowed to refresh its own
    manifest after the cache-aware stages complete. It must not silently replace
    a manifest belonging to a different run, config, or stage selection.
    """

    if not manifest_path.exists():
        return False
    try:
        existing = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(existing, dict)
        and existing.get("run_id") == request.run_id
        and existing.get("config_hash") == config_hash
        and existing.get("stage_signature") == stage_signature
    )


def _partial_input_refs(app: AppConfig, request: ViewshedRequest) -> tuple[ArtifactRef, ...]:
    """Keep missing inputs explicit and bind vector sidecar bytes as well."""
    return tuple(
        ArtifactRef(
            kind="input",
            producer="viewshed-toolkit",
            path=path,
            checksum=input_checksums({"input": path})["input"] if path.exists() else None,
        )
        for path in partial_run_inputs(app, request.stages)
    )


def process(request: ViewshedRequest) -> StageResult:
    """Execute with an explicit portable study selection scoped to this request."""
    from ..config.study import study_selection

    with study_selection(request.study_config):
        return _process(request)


def _process(request: ViewshedRequest) -> StageResult:
    validate_run_id(request.run_id)
    unknown = sorted(set(request.stages) - set(STAGES))
    if unknown:
        raise ValueError(f"Unknown viewshed stages: {unknown}")
    app = load_app_config(request.config)
    identity = request.identity or workflow_identity_from_config(app.raw_config)
    expected_paths = _expected_stage_outputs(request)
    artifact_paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    manifest_root = (
        app.paths.map_dir / f"h3r{artifact_paths.h3_resolution}" / "manifests"
        if tuple(request.stages) == STAGES
        else app.paths.output_dir / "manifests"
    )
    manifest_path = (manifest_root / f"{request.run_id}.json").resolve()
    if manifest_path.parent != manifest_root.resolve():
        raise ValueError("Manifest destination must remain inside its configured directory")
    signature = _stage_signature(app.config_hash, request.stages, identity=identity)
    same_logical_run = _manifest_identity_matches_run(
        manifest_path,
        request=request,
        config_hash=app.config_hash,
        stage_signature=signature,
    )
    if manifest_path.exists() and not (request.force or same_logical_run):
        raise FileExistsError(
            "Refusing to replace a viewshed manifest from a different logical run. "
            f"path={manifest_path}. Use a new run.version/run-id or pass --force."
        )
    if request.resume and tuple(request.stages) != STAGES and manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        current_inputs = _partial_input_refs(app, request)
        if previous.get("schema_version") != "3":
            raise ValueError(
                "Partial resume requires input-aware manifest v3; rebuild the stage inputs"
            )
        recorded = {item["path"]: item.get("checksum") for item in previous.get("inputs", ())}
        current = {str(item.path): item.checksum for item in current_inputs}
        if recorded != current:
            raise ValueError(
                "Partial run inputs changed or are missing; rebuild affected stages before reuse"
            )
    if request.resume and not request.force and tuple(request.stages) == STAGES:
        # A manifest alone cannot prove current scientific identity. Partial runs
        # re-enter stage-specific cache checks; only a validated retained paired
        # generation can bypass execution (including after permitted cleanup).
        if manifest_path.exists():
            from ..finalize.final_artifacts import validate_static_viewability_outputs

            validate_static_viewability_outputs(request.config)
        refs = _resume_refs(
            manifest_path,
            request=request,
            config_hash=app.config_hash,
            stage_signature=signature,
            expected_paths=expected_paths,
        )
        if refs is not None:
            report = ValidationReport(
                True,
                identity.dataset_id,
                metrics={"resumed": True, "artifact_count": len(refs)},
            )
            return StageResult(refs, (report,), skipped=True)

    execute_stages(app, request.stages)

    if tuple(request.stages) == STAGES:
        from ..finalize.final_artifacts import cleanup_viewshed_dir_to_static_outputs

        cleanup_viewshed_dir_to_static_outputs(request.config)

    missing = tuple(path for path in expected_paths if not path.exists())
    refs = (
        _artifact_refs(
            expected_paths,
            request=request,
            config_hash=app.config_hash,
            identity=identity,
        )
        if not missing
        else ()
    )
    report = ValidationReport(
        valid=not missing and bool(refs),
        dataset_id=identity.dataset_id,
        errors=(
            tuple(f"Missing required stage artifact: {path}" for path in missing)
            if missing
            else (() if refs else ("Viewshed processing produced no durable artifacts",))
        ),
        metrics={"artifact_count": len(refs), "required_artifact_count": len(expected_paths)},
    )
    report.require_valid()
    manifest = RunManifest(
        run_id=request.run_id,
        workflow=identity.workflow,
        config_hash=app.config_hash,
        resolved_config=app.raw_config,
        outputs=refs,
        inputs=_partial_input_refs(app, request) if tuple(request.stages) != STAGES else (),
        stages=tuple({"name": stage, "status": "complete"} for stage in request.stages),
        stage_signature=signature,
        schema_version="3",
    )
    manifest.write(
        manifest_path,
        overwrite=request.force or same_logical_run,
    )
    return StageResult(refs, (report,), manifest)


def validate(artifacts: tuple[ArtifactRef, ...]) -> ValidationReport:
    missing = tuple(item.path for item in artifacts if not item.path.exists())
    dataset_ids = {item.dataset_id for item in artifacts if item.dataset_id}
    dataset_id = (
        dataset_ids.pop() if len(dataset_ids) == 1 else DEFAULT_WORKFLOW_IDENTITY.dataset_id
    )
    return ValidationReport(
        valid=not missing,
        dataset_id=dataset_id,
        errors=tuple(f"Missing artifact: {path}" for path in missing),
        metrics={"artifact_count": len(artifacts)},
    )
