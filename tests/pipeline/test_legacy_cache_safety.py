from __future__ import annotations

import argparse
import copy
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import h3
import polars as pl
import pytest
import yaml
from shapely.geometry import box

from viewshed_toolkit import load_app_config
from viewshed_toolkit.pipeline.api import service
from viewshed_toolkit.pipeline.cli import compute as cli
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig, DistanceWeightPaths
from viewshed_toolkit.pipeline.weights.distance import compute


@pytest.fixture
def isolated_service(tmp_path: Path, monkeypatch):
    app = load_app_config("configs/salish_sea.yaml")
    source = tmp_path / "land.shp"
    source.write_bytes(b"shape")
    source.with_suffix(".dbf").write_bytes(b"attributes")
    water = tmp_path / "water.parquet"
    water.write_bytes(b"water")
    output = tmp_path / "land-cells.parquet"
    raw = copy.deepcopy(app.raw_config)
    raw.setdefault("paths", {}).update(
        output_dir=str(tmp_path / "work"), final_output_dir=str(tmp_path / "final")
    )
    app = replace(
        app,
        raw_config=raw,
        paths=replace(
            app.paths,
            output_dir=tmp_path / "work",
            map_dir=tmp_path / "maps",
            land_polygon_path=source,
            water_polygon_path=water,
            regional_dem_path=tmp_path / "dem.tif",
            canopy_height_path=tmp_path / "canopy.tif",
            land_h3_path=tmp_path / "land-h3.parquet",
            source_cells_path=tmp_path / "source-cells.parquet",
        ),
    )
    calls = []

    def execute(_app, stages):
        calls.append(tuple(stages))
        output.write_bytes(source.read_bytes() + source.with_suffix(".dbf").read_bytes())

    monkeypatch.setattr(service, "load_app_config", lambda _path: app)
    monkeypatch.setattr(service, "_expected_stage_outputs", lambda _request: (output,))
    monkeypatch.setattr(service, "execute_stages", execute)
    request = service.ViewshedRequest(
        config=app.config_path, stages=("build-land-cells",), run_id="bounded"
    )
    return SimpleNamespace(app=app, source=source, output=output, calls=calls, request=request)


def test_service_resume_checks_inputs_and_shapefile_companions(isolated_service) -> None:
    fixture = isolated_service
    service.process(fixture.request)
    assert service.process(replace(fixture.request, resume=True)).skipped
    assert len(fixture.calls) == 1

    fixture.source.with_suffix(".dbf").write_bytes(b"new attributes")
    for resume, force in ((True, False), (True, True), (False, False), (False, True)):
        with pytest.raises(ValueError, match="input provenance is missing or changed"):
            service.process(replace(fixture.request, resume=resume, force=force))
    assert fixture.output.read_bytes() == b"shapeattributes"
    assert len(fixture.calls) == 1
    fixture.source.with_suffix(".dbf").write_bytes(b"attributes")
    assert service.process(replace(fixture.request, resume=True)).skipped
    fixture.source.with_suffix(".prj").write_bytes(b"new CRS")
    with pytest.raises(ValueError, match="input provenance"):
        service.process(replace(fixture.request, resume=True))
    fixture.source.with_suffix(".prj").unlink()
    assert service.process(replace(fixture.request, resume=True)).skipped


@pytest.mark.parametrize(
    ("stage", "field"),
    [
        ("build-dual-surface-canopy-weights", "regional_dem_path"),
        ("build-dual-surface-canopy-weights", "canopy_height_path"),
        ("build-distance-weights", "lookup"),
    ],
)
def test_service_resume_rechecks_rasters_and_lookup(isolated_service, stage, field):
    fixture = isolated_service
    if field == "lookup":
        input_path = service.final_artifact_paths_from_raw(
            fixture.app.raw_config, fixture.app.config_path.parent
        ).source_target_lookup
    else:
        input_path = getattr(fixture.app.paths, field)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"original input")
    request = replace(fixture.request, stages=(stage,))
    service.process(request)
    assert service.process(replace(request, resume=True)).skipped
    input_path.write_bytes(b"changed input")
    with pytest.raises(ValueError, match="input provenance"):
        service.process(replace(request, resume=True))
    input_path.write_bytes(b"original input")
    input_path.with_name(input_path.stem + "_metadata.json").write_text('{"revision": 2}')
    with pytest.raises(ValueError, match="input provenance"):
        service.process(replace(request, resume=True))
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("resume", [False, True])
def test_service_conflict_fails_before_mutating_outputs(isolated_service, monkeypatch, resume):
    fixture = isolated_service
    service.process(fixture.request)
    before = fixture.output.read_bytes()
    monkeypatch.setattr(
        service, "load_app_config", lambda _path: replace(fixture.app, config_hash="changed")
    )
    with pytest.raises(FileExistsError, match="different logical run"):
        service.process(replace(fixture.request, resume=resume))
    assert len(fixture.calls) == 1
    assert fixture.output.read_bytes() == before
    with pytest.raises(FileExistsError, match="different logical run"):
        service.process(replace(fixture.request, force=True))
    assert len(fixture.calls) == 1


