"""Generation reuse must prove the evaluated universe and artifact integrity."""

import json

import polars as pl
import pytest
import yaml

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import load_app_config, run_component_stage
from viewshed_toolkit.pipeline.api.pipeline import execute_stages
from viewshed_toolkit.pipeline.finalize.final_artifacts import (
    materialize_static_viewability_outputs,
    refresh_static_artifact_metadata,
)
from viewshed_toolkit.pipeline.weights.terrain import gdal


def durable_snapshot(outputs):
    from viewshed_toolkit.pipeline.contracts.cleanup import metadata_sidecars_for

    paths = [*outputs.values(), outputs["land_static_weights"].parent / "viewshed-generation.json"]
    paths += [sidecar for path in outputs.values() for sidecar in metadata_sidecars_for(path)]
    return {path: path.read_bytes() for path in paths if path.exists()}


@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize(
    "mutation",
    ["observer", "target", "distance", "clearance", "dem", "chm", "lookup", "lookup_keys"],
)
def test_finalization_rejects_stale_producer_lineage(generation, mutation, overwrite):
    import rasterio

    config, outputs = generation
    before = durable_snapshot(outputs)
    app = load_app_config(config)
    raw = yaml.safe_load(config.read_text())
    if mutation in {"observer", "target", "clearance"}:
        field = {
            "observer": "observer_eye_height_m",
            "target": "target_height_m",
            "clearance": "observer_canopy_clearance_radius_m",
        }[mutation]
        raw["viewshed"][field] = raw["viewshed"].get(field, 0) + 1
        config.write_text(yaml.safe_dump(raw))
    elif mutation == "distance":
        raw.setdefault("distance_weight", {})["logistic_d50_km"] = 0.5
        config.write_text(yaml.safe_dump(raw))
    elif mutation in {"dem", "chm"}:
        path = app.paths.regional_dem_path if mutation == "dem" else app.paths.canopy_height_path
        with rasterio.open(path, "r+") as raster:
            values = raster.read(1)
            values[0, 0] += 1
            raster.write(values, 1)
    else:
        path = gdal._area_lookup_path_for_app(app)
        frame = pl.read_parquet(path)
        # Same count and columns: this must fail content lineage, not a shape gate.
        if mutation == "lookup_keys":
            frame = frame.with_columns(
                pl.when(pl.col("target_h3") == frame["target_h3"][0])
                .then(pl.lit("replacement-target"))
                .otherwise(pl.col("target_h3"))
                .alias("target_h3")
            )
        else:
            frame = frame.with_columns((pl.col("distance_km") + 0.01).alias("distance_km"))
        import pyarrow.parquet as pq

        metadata = pq.read_metadata(path).metadata
        frame.write_parquet(
            path,
            metadata={
                k.decode(): v.decode()
                for k, v in (metadata or {}).items()
                if k.startswith(b"orcacast.")
            },
        )
    with pytest.raises(ValueError, match=r"producer|lineage|stale|rebuild|mismatch|changed"):
        materialize_static_viewability_outputs(config, overwrite=overwrite)
    assert durable_snapshot(outputs) == before


def test_same_science_refinalization_succeeds(generation):
    config, outputs = generation
    before = pl.read_parquet(outputs["land_static_weights"])
    materialize_static_viewability_outputs(config, overwrite=True)
    assert (
        pl.read_parquet(outputs["land_static_weights"])
        .sort("source_h3", "target_h3")
        .equals(before.sort("source_h3", "target_h3"))
    )


@pytest.mark.parametrize("kind", ["terrain", "distance", "vegetation", "clear_sky", "dual_surface"])
@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_finalize_rejects_missing_or_tampered_producer_receipt(generation, kind, mutation):
    from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
    from viewshed_toolkit.pipeline.contracts.lineage import producer_receipt_path

    config, outputs = generation
    paths = final_artifact_paths(config)
    product = {
        "terrain": paths.terrain_weights,
        "distance": paths.distance_weights,
        "vegetation": paths.vegetation_weights,
        "clear_sky": paths.source_target_clear_sky,
        "dual_surface": paths.dual_surface_factors,
    }[kind]
    receipt = producer_receipt_path(product)
    before = durable_snapshot(outputs)
    if mutation == "missing":
        receipt.unlink()
    else:
        payload = json.loads(receipt.read_text())
        payload["producer_revision"] = "tampered"
        receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=r"producer lineage.*rebuild"):
        materialize_static_viewability_outputs(config, overwrite=True)
    assert durable_snapshot(outputs) == before


