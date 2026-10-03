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


def test_land_metadata_changes_with_lookup_content(tmp_path):
    config = coastal_fixture(tmp_path)
    run_component_stage(config, "build-source-target-lookup")
    app = load_app_config(config)
    path = gdal._area_lookup_path_for_app(app)
    before = gdal.expected_partition_metadata(app)
    pl.read_parquet(path).slice(1).write_parquet(path)
    assert gdal.expected_partition_metadata(app) != before


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