@pytest.mark.parametrize(
    ("stage", "module_name", "version_name"),
    [
        (
            "build-dual-surface-canopy-weights",
            "viewshed_toolkit.pipeline.config.loader",
            "SOURCE_SAMPLING_ALGORITHM_VERSION",
        ),
        (
            "terrain-weight",
            "viewshed_toolkit.pipeline.weights.terrain.gdal",
            "TERRAIN_PARTITION_SCHEMA_VERSION",
        ),
        (
            "terrain-weight",
            "viewshed_toolkit.pipeline.weights.terrain.gdal",
            "GRID_ALIGNMENT_ALGORITHM_VERSION",
        ),
        (
            "build-dual-surface-canopy-weights",
            "viewshed_toolkit.pipeline.prepare.elevation.canopy",
            "OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION",
        ),
        (
            "build-distance-weights",
            "viewshed_toolkit.pipeline.weights.distance.compute",
            "DISTANCE_PARTITION_ALGORITHM_VERSION",
        ),
    ],
)
def test_service_algorithm_version_change_invalidates_resume_before_execution(
    isolated_service, monkeypatch, stage, module_name, version_name
):
    fixture = isolated_service
    request = replace(fixture.request, stages=(stage,))
    service.process(request)
    assert service.process(replace(request, resume=True)).skipped
    manifest_path = fixture.app.paths.output_dir / "manifests" / "bounded.json"
    before = manifest_path.read_bytes()
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, version_name, getattr(module, version_name) + "_next")
    for force in (False, True):
        with pytest.raises(FileExistsError, match="different logical run"):
            service.process(replace(request, resume=True, force=force))
    assert len(fixture.calls) == 1
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize("force", [False, True])
def test_legacy_service_manifest_without_input_snapshot_is_not_reused(isolated_service, force):
    fixture = isolated_service
    service.process(fixture.request)
    manifest_path = fixture.app.paths.output_dir / "manifests" / "bounded.json"
    payload = json.loads(manifest_path.read_text())
    payload.pop("input_fingerprints")
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="input provenance"):
        service.process(replace(fixture.request, resume=True, force=force))
    assert len(fixture.calls) == 1


def test_complete_service_resume_preserves_post_cleanup_state(isolated_service, monkeypatch):
    from viewshed_toolkit.pipeline.finalize import final_artifacts

    fixture = isolated_service
    lookup = service.final_artifact_paths_from_raw(
        fixture.app.raw_config, fixture.app.config_path.parent
    ).source_target_lookup
    original_execute = service.execute_stages

    def execute(app, stages):
        original_execute(app, stages)
        lookup.parent.mkdir(parents=True, exist_ok=True)
        lookup.write_bytes(b"generated intermediate")

    monkeypatch.setattr(service, "execute_stages", execute)
    monkeypatch.setattr(
        final_artifacts, "cleanup_viewshed_dir_to_static_outputs", lambda _config: lookup.unlink()
    )
    validated = []
    monkeypatch.setattr(final_artifacts, "validate_static_viewability_outputs", validated.append)
    request = replace(fixture.request, stages=service.STAGES)
    service.process(request)
    assert not lookup.exists()
    assert service.process(replace(request, resume=True)).skipped
    assert validated == [request.config]
    assert len(fixture.calls) == 1
    fixture.source.write_bytes(b"updated source geometry")
    with pytest.raises(ValueError, match="input provenance"):
        service.process(replace(request, resume=True))
    assert len(fixture.calls) == 1
    assert not lookup.exists()


def test_service_resume_rejects_modified_output(isolated_service):
    fixture = isolated_service
    service.process(fixture.request)
    fixture.output.write_bytes(b"corrupt output")
    for force in (False, True):
        with pytest.raises(ValueError, match="outputs are missing, corrupt"):
            service.process(replace(fixture.request, resume=True, force=force))
    assert fixture.output.read_bytes() == b"corrupt output"
    assert len(fixture.calls) == 1


