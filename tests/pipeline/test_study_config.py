from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import h3
import pandas as pd
import pytest
import yaml
from shapely.geometry import Point, box

from viewshed_toolkit.pipeline.api.components import run_component_stage
from viewshed_toolkit.pipeline.api.regional import validate_region
from viewshed_toolkit.pipeline.cli.main import main
from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.config.study import (
    adapt_raw_config,
    canonical_bytes,
    load_study_config,
    planning_report,
    require_shared_owned_output,
    selected_study_path,
    study_selection,
    support_polygons,
    validate_study_app,
)
from viewshed_toolkit.pipeline.contracts.components import provenance
from viewshed_toolkit.pipeline.finalize.final_artifacts import static_scientific_config_hash
from viewshed_toolkit.pipeline.prepare.area.config import SourceTargetLookupConfig
from viewshed_toolkit.pipeline.prepare.area.domains import (
    load_land_water_domains,
    source_domain_polygon,
    target_domain_polygon,
)
from viewshed_toolkit.pipeline.prepare.area.land import build_land_cells_for_config
from viewshed_toolkit.pipeline.prepare.area.lookup import build_source_target_lookup
from viewshed_toolkit.pipeline.prepare.area.universe import _physical_cell_type_frame

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/marinecast_study_proposed_v1.json"
CONFIG_HASH = "16ff9f7e75cc4b2a79d193b9364921d4453ef569a93d148557588e642e429060"
GEOMETRY_HASH = "6d79e4dfd29a4ada66625e20fdcd01e7bfe6076bf6ebb4c449581eb3a0cdfb68"


@pytest.fixture(autouse=True)
def no_study_environment(monkeypatch):
    monkeypatch.delenv("MARINECAST_STUDY_CONFIG", raising=False)


def write_study(tmp_path, mutator=None):
    config = json.loads(FIXTURE.read_text())
    if mutator:
        mutator(config)
    path = tmp_path / "config" / "study.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(config))
    return path


def approved_study(tmp_path):
    def approve(config):
        config["domain"]["status"] = "approved"
        config["domain"]["approval"] = {
            "approved_at": "2026-10-06T00:00:00Z",
            "source_message_id": "synthetic-test-only",
            "scope": "rectangular_selection_only",
            "statement": "Synthetic fixture approval; no real domain approved.",
        }
        config["grid_registry"].update(
            status="validated",
            mask_revision="synthetic-test-only",
            mask_sha256="0" * 64,
            memberships=[
                {
                    "resolution": 7,
                    "role": "water_reporting",
                    "relative_path": "test.parquet",
                    "count": 1,
                    "sha256": "1" * 64,
                }
            ],
        )

    return write_study(tmp_path, approve)


def test_pinned_identities_and_read_only_plan(tmp_path, monkeypatch):
    path = write_study(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    study = load_study_config(path, planning=True)
    assert study.config_sha256 == CONFIG_HASH
    assert study.geometry_sha256 == GEOMETRY_HASH
    assert study.data_root == tmp_path / "Data"
    assert planning_report(study)["requested_time"]["end_exclusive"] == "2027-01-01"
    assert sorted(tmp_path.rglob("*")) == before


def test_identity_comes_from_one_file_snapshot(tmp_path, monkeypatch):
    path = write_study(tmp_path)
    snapshot = path.read_bytes()
    original = Path.read_bytes
    reads = []

    def read_once(file):
        if file == path:
            reads.append(file)
            return snapshot
        return original(file)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    study = load_study_config(path, planning=True)
    assert reads == [path]
    assert study.raw_file_sha256 == hashlib.sha256(snapshot).hexdigest()
    assert study.config_sha256 == CONFIG_HASH


def test_explicit_path_precedes_environment_without_missing_path_fallback(tmp_path, monkeypatch):
    path = write_study(tmp_path)
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(tmp_path / "missing"))
    assert load_study_config(path, planning=True).config_sha256 == CONFIG_HASH
    with pytest.raises(FileNotFoundError):
        load_study_config(planning=True)
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(path))
    with pytest.raises(FileNotFoundError):
        load_study_config(tmp_path / "missing", planning=True)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", 2),
        ("unexpected", True),
    ],
)
def test_schema_rejects_unsupported_or_unknown(tmp_path, key, value):
    path = write_study(tmp_path, lambda config: config.update({key: value}))
    with pytest.raises(ValueError, match="Invalid study config"):
        load_study_config(path, planning=True)


