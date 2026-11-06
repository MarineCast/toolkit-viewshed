from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.artifacts import ArtifactRef, RunManifest, checksum_path
from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file
from viewshed_toolkit._internal.data import StageResult, ValidationReport

from ..config import AppConfig, load_app_config, metadata_sidecar_candidates
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


def _numerical_algorithm_identity(stages: Iterable[str]) -> dict[str, str]:
    """Use the maintained producer versions, not only user configuration, for reuse."""

    selected = set(stages)
    versions: dict[str, str] = {}
    if "prepare-source-target-lookup" in selected:
        from ..contracts import pairs

        versions["source_target_lookup"] = pairs.LOOKUP_ALGORITHM_VERSION
    if "build-distance-weights" in selected:
        from ..weights.distance import compute

        versions.update(
            distance_partition=compute.DISTANCE_PARTITION_ALGORITHM_VERSION,
            distance_combination=compute.DISTANCE_COMBINATION_ALGORITHM_VERSION,
        )
    if selected.intersection({"terrain-weight", "build-dual-surface-canopy-weights"}):
        from ..config import loader
        from ..prepare.area import domains, geometry, raster_stack
        from ..weights.terrain import gdal, land_mask, pixel_index

        versions.update(
            source_sampling=loader.SOURCE_SAMPLING_ALGORITHM_VERSION,
            terrain_partition=gdal.TERRAIN_PARTITION_SCHEMA_VERSION,
            grid_alignment=gdal.GRID_ALIGNMENT_ALGORITHM_VERSION,
            water_terrain=gdal.WATER_TERRAIN_ALGORITHM_VERSION,
            water_terrain_prefilter=gdal.WATER_TERRAIN_PREFILTER_VERSION,
            canonical_pixel_h3=pixel_index.CANONICAL_PIXEL_H3_INDEX_VERSION,
            canonical_raster_stack=raster_stack.CANONICAL_RASTER_STACK_ALGORITHM_VERSION,
            h3_geometry=geometry.H3_GEOMETRY_ALGORITHM_VERSION,
            domain_geometry=domains.DOMAIN_GEOMETRY_ALGORITHM_VERSION,
            water_land_exclusion=land_mask.LAND_EXCLUSION_ALGORITHM_VERSION,
        )
    if "build-dual-surface-canopy-weights" in selected:
        from ..prepare.elevation import canopy
        from ..weights import canopy_visibility

        versions.update(
            canopy_visibility=canopy_visibility.CANOPY_VISIBILITY_CONTRACT_VERSION,
            canopy_alignment=canopy.CANOPY_ALIGNMENT_ALGORITHM_VERSION,
            canopy_surface=canopy.CANOPY_SURFACE_ALGORITHM_VERSION,
            canopy_base=canopy.CANONICAL_CANOPY_BASE_ALGORITHM_VERSION,
            canopy_observer=canopy.OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION,
        )
    if "finalize-viewshed-lookups" in selected:
        from ..contracts import artifacts

        versions.update(
            static_artifact_schema=artifacts.STATIC_ARTIFACT_SCHEMA_VERSION,
            observation_geometry_schema=artifacts.OBSERVATION_GEOMETRY_SCHEMA_VERSION,
        )
    return versions