def test_actual_land_producer_cannot_recertify_stale_cache_on_resume(tmp_path: Path):
    from viewshed_toolkit.pipeline.api.registry import StageInvocation
    from viewshed_toolkit.pipeline.api.stages import run_stage
    from viewshed_toolkit.pipeline.prepare.area.land import build_land_cells_for_config

    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    raw.pop("area", None)
    raw["region"]["bbox_wgs84"] = {
        "min_lon": -123.12,
        "min_lat": 48.08,
        "max_lon": -123.08,
        "max_lat": 48.12,
    }
    land = tmp_path / "land.gpkg"
    land_cells = tmp_path / "source" / "land-h3.parquet"
    raw.setdefault("paths", {}).update(
        land_polygon_path=str(land),
        water_polygon_path=str(tmp_path / "water.parquet"),
        land_h3_path=str(land_cells),
        source_cells_path=str(land_cells),
        output_dir=str(tmp_path / "work"),
        final_output_dir=str(tmp_path / "final"),
        map_dir=str(tmp_path / "maps"),
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    gpd.GeoDataFrame(geometry=[box(-123.12, 48.08, -123.10, 48.12)], crs=4326).to_file(land)
    request = service.ViewshedRequest(config=config, stages=("build-land-cells",), run_id="real")
    service.process(request)
    assert service.process(replace(request, resume=True)).skipped
    original_bytes = land_cells.read_bytes()
    manifest_path = tmp_path / "work" / "manifests" / "real.json"
    original_manifest = manifest_path.read_bytes()
    gpd.GeoDataFrame(geometry=[box(-123.10, 48.08, -123.08, 48.12)], crs=4326).to_file(land)
    # The ordinary producer's existing-cache path accepts this stale artifact.
    build_land_cells_for_config(config)
    assert land_cells.read_bytes() == original_bytes
    for force in (False, True):
        with pytest.raises(ValueError, match="input provenance"):
            service.process(replace(request, resume=True, force=force))
    assert manifest_path.read_bytes() == original_manifest
    assert land_cells.read_bytes() == original_bytes
    # The documented recovery is explicit stage rebuilding followed by a new ID.
    run_stage(config, StageInvocation("build-land-cells", overwrite=True))
    assert land_cells.read_bytes() != original_bytes
    refreshed = replace(request, run_id="rebuilt")
    service.process(refreshed)
    assert service.process(replace(refreshed, resume=True)).skipped


@pytest.fixture
def distance_fixture(tmp_path: Path, monkeypatch):
    source = h3.latlng_to_cell(48.1, -123.1, 7)
    sources = sorted(h3.grid_disk(source, 1))[:2]
    lookup = tmp_path / "lookup.parquet"
    frame = pl.DataFrame(
        {
            "source_h3": sources,
            "target_h3": [source, source],
            "distance_km": [2.0, 2.0],
            "source_type": ["land", "land"],
        }
    )
    frame.write_parquet(lookup)
    paths = DistanceWeightPaths(
        tmp_path,
        tmp_path / "partitions",
        tmp_path / "final.parquet",
        tmp_path / "target.parquet",
        tmp_path / "source.parquet",
        tmp_path / "manifest.csv",
    )
    runtime = SimpleNamespace(
        raw_config=load_app_config("configs/salish_sea.yaml").raw_config,
        viewshed_max_distance_km=30.0,
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(compute, "distance_weight_paths", lambda *_args: paths)
    from viewshed_toolkit.pipeline.finalize import final_artifacts

    artifacts = SimpleNamespace(
        source_target_lookup=lookup,
        weights_path=lambda *_args, **_kwargs: paths.final_weights_path,
    )
    monkeypatch.setattr(compute, "final_artifact_paths", lambda _path: artifacts)
    monkeypatch.setattr(final_artifacts, "final_artifact_paths", lambda _path: artifacts)
    monkeypatch.setattr(final_artifacts, "load_yaml", lambda _path: (runtime.raw_config, tmp_path))
    monkeypatch.setattr(
        "viewshed_toolkit.pipeline.contracts.lineage.final_artifact_paths_from_raw",
        lambda *_args: artifacts,
    )
    config = DistanceWeightConfig(selected_model="exponential", exponential_lambda_km=8.0)

    def build(cfg=config, **kwargs):
        return compute.build_distance_weight_partitions_from_lookup(
            runtime, cfg, lookup_path=lookup, progress_every=0, **kwargs
        )

    return SimpleNamespace(
        build=build, config=config, lookup=lookup, frame=frame, paths=paths, runtime=runtime
    )


def _partition(manifest) -> Path:
    return Path(manifest.iloc[0]["distance_weight_partition"])


def test_distance_reuse_tracks_curve_cutoff_and_lookup(distance_fixture):
    fixture = distance_fixture
    first = fixture.build()
    assert fixture.build().status.tolist() == ["skipped_existing"]
    different_curve = fixture.build(replace(fixture.config, exponential_lambda_km=2.0))
    assert _partition(different_curve) != _partition(first)
    assert pl.read_parquet(_partition(different_curve))["weight_distance"][0] == pytest.approx(
        0.36787944
    )
    different_cutoff = fixture.build(replace(fixture.config, hard_cutoff_km=1.0))
    assert pl.read_parquet(_partition(different_cutoff))["weight_distance"].to_list() == [0.0, 0.0]
    fixture.frame.with_columns(pl.lit(4.0).alias("distance_km")).write_parquet(fixture.lookup)
    changed_input = fixture.build()
    assert _partition(changed_input) != _partition(first)
    assert pl.read_parquet(_partition(changed_input))["distance_km"].to_list() == [4.0, 4.0]


def test_distance_source_slices_cannot_reuse_a_different_chunk(distance_fixture):
    fixture = distance_fixture
    full = fixture.build()
    limited = fixture.build(start=1, limit=1)
    assert _partition(full) != _partition(limited)
    assert limited.iloc[0]["unique_source_h3"] == limited.iloc[0]["n_source_cells"] == 1
    assert pl.read_parquet(_partition(limited))["source_h3"].to_list() == [
        fixture.frame["source_h3"][1]
    ]
    assert fixture.build(start=1, limit=1).status.tolist() == ["skipped_existing"]
    assert fixture.build().status.tolist() == ["skipped_existing"]


def test_distance_corrupt_and_unverified_outputs_are_rebuilt(distance_fixture):
    fixture = distance_fixture
    path = _partition(fixture.build())
    path.write_bytes(b"broken parquet")
    assert fixture.build().status.tolist() == ["ok"]
    assert pl.read_parquet(path).height == 2
    path.with_suffix(".parquet.json").unlink()
    assert fixture.build().status.tolist() == ["ok"]
    path.with_suffix(".parquet.json").write_text("[]")
    assert fixture.build().status.tolist() == ["ok"]


def test_distance_failed_overwrite_retains_previous_partition(distance_fixture, monkeypatch):
    fixture = distance_fixture
    path = _partition(fixture.build())
    before = path.read_bytes()
    unrelated = fixture.paths.partitioned_dir / "unrelated.txt"
    unrelated.write_text("keep")

    def fail_write(self, path, **_kwargs):
        Path(path).write_bytes(b"partial write")
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", fail_write)
    with pytest.raises(RuntimeError, match="disk write failed"):
        fixture.build(replace(fixture.config, overwrite=True))
    assert path.read_bytes() == before
    assert unrelated.read_text() == "keep"
    assert not list(fixture.paths.partitioned_dir.glob("*.tmp"))


def test_distance_combine_revalidates_partitions_and_refreshes_stale_final(
    distance_fixture, monkeypatch
):
    fixture = distance_fixture
    writes = []

    original_materialize = compute.materialize_distance_weights

    def materialize(*args, **kwargs):
        writes.append(kwargs["overwrite"])
        assert kwargs["overwrite"]
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(compute, "materialize_distance_weights", materialize)
    fixture.build()
    compute.combine_distance_weight_partitions(fixture.runtime, fixture.config)
    compute.combine_distance_weight_partitions(fixture.runtime, fixture.config)
    assert len(writes) == 1
    changed_config = replace(fixture.config, exponential_lambda_km=2.0)
    with pytest.raises(ValueError, match="Stale, corrupt, or unverified"):
        compute.combine_distance_weight_partitions(fixture.runtime, changed_config)
    fixture.build(changed_config)
    compute.combine_distance_weight_partitions(fixture.runtime, changed_config)
    assert len(writes) == 2
    assert pl.read_parquet(fixture.paths.final_weights_path)["weight_distance"][0] == pytest.approx(
        0.36787944
    )


def test_distance_failed_new_selection_does_not_publish_partial_manifest(
    distance_fixture, monkeypatch
):
    fixture = distance_fixture
    fixture.build()
    before = fixture.paths.manifest_path.read_bytes()
    sink = compute.atomic_sink_parquet
    writes = 0

    def fail_second_chunk(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("second chunk failed")
        return sink(*args, **kwargs)

    monkeypatch.setattr(compute, "atomic_sink_parquet", fail_second_chunk)
    with pytest.raises(RuntimeError, match="second chunk failed"):
        fixture.build(replace(fixture.config, source_chunk_size=1))
    assert fixture.paths.manifest_path.read_bytes() == before


def test_distance_missing_manifest_does_not_glob_different_models(distance_fixture):
    fixture = distance_fixture
    fixture.build()
    fixture.build(replace(fixture.config, exponential_lambda_km=2.0))
    fixture.paths.manifest_path.unlink()
    with pytest.raises(FileNotFoundError, match="manifest missing"):
        compute._partition_files_from_manifest_or_glob(fixture.paths)


@pytest.mark.parametrize("by", ["target", "source"])
@pytest.mark.parametrize("changed", ["model", "lookup", "partition", "metadata"])
def test_public_distance_aggregation_requires_current_verified_partitions(tmp_path, by, changed):
    from viewshed_toolkit.pipeline.api import stages
    from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths

    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    raw["paths"].update(
        output_dir=str(tmp_path / "work"),
        final_output_dir=str(tmp_path / "final"),
        map_dir=str(tmp_path / "maps"),
    )
    raw["distance_weight"].update(
        selected_model="exponential", exponential_lambda_km=8.0, overwrite=True
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    lookup = final_artifact_paths(config).source_target_lookup
    lookup.parent.mkdir(parents=True, exist_ok=True)
    target = h3.latlng_to_cell(48.1, -123.1, 7)
    sources = sorted(h3.grid_disk(target, 1))[:2]
    frame = pl.DataFrame(
        {
            "source_h3": sources,
            "target_h3": [target, target],
            "distance_km": [2.0, 2.0],
            "source_type": ["land", "land"],
        }
    )
    frame.write_parquet(lookup)
    built = stages.build_distance_weights(config, source_type="land")
    summary = stages.aggregate_distance_weights(config, source_type="land", by=by)
    output = Path(summary["path"])
    original_output = output.read_bytes()
    assert pl.read_parquet(output)["weight_distance_mean"].to_list() == pytest.approx(
        [0.7788008] * (1 if by == "target" else 2)
    )
    expected_weight = 0.7788008
    if changed == "model":
        raw["distance_weight"]["exponential_lambda_km"] = 2.0
        config.write_text(yaml.safe_dump(raw))
        expected_weight = 0.36787944
    elif changed == "lookup":
        frame.with_columns(pl.lit(4.0).alias("distance_km")).write_parquet(lookup)
        expected_weight = 0.60653066
    elif changed == "partition":
        _partition(built["partition_manifest"]).write_bytes(b"corrupt partition")
    else:
        _partition(built["partition_manifest"]).with_suffix(".parquet.json").write_text("[]")
    with pytest.raises(ValueError, match="Stale, corrupt, or unverified"):
        stages.aggregate_distance_weights(config, source_type="land", by=by)
    assert output.read_bytes() == original_output
    # A real rebuild with an exact subset also proves aggregation consumes only
    # the latest completed manifest, not other retained historical partitions.
    stages.build_distance_weights(config, source_type="land", start=1, limit=1)
    refreshed = stages.aggregate_distance_weights(config, source_type="land", by=by)
    result = pl.read_parquet(refreshed["path"])
    assert result.height == 1
    assert result["weight_distance_mean"][0] == pytest.approx(expected_weight)
    if by == "target":
        assert result["n_distance_weighted_sources"][0] == 1
    else:
        assert result["source_h3"].to_list() == [sources[1]]


@pytest.mark.parametrize("command", ["build-distance-weights", "distance-weight"])
def test_distance_cli_forwards_source_bounds(monkeypatch, command):
    parser = argparse.ArgumentParser()
    cli.register_commands(parser.add_subparsers(), default_config="config.yaml")
    calls = []
    monkeypatch.setattr(
        cli.pipeline, "build_distance_weights", lambda *args, **kwargs: calls.append(kwargs) or {}
    )
    args = parser.parse_args([command, "--limit", "1", "--start", "500"])
    args.func(args)
    assert calls[0]["limit"] == 1
    assert calls[0]["start"] == 500