@pytest.mark.parametrize(
    "mutator,match",
    [
        (lambda c: c["domain"].update(bbox_wgs84=[1, 2, 0, 3]), "rectangle"),
        (lambda c: c["domain"].update(geometry_sha256="0" * 64), "geometry_sha256"),
        (lambda c: c["time"].update(end_exclusive="2009-01-01"), "interval"),
        (lambda c: c["storage"].update(data_root="/absolute"), "relative"),
        (lambda c: c["producer_buffers"]["viewshed"].update(source_policy="drop_land"), "policy"),
    ],
)
def test_semantic_validation(tmp_path, mutator, match):
    with pytest.raises(ValueError, match=match):
        load_study_config(write_study(tmp_path, mutator), planning=True)


def test_duplicate_and_nonfinite_json_rejected(tmp_path):
    path = tmp_path / "study.json"
    for text in ['{"schema_version":1,"schema_version":1}', '{"bad":NaN}']:
        path.write_text(text)
        with pytest.raises(ValueError):
            load_study_config(path, planning=True)


def test_proposed_rejected_before_config_or_pipeline_io(tmp_path, monkeypatch):
    path = write_study(tmp_path)
    with pytest.raises(ValueError, match="proposed"):
        load_study_config(path)
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(path))
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="proposed"):
        main(["stage", "download-dem", "--config", str(tmp_path / "missing.yaml")])
    assert sorted(tmp_path.rglob("*")) == before


def test_cli_selection_and_planning(tmp_path, monkeypatch, capsys):
    path = write_study(tmp_path)
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(tmp_path / "missing"))
    for args in [
        ["plan-study", "--study-config", str(path)],
        ["--study-config", str(path), "plan-study"],
    ]:
        main(args)
        report = json.loads(capsys.readouterr().out)
        assert report["planning_only"]
        assert report["config_sha256"] == CONFIG_HASH


def test_approved_pending_registry_fails_closed(tmp_path):
    path = approved_study(tmp_path)
    config = json.loads(path.read_text())
    config["grid_registry"]["status"] = "pending_validated_marine_mask"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="validated marine mask"):
        load_study_config(path)


def test_approved_without_approval_provenance_fails_even_in_planning(tmp_path):
    path = write_study(tmp_path, lambda c: c["domain"].update(status="approved"))
    with pytest.raises(ValueError, match="approval provenance"):
        load_study_config(path, planning=True)


@pytest.mark.parametrize("selection", ["missing", "proposed"])
def test_exports_reject_invalid_study_before_artifact_io(tmp_path, monkeypatch, selection):
    from viewshed_toolkit.pipeline.api.stages import export_static_maps

    path = write_study(tmp_path) if selection == "proposed" else tmp_path / "missing.json"
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(path))
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises((FileNotFoundError, ValueError)):
        export_static_maps(ROOT / "configs/salish_sea.yaml")
    assert sorted(tmp_path.rglob("*")) == before


def test_adapter_paths_resolution_identity_and_no_writes(tmp_path, monkeypatch):
    path = approved_study(tmp_path)
    standalone = load_app_config(ROOT / "configs/salish_sea.yaml")
    before = sorted(tmp_path.rglob("*"))
    app = load_app_config(ROOT / "configs/salish_sea.yaml", study_config=path)
    assert app.paths.output_dir.is_relative_to(tmp_path / "Data/viewshed")
    assert app.paths.water_polygon_path.is_relative_to(tmp_path / "Data")
    assert app.study_config_path == path
    assert app.h3.source_resolution == app.h3.target_resolution == 7
    assert app.raw_config["marinecast_study"]["geometry_sha256"] == GEOMETRY_HASH
    assert (
        provenance(app, "test", {})["marinecast_study"]["config_sha256"]
        == load_study_config(path).config_sha256
    )
    assert sorted(tmp_path.rglob("*")) == before
    monkeypatch.chdir(tmp_path)
    assert load_app_config(ROOT / "configs/salish_sea.yaml").raw_config == standalone.raw_config


def test_resolution_buffer_and_role_conflicts(tmp_path):
    study = load_study_config(approved_study(tmp_path))
    raw = load_app_config(ROOT / "configs/salish_sea.yaml").raw_config
    for section, key, value in [
        ("h3", "source_resolution", 6),
        ("viewshed", "max_distance_m", 5000),
        ("source_target_lookup", "include_land_sources", False),
    ]:
        changed = copy.deepcopy(raw)
        changed[section][key] = value
        with pytest.raises(ValueError):
            adapt_raw_config(changed, study)