def test_presentation_only_change_allows_replacement(generation):
    config, outputs = generation
    raw = yaml.safe_load(config.read_text())
    raw["static_maps"] = {"colormap_name": "presentation-only"}
    config.write_text(yaml.safe_dump(raw))
    before = pl.read_parquet(outputs["land_static_weights"]).sort("source_h3", "target_h3")
    materialize_static_viewability_outputs(config, overwrite=True)
    assert (
        pl.read_parquet(outputs["land_static_weights"])
        .sort("source_h3", "target_h3")
        .equals(before)
    )


def test_partition_snapshot_avoids_repeated_lookup_hash_and_next_run_invalidates(
    generation, monkeypatch
):
    from dataclasses import replace

    from viewshed_toolkit.pipeline.contracts import generation as receipts

    config, _ = generation
    app = load_app_config(config)
    lookup = gdal._area_lookup_path_for_app(app)
    original = receipts.byte_checksum
    calls = []

    def counted(path):
        if path == lookup:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(receipts, "byte_checksum", counted)
    # Baseline repeated requests without a stage snapshot perform whole-lookup hashing each time.
    for _ in range(5):
        gdal.expected_partition_metadata(app)
    assert len(calls) == 5
    calls.clear()
    snapshot = gdal.expected_partition_metadata(app)
    staged = replace(app, partition_metadata_snapshot=snapshot)
    for _ in range(5):
        assert gdal.expected_partition_metadata(staged) == snapshot
    assert len(calls) == 1
    pl.read_parquet(lookup).slice(1).write_parquet(lookup)
    assert gdal.expected_partition_metadata(staged, refresh=True) != snapshot
    assert gdal.expected_partition_metadata(load_app_config(config)) != snapshot


