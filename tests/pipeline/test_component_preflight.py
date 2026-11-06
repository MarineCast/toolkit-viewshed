"""Header-only preflight cannot acquire data or create runtime artifacts."""

import argparse
import json
from pathlib import Path

import pytest
import yaml

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import load_app_config, plan_components
from viewshed_toolkit.pipeline.api import regional
from viewshed_toolkit.pipeline.api.components import component_run_context
from viewshed_toolkit.pipeline.cli import components as cli


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_plan_has_no_filesystem_writes_or_execution(tmp_path, monkeypatch):
    config = coastal_fixture(tmp_path)
    before = _snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only plan attempted runtime initialization or execution")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(cli, "run_components", forbidden)
    report = plan_components(config, target="static")
    assert report["read_only"]
    assert report["ready"]
    assert "export-maps" not in report["stages"]
    assert {item["dataset"] for item in report["planned_acquisition"]} == {"dem", "chm"}
    assert "DEM" in report["missing_inputs"]
    assert report["inputs"]["land"]["readable"]
    assert report["inputs"]["water"]["crs"] == "EPSG:4326"
    assert report["workload"]["eta_seconds"] is None
    assert before == _snapshot(tmp_path)


def test_water_plan_is_raster_free_and_requires_no_gdal(tmp_path, monkeypatch):
    config = coastal_fixture(tmp_path)

    def forbidden(name):
        pytest.fail(f"Water preflight attempted optional native import: {name}")

    monkeypatch.setattr(regional.importlib, "import_module", forbidden)
    original_header = regional._input_header

    def no_raster(path, *, raster, app):
        assert not raster
        return original_header(path, raster=raster, app=app)

    monkeypatch.setattr(regional, "_input_header", no_raster)
    report = plan_components(config, source_type="water")
    assert report["ready"]
    assert report["backend"]["los_mode"] == "raster_free"
    assert report["planned_acquisition"] == []
    assert not any(stage.startswith(("download-", "prepare-")) for stage in report["stages"])


def test_distance_plan_needs_no_native_rasters(tmp_path, monkeypatch):
    config = coastal_fixture(tmp_path)
    monkeypatch.setattr(
        regional.importlib,
        "import_module",
        lambda name: pytest.fail("Distance plan imported native GDAL"),
    )
    report = plan_components(config, target="distance")
    assert report["ready"]
    assert not report["backend"]["required"]
    assert report["planned_acquisition"] == []


def test_preflight_separates_missing_geometry_from_planned_rasters(tmp_path):
    config = coastal_fixture(tmp_path)
    app = load_app_config(config)
    app.paths.water_polygon_path.unlink()
    report = plan_components(app, target="dem")
    assert not report["ready"]
    assert {"water", "DEM"}.issubset(report["missing_inputs"])
    assert any(
        issue["input"] == "water" and issue["severity"] == "error" for issue in report["issues"]
    )
    assert not any(
        issue["input"] == "dem" and issue["severity"] == "error" for issue in report["issues"]
    )


def test_preflight_reports_unreadable_geometry_and_present_unverified_cache(tmp_path):
    config = coastal_fixture(tmp_path)
    app = load_app_config(config)
    app.paths.water_polygon_path.write_text("not parquet")
    app.paths.land_h3_path.parent.mkdir()
    app.paths.land_h3_path.write_text("not a validated cache")
    report = plan_components(app, source_type="water")
    assert not report["ready"]
    assert report["inputs"]["water"]["status"] == "unreadable"
    source = next(item for item in report["caches"] if item["product"] == "source_cells")
    assert source["reuse_status"] == "present_but_unverified"
    assert report["workload"]["source_table_rows_unverified"] is None