def test_reporting_targets_outside_land_sources_and_native_margin(tmp_path):
    study = load_study_config(approved_study(tmp_path))
    raw = adapt_raw_config(load_app_config(ROOT / "configs/salish_sea.yaml").raw_config, study)
    # Small fixture avoids enumerating the large proposed universe.
    raw["marinecast_study"]["reporting_bbox_wgs84"] = [-123.01, 48.0, -123.0, 48.01]
    runtime = SimpleNamespace(
        raw_config=raw, projected_crs="EPSG:32610", bbox_wgs84=(-123.01, 48, -123, 48.01)
    )
    reporting = target_domain_polygon(runtime)
    sources = source_domain_polygon(runtime)
    outside = Point(-123.1, 48.005)
    assert not reporting.covers(outside)
    assert sources.covers(outside)
    _, source, native = support_polygons([-123.01, 48, -123, 48.01], "EPSG:32610", 30000, 1000)
    assert native.covers(source)
    standalone = SimpleNamespace(
        raw_config={"viewshed": {"max_distance_m": 30000, "aoi_margin_m": 1000}},
        projected_crs="EPSG:32610",
        bbox_wgs84=(-123.01, 48, -123, 48.01),
    )
    assert source_domain_polygon(standalone).equals(reporting)
    assert target_domain_polygon(standalone).covers(outside)


def test_explicit_land_and_marine_mask_no_water_or_land_complement(tmp_path):
    land = box(-123.4, 47.6, -123.005, 48.4)
    water = box(-123.005, 47.6, -122.6, 48.4)
    land_path, water_path = tmp_path / "land.parquet", tmp_path / "water.parquet"
    for path, geom in [(land_path, land), (water_path, water)]:
        gpd.GeoDataFrame(geometry=[geom], crs=4326).to_parquet(path)
    raw = {
        "marinecast_study": {
            "reporting_bbox_wgs84": [-123.01, 48, -123, 48.01],
            "producer_buffers": {"line_of_sight_m": 30000, "aoi_margin_m": 1000},
        },
        "paths": {"land_polygon_path": str(land_path), "water_polygon_path": str(water_path)},
    }
    raw["marinecast_study"]["grid_registry"] = {
        "mask_sha256": hashlib.sha256(water_path.read_bytes()).hexdigest()
    }
    runtime = SimpleNamespace(
        raw_config=raw,
        projected_crs="EPSG:32610",
        bbox_wgs84=(-123.01, 48, -123, 48.01),
        config_dir=tmp_path,
    )
    source = load_land_water_domains(runtime, extent="source")
    target = load_land_water_domains(runtime, extent="target")
    assert source.land_domain.covers(Point(-123.1, 48.005))
    assert not target.water_domain.covers(Point(-123.1, 48.005))
    assert target.water_domain.covers(Point(-123.002, 48.005))
    raw["paths"]["water_polygon_path"] = str(tmp_path / "missing.parquet")
    with pytest.raises(ValueError, match="no fallback"):
        load_land_water_domains(runtime, extent="target")


def test_positive_area_water_does_not_include_observed_zero(tmp_path):
    study = load_study_config(approved_study(tmp_path))
    raw = adapt_raw_config(load_app_config(ROOT / "configs/salish_sea.yaml").raw_config, study)
    cfg = SourceTargetLookupConfig(
        min_water_fraction_for_target=raw["source_target_lookup"]["min_water_fraction_for_target"],
        min_land_fraction_for_source=raw["source_target_lookup"]["min_land_fraction_for_source"],
    )
    frame = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "h3_cell": ["zero_water", "tiny_water", "tiny_land", "zero_both"],
                "land_fraction": [1.0, 1.0, 1e-100, 0.0],
                "water_fraction": [0.0, 1e-100, 0.0, 0.0],
            }
        )
    )
    assert _physical_cell_type_frame(frame, cfg).cell_type.tolist() == [
        "land",
        "mixed",
        "land",
        "excluded",
    ]


def test_time_identity_does_not_claim_static_vintage_or_change_physics(tmp_path):
    study = load_study_config(approved_study(tmp_path))
    raw = adapt_raw_config(load_app_config(ROOT / "configs/salish_sea.yaml").raw_config, study)
    changed = copy.deepcopy(raw)
    changed["marinecast_study"]["requested_time"]["start"] = "2010-01-01"
    assert static_scientific_config_hash(raw) == static_scientific_config_hash(changed)
    changed["marinecast_study"]["geometry_sha256"] = "f" * 64
    assert static_scientific_config_hash(raw) != static_scientific_config_hash(changed)


def test_shared_preparation_cannot_write_external_reusable_input(tmp_path):
    app = load_app_config(ROOT / "configs/salish_sea.yaml", study_config=approved_study(tmp_path))
    require_shared_owned_output(app.raw_config, app.paths.output_dir / "prepared.tif")
    with pytest.raises(ValueError, match="must stay"):
        require_shared_owned_output(app.raw_config, tmp_path / "original.tif")