@pytest.fixture
def generation(tmp_path):
    config = coastal_fixture(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["paths"].update(
        final_visibility_path=str(tmp_path / "work/terrain/all.parquet"),
        partitioned_visibility_dir=str(tmp_path / "work/terrain/partitions"),
        manifest_path=str(tmp_path / "work/terrain/manifest.csv"),
    )
    config.write_text(yaml.safe_dump(raw))
    for stage in ("download-dem", "prepare-dem", "download-chm", "prepare-chm"):
        run_component_stage(config, stage)
    app = load_app_config(config)
    execute_stages(
        app,
        (
            "build-land-cells",
            "prepare-source-target-lookup",
            "build-distance-weights",
            "build-dual-surface-canopy-weights",
            "terrain-weight",
            "build-vegetation-path-weights",
            "finalize-viewshed-lookups",
        ),
    )
    return config, materialize_static_viewability_outputs(config)


@pytest.mark.parametrize("mutation", ["remove", "add", "replace", "empty"])
@pytest.mark.parametrize("empty_partition", [False, True])
def test_land_metadata_changes_with_lookup_content(tmp_path, mutation, empty_partition):
    config = coastal_fixture(tmp_path)
    run_component_stage(config, "build-source-target-lookup")
    app = load_app_config(config)
    path = gdal._area_lookup_path_for_app(app)
    before = gdal.expected_partition_metadata(app)
    partition = tmp_path / "cached.parquet"
    frame = pl.read_parquet(path)
    frame.head(0 if empty_partition else 1).write_parquet(partition)
    gdal._write_partition_metadata_sidecar(partition, before)
    assert gdal.partition_metadata_matches(partition, before)
    if mutation == "remove":
        changed = frame.slice(1)
    elif mutation == "add":
        changed = pl.concat(
            [frame, frame.head(1).with_columns(pl.lit("new-target").alias("target_h3"))]
        )
    elif mutation == "replace":
        changed = frame.with_columns(
            pl.when(pl.col("target_h3") == frame["target_h3"][0])
            .then(pl.lit("replacement-target"))
            .otherwise(pl.col("target_h3"))
            .alias("target_h3")
        )
    else:
        changed = frame.head(0)
    changed.write_parquet(path)
    current = gdal.expected_partition_metadata(app)
    assert current != before
    assert not gdal.partition_metadata_matches(partition, current)


def test_partition_checksum_rejects_mutated_cache(tmp_path):
    path = tmp_path / "part.parquet"
    expected = {"contract": "test"}
    pl.DataFrame({"value": [1]}).write_parquet(path)
    gdal._write_partition_metadata_sidecar(path, expected)
    assert gdal.partition_metadata_matches(path, expected)
    pl.DataFrame({"value": [2]}).write_parquet(path)
    assert not gdal.partition_metadata_matches(path, expected)


@pytest.mark.parametrize("mutation", ["value", "dtype", "role", "duplicate", "generation"])
def test_reuse_rejects_corrupt_geometry(generation, mutation):
    config, outputs = generation
    path = outputs["land_observation_geometry"]
    frame = pl.read_parquet(path)
    if mutation == "value":
        frame = frame.with_columns(
            (pl.col("distance_adjusted_viewability") + 0.01).alias("distance_adjusted_viewability")
        )
    elif mutation == "dtype":
        frame = frame.with_columns(pl.col("distance_adjusted_viewability").cast(pl.Float64))
    elif mutation == "role":
        frame = frame.with_columns(pl.lit("water").alias("source_type"))
    elif mutation == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    else:
        frame = frame.with_columns(pl.lit("old").alias("GENERATION_ID"))
    frame.write_parquet(path)
    with pytest.raises(ValueError, match=r"geometry|Geometry|generation|Generation"):
        materialize_static_viewability_outputs(config)


def test_metadata_refresh_rejects_scientific_relabeling(generation):
    config, _ = generation
    raw = yaml.safe_load(config.read_text())
    raw["viewshed"]["observer_eye_height_m"] += 1
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=r"scientific|Scientific"):
        refresh_static_artifact_metadata(config, source_type="land")


def test_local_unknown_canopy_vintage_is_not_default_2020(generation):
    _, outputs = generation
    vintages = json.loads(
        pl.read_parquet(outputs["land_observation_geometry"])["SOURCE_VINTAGES_JSON"][0]
    )
    assert vintages["canopy_product_year"] is None


def test_shared_generation_receipt_is_required(generation):
    config, outputs = generation
    receipt = outputs["land_static_weights"].parent / "viewshed-generation.json"
    assert receipt.exists()
    payload = json.loads(receipt.read_text())
    ids = {
        pl.read_parquet(outputs[role + "_observation_geometry"])["GENERATION_ID"][0]
        for role in ("land", "water")
    }
    assert ids == {payload["generation_id"]}
    receipt.unlink()
    with pytest.raises(ValueError, match=r"receipt|generation"):
        materialize_static_viewability_outputs(config)


def test_receipt_promotion_failure_restores_complete_previous_generation(generation, monkeypatch):
    from viewshed_toolkit.pipeline.finalize import final_artifacts

    config, outputs = generation
    receipt = outputs["land_static_weights"].parent / "viewshed-generation.json"
    files = [*outputs.values(), receipt]
    before = {path: path.read_bytes() for path in files}
    original = final_artifacts.validate_generation_receipt

    def fail_after_promotion(path, paths, *, config_hash):
        if path == receipt:
            raise ValueError("injected post-promotion receipt failure")
        return original(path, paths, config_hash=config_hash)

    monkeypatch.setattr(final_artifacts, "validate_generation_receipt", fail_after_promotion)
    with pytest.raises(ValueError, match="injected"):
        materialize_static_viewability_outputs(config, overwrite=True)
    assert {path: path.read_bytes() for path in files} == before


