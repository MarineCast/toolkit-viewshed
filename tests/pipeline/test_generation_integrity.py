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
