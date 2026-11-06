import json
import pytest
from viewshed_toolkit.pipeline.contracts.study import load_study_membership


def test_exact_native_roles_and_read_only_loading(tmp_path, study_fixture):
    path, config, manifest, (a, b) = study_fixture(tmp_path)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    assert load_study_membership(path, resolution=7)["cells"] == (a,)
    assert set(load_study_membership(path, resolution=7, role="land_source")["cells"]) == {a, b}
    assert set(load_study_membership(path, resolution=7, role="water_source")["cells"]) == {a, b}
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("name", ["mask.parquet", "manifest.json", "water_reporting7.txt"])
def test_pinned_artifact_tamper_rejected(tmp_path, name, study_fixture):
    path, _, _, _ = study_fixture(tmp_path)
    with (tmp_path / "Data" / name).open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="SHA256"):
        load_study_membership(path, resolution=7)


def test_source_extent_tamper_rejected(tmp_path, study_fixture):
    path, _, _, _ = study_fixture(tmp_path)
    (tmp_path / "Data/extent.parquet").write_bytes(b"bad")
    with pytest.raises(ValueError, match="SHA256"):
        load_study_membership(path, resolution=7, role="land_source")


def test_pending_policy_rejected(tmp_path, study_fixture):
    path, config, _, _ = study_fixture(tmp_path)
    config["domain"]["selection_policy"]["mask_status"] = "pending_source_qualified_build"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="validated coastal"):
        load_study_membership(path, resolution=7)


def test_no_outside_data_root_fallback(tmp_path, study_fixture):
    path, config, _, _ = study_fixture(tmp_path)
    config["grid_registry"]["mask_relative_path"] = "../outside.parquet"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="escapes"):
        load_study_membership(path, resolution=7)


def test_runtime_targets_and_mixed_source_roles_use_actual_registry(tmp_path, study_fixture):
    import yaml
    from viewshed_toolkit import load_app_config
    from viewshed_toolkit.pipeline.config.distance import (
        load_distance_runtime,
        load_distance_weight_config,
    )
    from viewshed_toolkit.pipeline.prepare.area.config import _lookup_config
    from viewshed_toolkit.pipeline.prepare.area.universe import _build_source_universe
    from viewshed_toolkit.pipeline.prepare.area.target_cells import build_target_cells
    import geopandas as gpd

    study, _, _, (a, b) = study_fixture(tmp_path)
    config = tmp_path / "viewshed.yaml"
    output = tmp_path / "outputs"
    config.write_text(
        yaml.safe_dump(
            {
                "run": {"name": "synthetic-shared", "version": "test"},
                "region": {
                    "bbox_wgs84": {
                        "min_lon": -125.0,
                        "min_lat": 47.0,
                        "max_lon": -122.0,
                        "max_lat": 50.0,
                    }
                },
                "h3": {"source_resolution": 7, "target_resolution": 7},
                "viewshed": {"dem_resolution_m": 30},
                "shared_study": {"config_path": study.name},
                "paths": {"output_dir": str(output), "land_h3_path": str(output / "land.parquet")},
            }
        )
    )
    app = load_app_config(config)
    assert not output.exists()
    runtime = load_distance_runtime(config)
    decay = load_distance_weight_config(app.raw_config)
    settings = _lookup_config(app.raw_config, runtime, decay)
    universe = _build_source_universe(
        config, runtime=runtime, cfg=decay, lookup_cfg=settings, overwrite=True
    )
    assert universe.target_cells == {a}
    assert set(universe.land_source_cells) == {a, b}
    assert set(universe.water_source_cells) == {a, b}
    target = build_target_cells(app, overwrite=True)
    assert gpd.read_parquet(target).h3_cell.tolist() == [a]
    from viewshed_toolkit.pipeline.prepare.area.land import build_land_cells_for_config

    stored = gpd.read_parquet(output / "land.parquet")
    stored.geometry = stored.geometry.translate(xoff=0.001)
    stored.to_parquet(output / "land.parquet", index=False)
    with pytest.raises(ValueError, match="cache identity"):
        build_land_cells_for_config(config, overwrite=False)
    with pytest.raises(ValueError, match="native R7"):
        build_land_cells_for_config(config, overwrite=True, h3_resolution=6)