def test_coverage_is_part_of_identity_and_missing_input_is_partial(generation):
    config, outputs = generation
    receipt = outputs["land_static_weights"].parent / "viewshed-generation.json"
    previous = json.loads(receipt.read_text())["generation_id"]
    audit = receipt.parent / "components/analysis/input-coverage.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps(
            {"rasters": {"dem": {"missing_land_pixels": 0}, "chm": {"missing_land_pixels": 1}}}
        )
    )
    materialize_static_viewability_outputs(config, overwrite=True)
    current = json.loads(receipt.read_text())
    assert current["generation_id"] != previous
    assert pl.read_parquet(outputs["land_observation_geometry"])[
        "DATA_COVERAGE_STATE"
    ].unique().to_list() == ["partial"]


def test_presentation_metadata_refresh_preserves_scientific_identity(generation):
    config, outputs = generation
    raw = yaml.safe_load(config.read_text())
    raw["static_maps"] = {"colormap_name": "changed-presentation"}
    config.write_text(yaml.safe_dump(raw))
    before = outputs["land_static_weights"].read_bytes()
    refresh_static_artifact_metadata(config, source_type="land")
    assert outputs["land_static_weights"].read_bytes() == before
    materialize_static_viewability_outputs(config)


@pytest.mark.parametrize("name", ["regional_dem_path", "canopy_height_path"])
def test_reuse_rejects_changed_prepared_surfaces(generation, name):
    import rasterio

    config, _ = generation
    path = getattr(load_app_config(config).paths, name)
    with rasterio.open(path, "r+") as raster:
        values = raster.read(1)
        values[0, 0] += 1
        raster.write(values, 1)
    with pytest.raises(ValueError, match=r"source.*changed|inputs changed"):
        materialize_static_viewability_outputs(config)


def test_reuse_rejects_changed_coverage_audit(generation):
    config, outputs = generation
    audit = outputs["land_static_weights"].parent / "components/analysis/input-coverage.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps({"rasters": {"chm": {"missing_land_pixels": 1}}}))
    with pytest.raises(ValueError, match=r"coverage.*changed"):
        materialize_static_viewability_outputs(config)


@pytest.mark.parametrize(
    "state", ["distance_detection_state", "distance_weighted_los_state", "vegetation_state"]
)
def test_typed_validator_checks_all_value_state_pairs(generation, state):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from viewshed_toolkit.pipeline.contracts.generation import validate_observation_geometry
    from viewshed_toolkit.pipeline.finalize.final_artifacts import static_scientific_config_hash

    config, outputs = generation
    path = outputs["land_observation_geometry"]
    frame = pl.read_parquet(path)
    generation_id = frame["GENERATION_ID"][0]
    # Preserve embedded metadata so the scientific validator, rather than the
    # receipt checksum, must catch this inconsistent declared state.
    table = pq.read_table(path)
    metadata = {
        key: value
        for key, value in pq.read_metadata(path).metadata.items()
        if key.startswith(b"orcacast.")
    }
    table = table.set_column(
        table.schema.get_field_index(state), state, pa.array(["not_applicable"] * table.num_rows)
    )
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="state"):
        validate_observation_geometry(
            path,
            source_type="land",
            config_hash=static_scientific_config_hash(yaml.safe_load(config.read_text())),
            generation_id=generation_id,
            compact_path=outputs["land_static_weights"],
        )


def test_typed_validator_checks_compact_diagnostic_parity(generation):
    import pyarrow.parquet as pq

    from viewshed_toolkit.pipeline.contracts.generation import validate_observation_geometry
    from viewshed_toolkit.pipeline.finalize.final_artifacts import static_scientific_config_hash

    config, outputs = generation
    path = outputs["land_observation_geometry"]
    metadata = {
        k: v for k, v in pq.read_metadata(path).metadata.items() if k.startswith(b"orcacast.")
    }
    frame = pl.read_parquet(path)
    frame = frame.with_columns(
        pl.when(pl.col("distance_detection_weight") > 0)
        .then(pl.lit(0.123, dtype=pl.Float32))
        .otherwise(pl.col("distance_detection_weight"))
        .alias("distance_detection_weight")
    )
    pq.write_table(frame.to_arrow().replace_schema_metadata(metadata), path)
    with pytest.raises(ValueError, match=r"compact.*values|values.*compact"):
        validate_observation_geometry(
            path,
            source_type="land",
            config_hash=static_scientific_config_hash(yaml.safe_load(config.read_text())),
            generation_id=frame["GENERATION_ID"][0],
            compact_path=outputs["land_static_weights"],
        )