def test_preflight_manifest_identity_matches_execution_and_detects_conflicts(tmp_path):
    config = coastal_fixture(tmp_path)
    app = load_app_config(config)
    plan, manifest, identity = component_run_context(
        app, target="static", source_type="water", run_id="run"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"identity": identity}))
    matching = plan_components(app, target="static", source_type="water", run_id="run")
    assert matching["run"]["identity"] == identity
    assert matching["stages"] == list(plan)
    assert matching["ready"]
    conflict = plan_components(app, target="all", source_type="water", run_id="run")
    assert conflict["run"]["conflict"]
    assert not conflict["ready"]
    allowed = plan_components(app, target="all", source_type="water", run_id="run", overwrite=True)
    assert not allowed["run"]["conflict"]
    assert allowed["ready"]


def test_preflight_requires_python_gdal_for_preparation_even_if_cli_available(
    tmp_path, monkeypatch
):
    config = coastal_fixture(tmp_path)

    def missing(name):
        raise ImportError("no Python GDAL")

    monkeypatch.setattr(regional.importlib, "import_module", missing)
    monkeypatch.setattr(regional.shutil, "which", lambda name: "/synthetic/gdal_viewshed")
    report = plan_components(config, target="dem")
    assert report["backend"]["los_mode"] == "cli_fallback"
    assert not report["backend"]["ready"]
    assert not report["ready"]


def test_preflight_reads_raster_grid_without_scanning_bands(tmp_path):
    config = coastal_fixture(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["paths"]["regional_dem_path"] = str(tmp_path / "dem.tif")
    config.write_text(yaml.safe_dump(raw))
    report = plan_components(config, target="dem")
    header = report["inputs"]["DEM"]
    assert header["readable"]
    assert (header["width"], header["height"], header["bands"]) == (100, 100, 1)
    assert header["configured_grid_matches"]
    assert header["envelope_covers_required_bounds"]
    assert header["observed_pixel_coverage"] == "unverified_no_band_scan"


def test_cli_static_dry_run_prints_plan_without_execution(tmp_path, monkeypatch, capsys):
    config = coastal_fixture(tmp_path)
    parser = argparse.ArgumentParser()
    cli.register_commands(parser.add_subparsers(), default_config=str(config))
    monkeypatch.setattr(cli, "run_components", lambda *a, **kw: pytest.fail("Executed dry-run"))
    args = parser.parse_args(["build", "static", "--source-type", "water", "--dry-run"])
    args.func(args)
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] and report["target"] == "static"
    assert not load_app_config(config).paths.output_dir.exists()


def test_input_only_regional_validation_checks_readability_and_water_role(tmp_path, monkeypatch):
    config = coastal_fixture(tmp_path)
    monkeypatch.setattr(regional, "default_config_path", lambda: config)
    report = regional.validate_region(config, source_type="water", require_outputs=False)
    assert report["valid"]
    load_app_config(config).paths.water_polygon_path.write_text("corrupt")
    report = regional.validate_region(config, source_type="water", require_outputs=False)
    assert not report["valid"]
    assert "water" in report["invalid_inputs"]


def test_preflight_preserves_required_shared_study_checksum_validation(tmp_path, study_fixture):
    study, _, _, _ = study_fixture(tmp_path)
    config = tmp_path / "viewshed.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "run": {"name": "preflight-shared", "version": "test"},
                "region": {
                    "bbox_wgs84": {"min_lon": -125, "min_lat": 47, "max_lon": -122, "max_lat": 50}
                },
                "h3": {"source_resolution": 7, "target_resolution": 7},
                "viewshed": {"dem_resolution_m": 30},
                "shared_study": {"config_path": study.name},
                "paths": {
                    "output_dir": str(tmp_path / "work"),
                    "final_output_dir": str(tmp_path / "final"),
                },
            }
        )
    )
    report = plan_components(config, target="distance")
    assert report["ready"]
    assert report["configuration_validation"]["shared_study_identity_validation_required"]
    with (tmp_path / "Data/mask.parquet").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="SHA256"):
        plan_components(config, target="distance")