def test_nested_selection_inherits_explicit_path(tmp_path):
    path = write_study(tmp_path)
    with study_selection(path), study_selection(None):
        assert selected_study_path() == path
    assert selected_study_path() is None


def test_loaded_app_rejects_changed_study_file(tmp_path):
    path = approved_study(tmp_path)
    app = load_app_config(ROOT / "configs/salish_sea.yaml", study_config=path)
    data = json.loads(path.read_text())
    data["time"]["start"] = "2010-01-01"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed after"):
        validate_study_app(app)


def test_existing_standalone_app_cannot_bypass_invalid_environment_on_export(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.visualization.component_maps import export_component_map

    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        export_component_map(app, source_type="land")


def test_shared_mask_mismatch_is_not_reinterpreted_as_zero(tmp_path):
    from viewshed_toolkit.pipeline.config.study import validate_shared_mask

    path = tmp_path / "mask.bin"
    path.write_bytes(b"changed input")
    raw = {"marinecast_study": {"grid_registry": {"mask_sha256": "0" * 64}}}
    with pytest.raises(ValueError, match="mask_sha256"):
        validate_shared_mask(raw, path)


def test_regional_reference_does_not_inherit_shared_domain(tmp_path):
    study = approved_study(tmp_path)
    with study_selection(study):
        report = validate_region(ROOT / "configs/salish_sea.yaml", require_outputs=False)
    assert report["bbox_policy"] == "marinecast_study"
    assert report["matches_canonical_model_area"] is False
    assert report["valid"] is False  # synthetic fixture has no qualified native inputs


def test_local_dataset_assets_resolve_from_study_data_root(tmp_path):
    study = load_study_config(approved_study(tmp_path))
    raw = load_app_config(ROOT / "configs/salish_sea.yaml").raw_config
    raw["datasets"] = {"dem": {"provider": "local", "assets": ["data/raw/dem.tif"]}}
    adapted = adapt_raw_config(raw, study)
    assert adapted["datasets"]["dem"]["assets"] == [str(tmp_path / "Data/raw/dem.tif")]


@pytest.fixture
def coastal_study(tmp_path):
    selected = approved_study(tmp_path)
    config = json.loads(selected.read_text())
    bbox = [-123.01, 47.98, -122.97, 48.03]
    west, south, east, north = bbox
    config["domain"]["bbox_wgs84"] = bbox
    config["domain"]["geometry_sha256"] = hashlib.sha256(
        canonical_bytes(
            {
                "crs": "EPSG:4326",
                "boundary_semantics": "longitude_latitude_rectangle",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[west, south], [east, south], [east, north], [west, north], [west, south]]
                    ],
                },
            }
        )
    ).hexdigest()
    land = tmp_path / "land.geojson"
    water = tmp_path / "water.parquet"
    # Reporting rectangle is wholly marine. The nearest mapped land lies west
    # of it; inland observer -123.15 is over 6 km from water and within 30 km LOS.
    gpd.GeoDataFrame(geometry=[box(-123.6, 47.6, -123.02, 48.4)], crs=4326).to_file(land)
    gpd.GeoDataFrame(geometry=[box(-123.02, 47.6, -122.4, 48.4)], crs=4326).to_parquet(water)
    config["grid_registry"]["mask_sha256"] = hashlib.sha256(water.read_bytes()).hexdigest()
    selected.write_text(json.dumps(config))
    raw = copy.deepcopy(load_app_config(ROOT / "configs/salish_sea.yaml").raw_config)
    raw["paths"].update(
        land_polygon_path=str(land),
        water_polygon_path=str(water),
        regional_dem_path=str(tmp_path / "dem.tif"),
        canopy_height_path=str(tmp_path / "chm.tif"),
    )
    raw["source_target_lookup"]["parallel_workers"] = 1
    path = tmp_path / "viewshed.yaml"
    path.write_text(yaml.safe_dump(raw))
    app = load_app_config(path, study_config=selected)
    app.paths.land_h3_path.parent.mkdir(parents=True, exist_ok=True)
    return selected, path, app, land, water