@pytest.mark.parametrize("entry", ["standalone", "paired"])
def test_refinalization_failure_preserves_receipt_and_sidecars(generation, monkeypatch, entry):
    from viewshed_toolkit.pipeline.finalize import final_artifacts as finalizer

    config, outputs = generation
    before = durable_snapshot(outputs)

    def fail(*args, **kwargs):
        raise RuntimeError("publication failure")

    monkeypatch.setattr(finalizer, "_write_static_artifact_metadata", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        if entry == "standalone":
            finalizer.materialize_static_viewability_output(
                config, source_type="land", overwrite=True
            )
        else:
            finalizer.materialize_static_viewability_outputs(config, overwrite=True)
    assert durable_snapshot(outputs) == before


@pytest.mark.parametrize("role", ["land", "water"])
def test_standalone_overwrite_rejects_stale_science(generation, role):
    from viewshed_toolkit.pipeline.finalize.final_artifacts import (
        materialize_static_viewability_output,
    )

    config, outputs = generation
    before = durable_snapshot(outputs)
    raw = yaml.safe_load(config.read_text())
    raw.setdefault("distance_weight", {})["logistic_d50_km"] = 0.5
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=r"producer lineage.*rebuild"):
        materialize_static_viewability_output(config, source_type=role, overwrite=True)
    assert durable_snapshot(outputs) == before


def test_stage_rejects_lookup_change_during_execution(generation, monkeypatch):
    from viewshed_toolkit.pipeline.weights.terrain import runner

    config, outputs = generation
    app = load_app_config(config)
    before = durable_snapshot(outputs)

    def mutate(current, **kwargs):
        lookup = gdal._area_lookup_path_for_app(current)
        pl.read_parquet(lookup).slice(1).write_parquet(lookup)
        return None

    monkeypatch.setattr(runner, "_run_source_cells", mutate)
    with pytest.raises(ValueError, match="changed"):
        runner.run_source_cells(app)
    assert durable_snapshot(outputs) == before


def test_refinalization_succeeds_after_actual_upstream_rebuild(generation):
    from dataclasses import replace

    from viewshed_toolkit.pipeline.api.registry import invocations_for_stage
    from viewshed_toolkit.pipeline.api.stages import run_stage

    config, outputs = generation
    receipt = outputs["land_static_weights"].parent / "viewshed-generation.json"
    old_id = json.loads(receipt.read_text())["generation_id"]
    raw = yaml.safe_load(config.read_text())
    raw["viewshed"]["observer_eye_height_m"] += 1
    config.write_text(yaml.safe_dump(raw))
    app = load_app_config(config)
    for stage in (
        "build-dual-surface-canopy-weights",
        "terrain-weight",
        "build-vegetation-path-weights",
    ):
        for invocation in invocations_for_stage(stage):
            run_stage(app, replace(invocation, overwrite=True))
    materialize_static_viewability_outputs(config, overwrite=True)
    assert json.loads(receipt.read_text())["generation_id"] != old_id


@pytest.mark.parametrize("role", ["land", "water"])
def test_view_score_report_failure_preserves_previous_publication(generation, monkeypatch, role):
    from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
    from viewshed_toolkit.pipeline.weights import view_score

    config, _outputs = generation
    paths = final_artifact_paths(config)
    score = paths.land_view_score if role == "land" else paths.ocean_view_score
    outputs = [score, score.with_suffix(score.suffix + ".join_report.json")]
    if role == "water":
        outputs.append(paths.ocean_physical_weights)
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"previous durable publication")

    def fail(*args, **kwargs):
        raise RuntimeError("report failure")

    monkeypatch.setattr(view_score, "_write_join_report", fail)
    with pytest.raises(RuntimeError, match="report failure"):
        view_score.finalize_view_score(config, source_type=role, overwrite=True)
    assert all(path.read_bytes() == b"previous durable publication" for path in outputs)