def _stage_signature(
    config_hash: str,
    stages: Iterable[str],
    *,
    identity: WorkflowIdentity = DEFAULT_WORKFLOW_IDENTITY,
) -> str:
    stages = tuple(stages)
    payload = json.dumps(
        {
            "config_hash": config_hash,
            "stages": list(stages),
            "numerical_algorithms": _numerical_algorithm_identity(stages),
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


def _run_input_fingerprints(app: AppConfig, stages: Iterable[str]) -> dict[str, Any]:
    """Snapshot local stage dependencies, including missing paths and vector companions.

    This is a local reuse check, not a provider freshness claim. A completed full
    workflow snapshots its post-cleanup state, so removed intermediates need not
    be recreated merely to resume; reintroduced or changed inputs invalidate reuse.
    """

    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    geometry = (
        app.paths.land_polygon_path,
        app.paths.water_polygon_path,
        app.paths.land_h3_path,
        app.paths.source_cells_path,
    )
    physical: tuple[Path, ...] = (
        *geometry,
        app.paths.regional_dem_path,
        app.paths.canopy_height_path,
    )
    if app.source_type == "land" and app.paths.marine_water_mask_path is not None:
        physical = (*physical, app.paths.marine_water_mask_path)
    weights = (
        paths.source_target_lookup,
        paths.target_water_area,
        paths.distance_weights,
        paths.ocean_distance_weights,
        paths.terrain_weights,
        paths.canopy_los_weights,
        paths.dual_surface_factors,
        paths.vegetation_weights,
        paths.source_target_clear_sky,
        paths.ocean_terrain_weights,
        paths.ocean_source_target_clear_sky,
        paths.ocean_vegetation_weights,
    )
    dependencies = {
        "download-data": (app.paths.land_polygon_path,),
        "build-land-cells": geometry[:2],
        "prepare-source-target-lookup": geometry,
        "build-distance-weights": (paths.source_target_lookup,),
        "build-dual-surface-canopy-weights": (*physical, paths.source_target_lookup),
        "terrain-weight": (*geometry, paths.source_target_lookup),
        "build-vegetation-path-weights": (
            *geometry,
            paths.source_target_lookup,
            paths.ocean_terrain_weights,
        ),
        "finalize-viewshed-lookups": (*physical, *weights),
        "export-static-maps": (
            *geometry,
            paths.land_static_weights,
            paths.water_static_weights,
            paths.land_observation_geometry,
            paths.water_observation_geometry,
        ),
    }
    selected = {Path(path).resolve() for stage in stages for path in dependencies[stage]}
    result: dict[str, Any] = {}
    for path in sorted(selected):
        if not path.exists():
            result[str(path)] = {"missing": True}
            continue
        members = (
            sorted(member for member in path.parent.glob(path.stem + ".*") if member.is_file())
            if path.suffix.lower() == ".shp"
            else [path]
        )
        members = sorted(
            {
                *members,
                *(
                    member
                    for member in (
                        *metadata_sidecar_candidates(path),
                        path.with_suffix(path.suffix + ".json"),
                    )
                    if member.is_file()
                ),
            }
        )
        result[str(path)] = {
            "members": {
                member.name: (
                    checksum_unchanged_file(member) if member.is_file() else checksum_path(member)
                )
                for member in members
            }
        }
    return result


def _resume_refs(
    manifest_path: Path,
    *,
    request: ViewshedRequest,
    config_hash: str,
    stage_signature: str,
    expected_paths: tuple[Path, ...],
    input_fingerprints: Mapping[str, Any] | None = None,
) -> tuple[ArtifactRef, ...] | None:
    if not manifest_path.exists():
        return None
    try:
        existing = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(existing, dict):
        return None
    if input_fingerprints is None or existing.get("input_fingerprints") != input_fingerprints:
        return None
    if existing.get("status") != "complete":
        return None
    if existing.get("config_hash") != config_hash:
        return None
    if existing.get("stage_signature") != stage_signature:
        return None
    if existing.get("run_id") != request.run_id:
        return None
    try:
        refs = tuple(ArtifactRef.from_dict(item) for item in existing.get("outputs", ()))
    except (KeyError, TypeError, ValueError):
        return None
    if not refs or {item.path.resolve() for item in refs} != set(expected_paths):
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
    """Validate retained lineage before executing the registered stages."""
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
    if manifest_path.exists() and not same_logical_run:
        raise FileExistsError(
            "Refusing to replace a viewshed manifest from a different logical run. "
            f"path={manifest_path}. Explicitly rebuild affected stages with overwrite=True "
            "and use a new run_id. force cannot verify or rebuild cached scientific "
            "products and cannot bypass this check."
        )
    if same_logical_run:
        # Legacy producers do not all validate their input content before reuse.
        # Merely invoking them cannot certify a changed source or corrupt output.
        # Refuse before mutation rather than silently blessing old stage caches.
        current_inputs = _run_input_fingerprints(app, request.stages)
        previous = json.loads(manifest_path.read_text())
        if previous.get("input_fingerprints") != current_inputs:
            raise ValueError(
                "Run input provenance is missing or changed. Explicitly rebuild affected "
                "stages with overwrite=True and use a new run_id; resume/force cannot "
                f"certify existing stage caches: {manifest_path}"
            )
        if request.resume and tuple(request.stages) == STAGES:
            from ..finalize.final_artifacts import validate_static_viewability_outputs

            validate_static_viewability_outputs(request.config)
        refs = _resume_refs(
            manifest_path,
            request=request,
            config_hash=app.config_hash,
            stage_signature=signature,
            expected_paths=expected_paths,
            input_fingerprints=current_inputs,
        )
        if refs is None:
            raise ValueError(
                "Recorded run outputs are missing, corrupt, or incompatible. Explicitly "
                "rebuild affected stages with overwrite=True and use a new run_id; "
                f"resume/force cannot certify existing stage caches: {manifest_path}"
            )
        if request.resume:
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
        input_fingerprints=_run_input_fingerprints(app, request.stages),
    )
    manifest.write(
        manifest_path,
        overwrite=request.force or same_logical_run,
    )
    return StageResult(refs, (report,), manifest)


def validate(artifacts: tuple[ArtifactRef, ...]) -> ValidationReport:
    """Report artifact presence and count. Checksum and schema checks live at their use sites."""
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
