"""Review regressions: observed values, source completion, lineage and fresh production."""

import hashlib

import geopandas as gpd
import polars as pl
import pytest
import yaml

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import load_app_config, run_component_stage
from viewshed_toolkit.pipeline.api.pipeline import execute_stages
from viewshed_toolkit.pipeline.config import apply_source_type_policy
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths_from_raw
from viewshed_toolkit.pipeline.providers.base import Asset, FileProvider
from viewshed_toolkit.pipeline.weights.canopy_visibility import compose_dual_surface_artifacts
from viewshed_toolkit.pipeline.weights.terrain import gdal


def test_provider_accepts_raw_source_sha256(tmp_path):
    config = coastal_fixture(tmp_path)
    source = config.parent / "dem.tif"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    provider = FileProvider()
    asset = Asset("dem", str(source), digest)
    output = provider.download([asset], tmp_path / "downloads").paths[0]
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    assert provider.download([asset], tmp_path / "downloads").paths == (output,)


@pytest.mark.parametrize("change", ["asset", "version", "extent"])
def test_prepare_rejects_stale_acquisition_request(tmp_path, change):
    config = coastal_fixture(tmp_path)
    run_component_stage(config, "download-dem")
    raw = yaml.safe_load(config.read_text())
    if change == "asset":
        raw["datasets"]["dem"]["assets"] = [str(tmp_path / "chm.tif")]
    elif change == "version":
        raw["datasets"]["dem"]["version"] = "new"
    else:
        raw["viewshed"]["aoi_margin_m"] += 100
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="Stale dem download manifest"):
        run_component_stage(config, "prepare-dem")
    assert not load_app_config(config).paths.regional_dem_path.exists()


def test_water_identity_and_cached_domains_follow_changed_geometry(tmp_path):
    config = coastal_fixture(tmp_path)
    run_component_stage(config, "build-source-target-lookup")
    app = apply_source_type_policy(load_app_config(config), "water")
    before = gdal.expected_partition_metadata(app)
    domain = gdal._water_terrain_domains_for_app(app)
    assert gdal._water_terrain_domains_for_app(app) is domain
    land = gpd.read_file(app.paths.land_polygon_path)
    land.geometry = land.geometry.translate(xoff=0.001)
    land.to_file(app.paths.land_polygon_path, driver="GeoJSON")
    assert gdal.expected_partition_metadata(app) != before
    updated = gdal._water_terrain_domains_for_app(app)
    assert not updated.land_domain.equals(domain.land_domain)


def composition_fixture(tmp_path, weight=0.4, sources=("s",)):
    raw = {
        "run": {"version": "review"},
        "h3": {"source_resolution": 8, "target_resolution": 8},
        "paths": {"output_dir": str(tmp_path / "out")},
    }
    paths = final_artifact_paths_from_raw(raw, tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": list(sources),
            "target_h3": ["t"] * len(sources),
            "source_type": ["land"] * len(sources),
        }
    ).write_parquet(paths.source_target_lookup)
    bare, canopy = tmp_path / "bare.parquet", tmp_path / "canopy.parquet"
    pl.DataFrame({"source_h3": ["s"], "target_h3": ["t"], "weight_terrain": [0.8]}).write_parquet(
        bare
    )
    pl.DataFrame(
        {"source_h3": ["s"], "target_h3": ["t"], "weight_terrain": [weight]}
    ).write_parquet(canopy)
    return dict(
        lookup_path=paths.source_target_lookup,
        bare_earth_partition_paths=[bare],
        canopy_partition_paths=[canopy],
        paths=paths,
        raw_config=raw,
        config_dir=tmp_path,
        bare_config_hash="bare",
        canopy_config_hash="canopy",
        bare_completed_sources=["s"],
        canopy_completed_sources=["s"],
    )


@pytest.mark.parametrize("weight", [None, "bad", float("nan"), float("inf"), -0.1, 1.1])
def test_production_rejects_invalid_observed_kernel(tmp_path, weight):
    kwargs = composition_fixture(tmp_path, weight)
    with pytest.raises(ValueError, match="Invalid observed pair kernel"):
        compose_dual_surface_artifacts(**kwargs)
    assert not kwargs["paths"].dual_surface_factors.exists()


def test_production_rejects_incomplete_source_execution(tmp_path):
    kwargs = composition_fixture(tmp_path, sources=("s", "missing"))
    with pytest.raises(ValueError, match="Incomplete paired LOS"):
        compose_dual_surface_artifacts(**kwargs)
    assert not kwargs["paths"].terrain_weights.exists()


def test_fresh_production_finalizes_all_four_outputs_and_preserves_old_set_on_failure(
    tmp_path, monkeypatch
):
    pytest.importorskip("osgeo.gdal")
    from viewshed_toolkit.pipeline.finalize import final_artifacts

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
    paths = final_artifact_paths_from_raw(app.raw_config, config.parent)
    outputs = final_artifacts.materialize_static_viewability_outputs(config)
    assert len(outputs) == 4
    for role in ("land", "water"):
        compact = pl.read_parquet(outputs[f"{role}_static_weights"]).sort("source_h3", "target_h3")
        geometry = pl.read_parquet(outputs[f"{role}_observation_geometry"]).sort(
            "source_h3", "target_h3"
        )
        assert compact.height == geometry.height > 0
        assert geometry["line_of_sight_support"].null_count() == 0
        assert geometry["distance_adjusted_viewability"].to_list() == pytest.approx(
            compact["weight_static_viewability"].to_list()
        )
    originals = {path: path.read_bytes() for path in outputs.values()}
    real = final_artifacts.materialize_observation_geometry_output

    def fail_water(*args, **kwargs):
        if kwargs["source_type"] == "water":
            raise RuntimeError("geometry staging failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(final_artifacts, "materialize_observation_geometry_output", fail_water)
    with pytest.raises(RuntimeError, match="geometry staging failed"):
        final_artifacts.materialize_static_viewability_outputs(config, overwrite=True)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not list(paths.final_output_dir.rglob("*.staged"))
