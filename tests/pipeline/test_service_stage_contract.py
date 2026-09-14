from __future__ import annotations

from pathlib import Path

from viewshed_toolkit import WorkflowIdentity
from viewshed_toolkit._internal.artifacts import RunManifest
from viewshed_toolkit.pipeline.api import stages
from viewshed_toolkit.pipeline.api.service import (
    STAGES,
    ViewshedRequest,
    _artifact_refs,
    _expected_stage_outputs,
    _manifest_identity_matches_run,
    _resume_refs,
    _stage_signature,
)
from viewshed_toolkit.pipeline.api.registry import invocations_for_stage


def test_default_service_uses_dual_surface_land_contract() -> None:
    assert "build-dual-surface-canopy-weights" in STAGES
    assert "build-vegetation-weights" not in STAGES
    assert STAGES.index("terrain-weight") < STAGES.index("build-vegetation-path-weights")
    assert STAGES[-1] == "export-static-maps"
    assert [item.stage for item in invocations_for_stage("build-dual-surface-canopy-weights")] == [
        "build-dual-surface-canopy-weights"
    ]
    assert [item.source_type for item in invocations_for_stage("terrain-weight")] == ["water"]
    assert [
        item.source_type for item in invocations_for_stage("build-vegetation-path-weights")
    ] == ["water"]
    assert [item.source_type for item in invocations_for_stage("build-distance-weights")] == [
        "land",
        "water",
    ]
    assert invocations_for_stage("export-static-maps")[0].overwrite
    assert invocations_for_stage("finalize-viewshed-lookups")[0].overwrite


def test_stage_signature_includes_workflow_identity() -> None:
    generic = _stage_signature("config-hash", ("terrain-weight",))
    branded = _stage_signature(
        "config-hash",
        ("terrain-weight",),
        identity=WorkflowIdentity(
            workflow="example.visibility",
            dataset_id="example.visibility.static",
            producer="example",
        ),
    )

    assert generic != branded


def test_land_terrain_api_routes_to_paired_surface_production_path(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def paired(config, **kwargs):
        calls.append({"config": config, **kwargs})
        return "paired-result"

    monkeypatch.setattr(stages, "build_dual_surface_canopy_weights", paired)

    result = stages.run_terrain_weights(
        Path("config.yaml"),
        source_type="land",
        overwrite=True,
        max_workers=3,
        batch_size=4,
    )

    assert result == "paired-result"
    assert calls == [
        {
            "config": Path("config.yaml"),
            "overwrite": True,
            "max_workers": 3,
            "batch_size": 4,
        }
    ]


def test_service_inventory_is_exact_not_a_recursive_root_scan() -> None:
    request = ViewshedRequest(
        config=Path("configs/salish_sea.yaml"),
        stages=("prepare-source-target-lookup",),
    )

    outputs = _expected_stage_outputs(request)

    assert len(outputs) == 1
    assert outputs[0].name == "SOURCE_TARGET_LOOKUP_H3R7.parquet"


def test_complete_service_inventory_keeps_only_durable_tables_and_maps() -> None:
    request = ViewshedRequest(config=Path("configs/salish_sea.yaml"))

    outputs = _expected_stage_outputs(request)
    names = {path.name for path in outputs}

    assert "LAND_STATIC_WEIGHTS_R7.parquet" in names
    assert "WATER_STATIC_WEIGHTS_R7.parquet" in names
    assert "SOURCE_TARGET_LOOKUP_H3R7.parquet" not in names
    assert "DISTANCE_WEIGHTS_H3R7.parquet" not in names


def test_service_force_replaces_manifest_without_overwriting_cached_data() -> None:
    assert not invocations_for_stage("download-data")[0].overwrite
    assert not invocations_for_stage("build-dual-surface-canopy-weights")[0].overwrite
    assert invocations_for_stage("export-static-maps")[0].overwrite
    assert invocations_for_stage("finalize-viewshed-lookups")[0].overwrite


def test_resume_requires_exact_stage_signature_paths_and_checksums(tmp_path: Path) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.json"
    first.write_bytes(b"first")
    second.write_text("{}\n", encoding="utf-8")
    request = ViewshedRequest(
        config=Path("configs/salish_sea.yaml"),
        stages=("prepare-source-target-lookup",),
        run_id="test-run",
        resume=True,
    )
    config_hash = "config-hash"
    signature = _stage_signature(config_hash, request.stages)
    refs = _artifact_refs(
        (first.resolve(), second.resolve()),
        request=request,
        config_hash=config_hash,
    )
    manifest_path = tmp_path / "manifest.json"
    RunManifest(
        run_id=request.run_id,
        workflow="human.viewshed",
        config_hash=config_hash,
        resolved_config={},
        outputs=refs,
        stage_signature=signature,
    ).write(manifest_path)

    resumed = _resume_refs(
        manifest_path,
        request=request,
        config_hash=config_hash,
        stage_signature=signature,
        expected_paths=(first.resolve(), second.resolve()),
    )
    assert resumed is not None

    second.unlink()
    assert (
        _resume_refs(
            manifest_path,
            request=request,
            config_hash=config_hash,
            stage_signature=signature,
            expected_paths=(first.resolve(), second.resolve()),
        )
        is None
    )


def test_completed_run_can_refresh_only_its_own_manifest(tmp_path: Path) -> None:
    request = ViewshedRequest(
        config=Path("configs/salish_sea.yaml"),
        stages=("export-static-maps",),
        run_id="test-run",
    )
    config_hash = "config-hash"
    signature = _stage_signature(config_hash, request.stages)
    manifest_path = tmp_path / "manifest.json"
    RunManifest(
        run_id=request.run_id,
        workflow="human.viewshed",
        config_hash=config_hash,
        resolved_config={},
        stage_signature=signature,
    ).write(manifest_path)

    assert _manifest_identity_matches_run(
        manifest_path,
        request=request,
        config_hash=config_hash,
        stage_signature=signature,
    )
    assert not _manifest_identity_matches_run(
        manifest_path,
        request=request,
        config_hash="different-config",
        stage_signature=signature,
    )
    assert not _manifest_identity_matches_run(
        manifest_path,
        request=request,
        config_hash=config_hash,
        stage_signature=_stage_signature(config_hash, ("terrain-weight",)),
    )
