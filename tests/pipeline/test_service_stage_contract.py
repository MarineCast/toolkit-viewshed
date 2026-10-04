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


def test_process_collision_is_rejected_before_stage_side_effects(tmp_path, monkeypatch):
    import json

    import pytest

    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.api import service

    config = coastal_fixture(tmp_path)
    app = service.load_app_config(config)
    calls = []

    def execute(app, stages):
        calls.append(stages)
        app.paths.land_h3_path.parent.mkdir(parents=True, exist_ok=True)
        app.paths.land_h3_path.write_bytes(b"test output")

    monkeypatch.setattr(service, "execute_stages", execute)
    request = ViewshedRequest(config, stages=("build-land-cells",), run_id="same-id")
    service.process(request)
    manifest = app.paths.output_dir / "manifests/same-id.json"
    payload = json.loads(manifest.read_text())
    payload["config_hash"] = "another configuration"
    manifest.write_text(json.dumps(payload))
    before = manifest.read_bytes()
    calls.clear()
    for resume in (False, True):
        with pytest.raises(FileExistsError, match="different logical run"):
            service.process(
                ViewshedRequest(config, stages=request.stages, run_id="same-id", resume=resume)
            )
        assert not calls
        assert manifest.read_bytes() == before
    service.process(ViewshedRequest(config, stages=request.stages, run_id="same-id", force=True))
    assert len(calls) == 1
    calls.clear()
    service.process(request)
    assert len(calls) == 1  # Same-identity refresh remains supported.


def test_partial_resume_reenters_stage_validation_and_rejects_changed_inputs(tmp_path, monkeypatch):
    import pytest
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.api import service

    config = coastal_fixture(tmp_path)
    app = service.load_app_config(config)
    observed = []

    def execute(app, stages):
        observed.append(app.paths.land_polygon_path.read_bytes())
        app.paths.land_h3_path.parent.mkdir(parents=True, exist_ok=True)
        app.paths.land_h3_path.write_bytes(b"test output")

    monkeypatch.setattr(service, "execute_stages", execute)
    request = ViewshedRequest(config, stages=("build-land-cells",), run_id="partial", resume=True)
    service.process(request)
    result = service.process(request)
    assert not result.skipped
    assert len(observed) == 2
    app.paths.land_polygon_path.write_bytes(b"changed input at the same path")
    with pytest.raises(ValueError, match="inputs changed"):
        service.process(request)
    assert len(observed) == 2


def test_manifest_identifiers_reject_paths_before_loading_configuration(monkeypatch):
    import pytest

    from viewshed_toolkit.pipeline.api import service

    def unexpected(*args):
        pytest.fail("Invalid run ID reached configuration loading")

    monkeypatch.setattr(service, "load_app_config", unexpected)
    for run_id in ("", ".", "..", "../escape", "/absolute", r"a\b", "C:drive", "a\x00b"):
        with pytest.raises(ValueError, match="simple filename"):
            service.process(ViewshedRequest(Path("unused.yaml"), run_id=run_id))


def test_distance_cli_rejects_unsupported_partial_flags():
    import pytest

    from viewshed_toolkit.pipeline.cli.main import build_cli

    for command in ("build-distance-weights", "distance-weight"):
        for flag in ("--limit", "--start"):
            with pytest.raises(SystemExit) as error:
                build_cli().parse_args([command, flag, "1"])
            assert error.value.code == 2


def test_partial_resume_rejects_legacy_manifest_before_stage_calls(tmp_path, monkeypatch):
    import json

    import pytest

    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.api import service

    config = coastal_fixture(tmp_path)
    app = service.load_app_config(config)
    request = ViewshedRequest(config, stages=("build-land-cells",), resume=True)
    identity = service.workflow_identity_from_config(app.raw_config)
    manifest = app.paths.output_dir / "manifests/viewshed.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "run_id": request.run_id,
                "config_hash": app.config_hash,
                "stage_signature": service._stage_signature(
                    app.config_hash, request.stages, identity=identity
                ),
                "schema_version": "2",
            }
        )
    )
    monkeypatch.setattr(
        service, "execute_stages", lambda *args: pytest.fail("Unexpected execution")
    )
    with pytest.raises(ValueError, match="input-aware manifest v3"):
        service.process(request)


def test_service_rejects_manifest_symlink_escape(tmp_path):
    import pytest

    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.api import service

    config = coastal_fixture(tmp_path)
    app = service.load_app_config(config)
    manifest = app.paths.output_dir / "manifests/viewshed.json"
    manifest.parent.mkdir(parents=True)
    destination = tmp_path / "outside.json"
    destination.write_text("preserve me")
    manifest.symlink_to(destination)
    with pytest.raises(ValueError, match="inside its configured directory"):
        service.process(ViewshedRequest(config, stages=("build-land-cells",), force=True))
    assert destination.read_text() == "preserve me"