def test_shared_actual_land_and_pair_stages_retain_inland_observers(coastal_study):
    import polars as pl

    selected, path, app, _, _ = coastal_study
    cell = h3.latlng_to_cell(48.005, -123.15, 7)
    with study_selection(selected):
        result = build_land_cells_for_config(path, overwrite=True)
        land = gpd.read_parquet(result.land_h3_path)
        assert cell in set(land.h3_cell)
        assert land.loc[land.h3_cell == cell, "distance_to_water_m"].item() > 6000
        # Same inputs plus valid provenance permit cache reuse.
        assert build_land_cells_for_config(path).land_h3_path == result.land_h3_path
        assert run_component_stage(app, "build-source-cells") == result.land_h3_path
        lookup = build_source_target_lookup(path, overwrite=False)
    pairs = pl.read_parquet(lookup.lookup_path)
    outside_pairs = pairs.filter((pl.col("source_h3") == cell) & (pl.col("source_type") == "land"))
    assert outside_pairs.height > 0
    assert outside_pairs["distance_km"].max() <= 30
    runtime = SimpleNamespace(
        raw_config=app.raw_config,
        projected_crs="EPSG:32610",
        bbox_wgs84=tuple(app.raw_config["marinecast_study"]["reporting_bbox_wgs84"]),
        config_dir=path.parent,
    )
    reporting = load_land_water_domains(runtime, extent="target")
    assert reporting.land_domain.is_empty
    assert reporting.water_domain.area > 0
    sources = load_land_water_domains(runtime, extent="source")
    assert sources.land_domain.covers(Point(-123.15, 48.005))


@pytest.mark.parametrize("changed", ["mask", "land", "receipt"])
def test_shared_land_cache_rejects_changed_inputs_before_reuse(coastal_study, changed):
    selected, path, app, land, water = coastal_study
    with study_selection(selected):
        result = build_land_cells_for_config(path, overwrite=True)
        if changed == "mask":
            water.write_bytes(b"wrong-mask-bytes")
            message = "mask_sha256"
        elif changed == "land":
            gpd.GeoDataFrame(geometry=[box(-123.6, 47.6, -123.03, 48.4)], crs=4326).to_file(land)
            message = "Stale shared land-cell cache"
        else:
            result.land_h3_path.with_suffix(".parquet.json").write_text("{}")
            message = "Stale shared land-cell cache"
        with pytest.raises(ValueError, match=message):
            build_land_cells_for_config(path, overwrite=False)
        if changed == "mask":
            with pytest.raises(ValueError, match="mask_sha256"):
                run_component_stage(app, "build-source-cells")


def test_standalone_land_stage_keeps_legacy_coastal_filter(coastal_study):
    selected, path, _, _, _ = coastal_study
    raw = yaml.safe_load(path.read_text())
    source_bounds = support_polygons([-123.01, 48, -123, 48.01], "EPSG:32610", 30000, 1000)[
        1
    ].bounds
    raw["region"]["bbox_wgs84"] = dict(
        zip(("min_lon", "min_lat", "max_lon", "max_lat"), source_bounds, strict=True)
    )
    raw["paths"]["land_h3_path"] = str(path.parent / "standalone_land.parquet")
    path.write_text(yaml.safe_dump(raw))
    assert selected.exists()  # no selection is active for this standalone run
    result = build_land_cells_for_config(path, overwrite=True)
    assert h3.latlng_to_cell(48.005, -123.15, 7) not in set(
        gpd.read_parquet(result.land_h3_path).h3_cell
    )


def test_current_coastal_policy_is_planning_only_until_geometry_and_mask_qualified(tmp_path):
    path = tmp_path / "study.json"
    config = json.loads((ROOT / "tests/fixtures/marinecast_coastal_policy_v1.json").read_bytes())
    path.write_text(json.dumps(config))
    study = load_study_config(path, planning=True)
    assert study.config_sha256 == "bacf22ea2b0beb32d1ef5607f52b2f6104419dd329edf25657bca196acc8018c"
    plan = planning_report(study)
    assert plan["bbox_role"] == "acquisition_planning_envelope_only"
    assert plan["reporting_target_bbox_wgs84"] is None
    assert plan["acquisition_planning_envelope_bbox_wgs84"] == config["domain"]["bbox_wgs84"]
    assert plan["reporting_selection_policy"]["offshore_distance_m"] == 22224
    assert plan["reporting_selection_policy"]["status"] == "approved"
    assert plan["reporting_geometry_status"] == "pending_qualified_coastline_validation"
    with pytest.raises(ValueError, match="remains proposed"):
        load_study_config(path)
    # Approval and a nominal registry alone cannot bypass pending coastal geometry.
    synthetic = json.loads(approved_study(tmp_path).read_bytes())
    for key in ("status", "approval"):
        config["domain"][key] = synthetic["domain"][key]
    config["grid_registry"] = synthetic["grid_registry"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="validated coastal mask and geometry"):
        load_study_config(path)
