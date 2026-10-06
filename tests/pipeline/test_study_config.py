from __future__ import annotations

import copy
import hashlib
import json
import os
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
from viewshed_toolkit.pipeline.config.reporting import (
    load_reporting_support,
    reporting_cells_for_geometry,
    support_polygons_for_reporting_geometry,
    validate_native_path_coverage,
    validate_reporting_membership_ids,
)
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
FIXTURE = ROOT / "tests/fixtures/marinecast_coastal_policy_v1.json"
CONFIG_HASH = "bacf22ea2b0beb32d1ef5607f52b2f6104419dd329edf25657bca196acc8018c"
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
        config["domain"]["geometry_status"] = "source_relative_validated"
        config["domain"]["selection_policy"].update(
            mask_status="source_relative_validated",
            approval={
                "approved_at": "2026-10-06T00:00:00Z",
                "source_message_id": "synthetic-test-only",
                "scope": "coastal_collection_policy",
                "statement": "Synthetic fixture only; no real mask certified.",
                "confirmed_question_id": "synthetic-question-only",
            },
        )
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


def reporting_fixture_files(tmp_path, geometry, *, config_directory=None):
    directory = tmp_path / "Data/shared/synthetic"
    directory.mkdir(parents=True, exist_ok=True)
    mask = directory / "reporting_water.parquet"
    members = directory / "water_reporting.r7.txt"
    gpd.GeoDataFrame(geometry=[geometry], crs=4326).to_parquet(mask)
    cells = reporting_cells_for_geometry(geometry, 7)
    members.write_bytes("".join(f"{cell}\n" for cell in cells).encode("ascii"))
    entry = {
        "resolution": 7,
        "role": "water_reporting",
        "relative_path": os.path.relpath(members, config_directory or tmp_path),
        "count": len(cells),
        "sha256": hashlib.sha256(members.read_bytes()).hexdigest(),
    }
    return mask, entry


def bind_reporting_raw(raw, tmp_path, geometry):
    mask, entry = reporting_fixture_files(tmp_path, geometry)
    raw["paths"]["reporting_water_polygon_path"] = str(mask)
    raw["marinecast_study"].update(
        resolved_data_root=str(tmp_path / "Data"),
        study_config_directory=str(tmp_path),
        grid_registry={
            "status": "validated",
            "mask_revision": "synthetic-test-only",
            "mask_sha256": hashlib.sha256(mask.read_bytes()).hexdigest(),
            "memberships": [entry],
        },
    )
    return mask


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
    bind_reporting_raw(raw, tmp_path, box(-123.01, 48, -123, 48.01))
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
    bind_reporting_raw(raw, tmp_path, water.intersection(box(-123.01, 48, -123, 48.01)))
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
    reporting_mask, membership = reporting_fixture_files(
        tmp_path, box(*bbox), config_directory=selected.parent
    )
    config["grid_registry"].update(
        mask_sha256=hashlib.sha256(reporting_mask.read_bytes()).hexdigest(),
        memberships=[membership],
    )
    selected.write_text(json.dumps(config))
    raw = copy.deepcopy(load_app_config(ROOT / "configs/salish_sea.yaml").raw_config)
    raw["paths"].update(
        land_polygon_path=str(land),
        water_polygon_path=str(water),
        regional_dem_path=str(tmp_path / "dem.tif"),
        canopy_height_path=str(tmp_path / "chm.tif"),
        reporting_water_polygon_path=str(reporting_mask),
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
    selected, path, app, land, _water = coastal_study
    with study_selection(selected):
        result = build_land_cells_for_config(path, overwrite=True)
        if changed == "mask":
            Path(app.raw_config["paths"]["reporting_water_polygon_path"]).write_bytes(
                b"wrong-mask-bytes"
            )
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


@pytest.mark.parametrize("field", ["selection_policy", "geometry_status", "bbox_role"])
@pytest.mark.parametrize("bad_value", ["omitted", None, {}, "malformed"])
@pytest.mark.parametrize("planning", [False, True])
def test_coastal_fields_cannot_be_omitted_null_or_malformed(tmp_path, field, bad_value, planning):
    path = approved_study(tmp_path)
    config = json.loads(path.read_bytes())
    # Keep coastal pending support while supplying nominal domain approval/registry.
    config["domain"]["geometry_status"] = "pending_qualified_coastline_validation"
    if bad_value == "omitted":
        config["domain"].pop(field)
    else:
        config["domain"][field] = bad_value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_study_config(path, planning=planning)


@pytest.mark.parametrize("field", ["selection_policy", "geometry_status", "bbox_role"])
def test_incomplete_coastal_selection_fails_entrypoints_without_writes(
    tmp_path, monkeypatch, field
):
    from viewshed_toolkit.pipeline.api.stages import export_static_maps

    path = approved_study(tmp_path)
    config = json.loads(path.read_bytes())
    config["domain"].pop(field)
    path.write_text(json.dumps(config))
    monkeypatch.setenv("MARINECAST_STUDY_CONFIG", str(path))
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        load_app_config(ROOT / "configs/salish_sea.yaml", study_config=path)
    with pytest.raises(ValueError):
        export_static_maps(ROOT / "configs/salish_sea.yaml")
    with pytest.raises(ValueError):
        main(["export-static-maps", "--study-config", str(path)])
    with pytest.raises(ValueError):
        main(["plan-study", "--study-config", str(path)])
    assert sorted(tmp_path.rglob("*")) == before


def test_removing_all_coastal_fields_does_not_enable_shared_rectangular_fallback(tmp_path):
    path = approved_study(tmp_path)
    config = json.loads(path.read_bytes())
    for field in ("selection_policy", "geometry_status", "bbox_role"):
        config["domain"].pop(field)
    path.write_text(json.dumps(config))
    for planning in (True, False):
        with pytest.raises(ValueError):
            load_study_config(path, planning=planning)


def test_actual_reporting_geometry_derives_halo_without_enclosing_bbox_fallback():
    reporting = box(-123.01, 47.98, -122.97, 48.03)
    selected, source, native = support_polygons_for_reporting_geometry(
        reporting, "EPSG:32610", 30000, 1000
    )
    assert selected.equals(reporting)
    assert not selected.covers(Point(-123.15, 48.005))
    assert source.covers(Point(-123.15, 48.005))
    # Far offshore lies inside the planning rectangle, outside this actual halo.
    assert not selected.covers(Point(-126, 48.005))
    assert not source.covers(Point(-126, 48.005))
    assert native.covers(source)


@pytest.mark.parametrize("invalid", [None, Point(-123, 48), box(0, 0, 0, 0)])
def test_invalid_reporting_geometry_has_no_bbox_fallback(invalid):
    with pytest.raises(ValueError, match="Reporting water"):
        support_polygons_for_reporting_geometry(invalid, "EPSG:32610", 30000, 1000)


def test_reporting_support_rejects_degree_buffers():
    with pytest.raises(ValueError, match="projected CRS in metres"):
        support_polygons_for_reporting_geometry(
            box(-123.01, 48, -123, 48.01), "EPSG:4326", 30000, 1000
        )


def test_decoded_membership_identity_is_strict_and_uses_owner_canonical_ids():
    cells = sorted({h3.latlng_to_cell(48, -123, 7), h3.latlng_to_cell(48.03, -123, 7)})
    digest = hashlib.sha256("".join(f"{cell}\n" for cell in cells).encode()).hexdigest()
    assert validate_reporting_membership_ids(cells, resolution=7, count=2, sha256=digest) == tuple(
        cells
    )
    for invalid in (
        [cells[0], cells[0]],
        cells[::-1],
        [None],
        [cells[0].upper()],
        [h3.latlng_to_cell(48, -123, 6)],
    ):
        with pytest.raises(ValueError):
            validate_reporting_membership_ids(invalid, resolution=7, count=2, sha256=digest)
    with pytest.raises(ValueError, match="SHA256"):
        validate_reporting_membership_ids(cells, resolution=7, count=2, sha256="0" * 64)


def test_registry_targets_and_outside_water_and_land_roles(coastal_study):
    from viewshed_toolkit.pipeline.config import apply_source_type_policy
    from viewshed_toolkit.pipeline.prepare.area.target_cells import build_target_cells
    from viewshed_toolkit.pipeline.weights.terrain.gdal import (
        _load_terrain_source_cells,
        _water_terrain_domains_for_app,
    )

    selected, path, app, _, _ = coastal_study
    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    with study_selection(selected):
        targets = gpd.read_parquet(build_target_cells(app))
        assert tuple(targets.h3_cell) == support.cells
        build_land_cells_for_config(path, overwrite=True)
        build_source_target_lookup(path)
        water_app = apply_source_type_policy(app, "water")
        sources = _load_terrain_source_cells(water_app)
        outside = h3.latlng_to_cell(48.005, -122.85, 7)
        assert outside not in support.cells
        assert outside in set(sources.h3_cell)
        assert not sources.loc[sources.h3_cell == outside, "water_geometry"].item().is_empty
        domains = _water_terrain_domains_for_app(water_app)
        assert domains.land_domain.covers(Point(-123.15, 48.005))
        assert domains.water_domain.equals(support.reporting_water)


@pytest.mark.parametrize("damage", ["escape", "count", "hash", "missing_cell", "mask_bytes"])
def test_reporting_artifacts_reject_damage_before_cache_reuse(coastal_study, damage):
    _, _, app, _, _ = coastal_study
    raw = copy.deepcopy(app.raw_config)
    support = load_reporting_support(raw, app.viewshed.crs_projected)
    entry = raw["marinecast_study"]["grid_registry"]["memberships"][0]
    if damage == "escape":
        entry["relative_path"] = "../../escape.txt"
    elif damage == "count":
        entry["count"] += 1
    elif damage == "hash":
        entry["sha256"] = "0" * 64
    elif damage == "missing_cell":
        payload = "".join(f"{cell}\n" for cell in support.cells[1:]).encode("ascii")
        support.membership_path.write_bytes(payload)
        entry.update(count=len(support.cells) - 1, sha256=hashlib.sha256(payload).hexdigest())
    else:
        support.mask_path.write_bytes(b"changed after qualification")
    with pytest.raises(ValueError):
        load_reporting_support(raw, app.viewshed.crs_projected)


def synthetic_native_rasters(app):
    import math

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    west, south, east, north = (
        gpd.GeoSeries([support.native_extent], crs=4326)
        .to_crs(app.viewshed.crs_projected)
        .iloc[0]
        .bounds
    )
    resolution = 1000
    west, south, east, north = west - 2000, south - 2000, east + 2000, north + 2000
    width = math.ceil((east - west) / resolution)
    height = math.ceil((north - south) / resolution)
    for name, path in (("dem", app.paths.regional_dem_path), ("chm", app.paths.canopy_height_path)):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="float32",
            crs=app.viewshed.crs_projected,
            transform=from_origin(west, north, resolution, resolution),
            nodata=-9999,
        ) as raster:
            raster.write(np.full((height, width), 10 if name == "dem" else 0, dtype="float32"), 1)
            raster.update_tags(
                source_date="2020-01-01",
                **(
                    {"vertical_reference": "synthetic_test_only", "vertical_units": "m"}
                    if name == "dem"
                    else {"height_reference": "above_ground", "height_units": "m"}
                ),
            )


def test_native_path_qualification_and_separate_canonical_masks(coastal_study):
    from dataclasses import replace

    import rasterio

    from viewshed_toolkit.pipeline.prepare.area.raster_stack import ensure_canonical_raster_stack

    selected, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)
    receipt = validate_native_path_coverage(app.raw_config, app.viewshed.crs_projected, canopy=True)
    assert receipt["native_rasters"]["dem"]["valid_land_pixels"] > 0
    assert receipt["native_rasters"]["chm"]["source_date"] == "2020-01-01"
    app = replace(app, viewshed=replace(app.viewshed, dem_resolution_m=1000))
    with study_selection(selected):
        stack = ensure_canonical_raster_stack(app, include_canopy=False)
        assert stack.reporting_water_mask_path is not None
        with (
            rasterio.open(stack.water_mask_path) as native,
            rasterio.open(stack.reporting_water_mask_path) as reporting,
        ):
            assert native.transform == reporting.transform
            assert native.read(1).sum() > reporting.read(1).sum() > 0
        assert ensure_canonical_raster_stack(app, include_canopy=False) == stack
        from viewshed_toolkit.pipeline.prepare.area.context import prepare_batch_context

        build_land_cells_for_config(app.config_path, overwrite=True)
        app = replace(app, viewshed=replace(app.viewshed, surface_model="bare_earth"))
        context = prepare_batch_context(app, [h3.latlng_to_cell(48.005, -123.15, 7)], 0)
        assert context.canonical_water_mask_path == stack.reporting_water_mask_path
        assert context.surface_metadata["aggregation_water_role"] == "water_reporting"
        assert context.water_mask_path.name.endswith("_reporting_water_mask.tif")
        native_mask = Path(context.surface_metadata["endpoint_native_water_mask_path"])
        with rasterio.open(native_mask) as native:
            assert native.read(1).sum() > context.water_mask_arr.sum() > 0


@pytest.mark.parametrize(
    "damage", ["missing_tags", "missing_land", "missing_canopy", "short_water", "short_land"]
)
def test_native_path_qualification_rejects_incomplete_inputs(coastal_study, damage):
    import numpy as np
    import rasterio

    _, _, app, land, water = coastal_study
    synthetic_native_rasters(app)
    if damage == "missing_tags":
        with rasterio.open(app.paths.regional_dem_path, "r+") as raster:
            raster.update_tags(source_date="")
    elif damage in {"missing_land", "missing_canopy"}:
        path = (
            app.paths.canopy_height_path
            if damage == "missing_canopy"
            else app.paths.regional_dem_path
        )
        with rasterio.open(path, "r+") as raster:
            raster.write(np.full((raster.height, raster.width), -9999, dtype="float32"), 1)
    else:
        path = water if damage == "short_water" else land
        frame = gpd.GeoDataFrame(geometry=[box(-123, 48, -122.99, 48.01)], crs=4326)
        if damage == "short_water":
            frame.to_parquet(path)
        else:
            frame.to_file(path)
    with pytest.raises(ValueError):
        validate_native_path_coverage(app.raw_config, app.viewshed.crs_projected, canopy=True)


def test_positive_area_membership_retains_cell_without_center(tmp_path):
    from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

    cell = h3.latlng_to_cell(48.005, -122.99, 7)
    polygon = cell_to_polygon(cell)
    vertex = polygon.exterior.coords[0]
    fragment = polygon.intersection(Point(vertex).buffer(0.0001))
    center = Point(*reversed(h3.cell_to_latlng(cell)))
    assert not fragment.covers(center)
    assert cell in reporting_cells_for_geometry(fragment, 7)


def test_consumer_never_falls_back_to_planning_envelope(coastal_study):
    from viewshed_toolkit.pipeline.contracts.components import acquisition_request

    _, _, app, _, _ = coastal_study
    raw = copy.deepcopy(app.raw_config)
    del raw["paths"]["reporting_water_polygon_path"]
    with pytest.raises(ValueError, match=r"explicit paths\.reporting"):
        load_reporting_support(raw, app.viewshed.crs_projected)
    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    request = acquisition_request(app, "dem")
    assert request["acquisition_bbox"] == list(support.native_extent.bounds)
    assert request["reporting_support"]["membership"]["count"] == len(support.cells)


def test_reporting_cache_revalidates_changed_planning_envelope(coastal_study):
    from viewshed_toolkit.pipeline.config.reporting import _SUPPORT_CACHE

    _, _, app, _, _ = coastal_study
    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    assert load_reporting_support(app.raw_config, app.viewshed.crs_projected) == support
    raw = copy.deepcopy(app.raw_config)
    raw["marinecast_study"]["reporting_bbox_wgs84"] = [-123, 48, -122.99, 48.01]
    with pytest.raises(ValueError, match="outside declared acquisition envelope"):
        load_reporting_support(raw, app.viewshed.crs_projected)
    _SUPPORT_CACHE.clear()
    with pytest.raises(ValueError, match="outside declared acquisition envelope"):
        load_reporting_support(raw, app.viewshed.crs_projected)


@pytest.mark.parametrize("side_km,qualified", [(50, False), (100, True)])
def test_native_coverage_uses_rotated_affine_footprint(coastal_study, side_km, qualified):
    import numpy as np
    import rasterio
    from affine import Affine
    from shapely.geometry import Polygon

    _, _, app, _, _ = coastal_study
    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    native = (
        gpd.GeoSeries([support.native_extent], crs=4326).to_crs(app.viewshed.crs_projected).iloc[0]
    )
    cx, cy = native.centroid.coords[0]
    transform = (
        Affine.translation(cx, cy)
        * Affine.rotation(45)
        * Affine.translation(-side_km * 500, side_km * 500)
        * Affine.scale(1000, -1000)
    )
    footprint = Polygon(
        [transform * point for point in [(0, 0), (side_km, 0), (side_km, side_km), (0, side_km)]]
    )
    with rasterio.open(
        app.paths.regional_dem_path,
        "w",
        driver="GTiff",
        width=side_km,
        height=side_km,
        count=1,
        dtype="float32",
        crs=app.viewshed.crs_projected,
        transform=transform,
        nodata=-9999,
    ) as raster:
        raster.write(np.full((side_km, side_km), 10, dtype="float32"), 1)
        raster.update_tags(
            source_date="2020-01-01", vertical_reference="synthetic_test_only", vertical_units="m"
        )
    with rasterio.open(app.paths.regional_dem_path) as raster:
        assert box(*raster.bounds).covers(native)
    assert footprint.covers(native) is qualified
    if qualified:
        receipt = validate_native_path_coverage(
            app.raw_config, app.viewshed.crs_projected, canopy=False
        )
        assert receipt["native_rasters"]["dem"]["uncovered_path_area_raster_crs_squared_units"] == 0
        assert receipt["native_rasters"]["dem"]["valid_land_pixels"] > 0
    else:
        assert native.difference(footprint).area > 1e9
        with pytest.raises(ValueError, match="uncovered area"):
            validate_native_path_coverage(app.raw_config, app.viewshed.crs_projected, canopy=False)


@pytest.mark.parametrize("affine_type", ["rotated", "sheared"])
@pytest.mark.parametrize("missing", ["none", "all", "partial"])
def test_affine_blocks_cannot_skip_required_land_pixels(coastal_study, affine_type, missing):
    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.features import geometry_mask
    from shapely.geometry import mapping

    from viewshed_toolkit.pipeline.config.reporting import read_polygon

    _, _, app, land_path, _ = coastal_study
    support = load_reporting_support(app.raw_config, app.viewshed.crs_projected)
    native = (
        gpd.GeoSeries([support.native_extent], crs=4326).to_crs(app.viewshed.crs_projected).iloc[0]
    )
    cx, cy = native.centroid.coords[0]
    rotation = Affine.rotation(45) if affine_type == "rotated" else Affine.shear(25, 10)
    transform = (
        Affine.translation(cx, cy)
        * rotation
        * Affine.translation(-64000, 64000)
        * Affine.scale(1000, -1000)
    )
    land = (
        gpd.GeoSeries([read_polygon(land_path).intersection(support.native_extent)], crs=4326)
        .to_crs(app.viewshed.crs_projected)
        .iloc[0]
    )
    required = geometry_mask(
        [mapping(land)], out_shape=(128, 128), transform=transform, invert=True, all_touched=True
    )
    assert required.sum() > 1000
    values = np.full((128, 128), 10, dtype="float32")
    if missing == "all":
        values[:] = -9999
    elif missing == "partial":
        row, column = np.argwhere(required)[len(np.argwhere(required)) // 2]
        values[row, column] = -9999
    with rasterio.open(
        app.paths.regional_dem_path,
        "w",
        driver="GTiff",
        width=128,
        height=128,
        count=1,
        dtype="float32",
        crs=app.viewshed.crs_projected,
        transform=transform,
        nodata=-9999,
        tiled=True,
        blockxsize=32,
        blockysize=32,
    ) as raster:
        raster.write(values, 1)
        raster.update_tags(
            source_date="2020-01-01", vertical_reference="synthetic_test_only", vertical_units="m"
        )
    if missing == "none":
        receipt = validate_native_path_coverage(
            app.raw_config, app.viewshed.crs_projected, canopy=False
        )
        assert receipt["native_rasters"]["dem"]["valid_land_pixels"] == int(required.sum())
        assert receipt["native_rasters"]["dem"]["required_land_pixels"] == int(required.sum())
    else:
        with pytest.raises(ValueError, match="missing land pixels"):
            validate_native_path_coverage(app.raw_config, app.viewshed.crs_projected, canopy=False)


@pytest.mark.parametrize("cache_damage", ["stale", "incomplete"])
def test_streamed_projected_windows_equal_global_grid(tmp_path, cache_damage):
    from dataclasses import replace

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    from viewshed_toolkit._internal.geo.raster import reproject_raster
    from viewshed_toolkit.pipeline.prepare.elevation.windows import ensure_projected_dem_window

    values = (np.arange(151 * 161).reshape(151, 161) / 30).astype("float32")
    values[40:44, 58:64] = -9999
    source = tmp_path / "raw.tif"
    transform = from_origin(500000, 5400000, 30, 30)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=161,
        height=151,
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=transform,
        nodata=-9999,
    ) as raster:
        raster.write(values, 1)
    global_path = tmp_path / "global.tif"
    reproject_raster(source, global_path, "EPSG:32610", 30)
    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    app = replace(
        app,
        paths=replace(
            app.paths, regional_dem_path=source, projected_dem_path=tmp_path / "unused_global.tif"
        ),
        viewshed=replace(app.viewshed, dem_resolution_m=30),
    )
    with rasterio.open(global_path) as global_raster:
        window = Window(10, 10, 65, 73)
        bounds = global_raster.window_bounds(window)
        expected = global_raster.read(1, window=window)
        expected_transform = global_raster.window_transform(window)
    tiled = ensure_projected_dem_window(app, bounds)
    assert not app.paths.projected_dem_path.exists()
    with rasterio.open(tiled) as raster:
        assert raster.transform == expected_transform
        assert np.array_equal(raster.read(1), expected, equal_nan=True)
    assert ensure_projected_dem_window(app, bounds) == tiled
    if cache_damage == "stale":
        with rasterio.open(tiled, "r+") as raster:
            altered = raster.read(1)
            altered[0, 0] += 1
            raster.write(altered, 1)
        message = "Stale projected"
    else:
        receipt = tiled.with_suffix(".tif.metadata.json")
        receipt.rename(receipt.with_suffix(".preserved.json"))
        message = "Incomplete projected"
    preserved = tiled.read_bytes()
    with pytest.raises(ValueError, match=message):
        ensure_projected_dem_window(app, bounds)
    assert tiled.read_bytes() == preserved


def test_projected_window_guard_runs_before_raster_allocation(coastal_study):
    from dataclasses import replace

    from viewshed_toolkit.pipeline.prepare.elevation.windows import ensure_projected_dem_window

    _, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)
    app = replace(
        app,
        batch=replace(app.batch, max_batch_aoi_pixels=10),
        viewshed=replace(app.viewshed, dem_resolution_m=1000),
    )
    with pytest.raises(ValueError, match="too large"):
        ensure_projected_dem_window(app, (460000, 5290000, 530000, 5360000))
    assert not (app.paths.projected_dem_path.parent / "projected_dem_windows").exists()


def test_windowed_reprojection_fails_closed_before_allocation(coastal_study):
    from viewshed_toolkit.pipeline.prepare.elevation.windows import ensure_projected_dem_window

    _, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)  # 1000 m source cannot silently become the 30 m grid.
    with pytest.raises(ValueError, match="warp parity is unqualified"):
        ensure_projected_dem_window(app, (460000, 5290000, 530000, 5360000))
    assert not (app.paths.projected_dem_path.parent / "projected_dem_windows").exists()


def test_windowed_canopy_grid_mismatch_fails_before_allocation(coastal_study):
    from dataclasses import replace

    import rasterio
    from affine import Affine

    from viewshed_toolkit.pipeline.prepare.area.raster_stack import ensure_canonical_raster_stack

    selected, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)
    with rasterio.open(app.paths.canopy_height_path) as canopy:
        profile = canopy.profile
        data = canopy.read(1)
        tags = canopy.tags()
    profile["transform"] = Affine.translation(1, 0) * profile["transform"]
    with rasterio.open(app.paths.canopy_height_path, "w", **profile) as canopy:
        canopy.write(data, 1)
        canopy.update_tags(**tags)
    app = replace(app, viewshed=replace(app.viewshed, dem_resolution_m=1000))
    with study_selection(selected), pytest.raises(ValueError, match="transform mismatch"):
        ensure_canonical_raster_stack(
            app, include_canopy=True, window_bounds=(460000, 5290000, 530000, 5360000)
        )
    assert not (app.paths.projected_dem_path.parent / "projected_dem_windows").exists()


def test_windowed_canopy_resolution_warp_needs_independent_qualification(coastal_study):
    from viewshed_toolkit.pipeline.prepare.area.raster_stack import ensure_canonical_raster_stack

    selected, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)
    with study_selection(selected), pytest.raises(ValueError, match="canopy maximum-resampling"):
        ensure_canonical_raster_stack(
            app, include_canopy=True, window_bounds=(460000, 5290000, 530000, 5360000)
        )
    assert not (app.paths.projected_dem_path.parent / "projected_dem_windows").exists()


@pytest.mark.parametrize(
    "surface,role", [("bare_earth", "land"), ("canopy", "land"), ("bare_earth", "water")]
)
def test_windowed_native_h3_kernel_matches_global_stack(coastal_study, surface, role):
    from dataclasses import replace

    import pandas as pd

    from viewshed_toolkit.pipeline.config.loader import apply_source_type_policy
    from viewshed_toolkit.pipeline.weights.terrain.runner import run_single_cell

    pytest.importorskip("osgeo.gdal")
    selected, _, app, _, _ = coastal_study
    synthetic_native_rasters(app)
    app = apply_source_type_policy(app, role)
    app = replace(
        app,
        viewshed=replace(app.viewshed, dem_resolution_m=1000, surface_model=surface),
        batch=replace(app.batch, max_workers=1),
        run=replace(app.run, keep_batch_intermediates=True),
    )
    cell = h3.latlng_to_cell(48.005, -123.035 if role == "land" else -122.99, 7)
    results = []
    with study_selection(selected):
        build_land_cells_for_config(app.config_path, overwrite=True)
        build_source_target_lookup(app.config_path, overwrite=False)
        for mode in ("global", "windowed"):
            configured = replace(
                app,
                batch=replace(app.batch, raster_stack_mode=mode),
                run=replace(app.run, name=f"parity_{surface}_{role}_{mode}"),
                paths=replace(
                    app.paths,
                    partitioned_visibility_dir=app.paths.output_dir
                    / "parity"
                    / mode
                    / "partitions",
                ),
            )
            result = run_single_cell(configured, cell)
            assert result.combined_h3 is not None
            assert result.combined_observers is not None
            results.append(result)
    # Compare scientific fields while excluding run-specific metadata columns.
    left, right = results
    assert left.n_rows == right.n_rows > 0
    scientific = [
        name
        for name in left.combined_h3.columns
        if not name.startswith(("run_", "viewshed_", "config_"))
    ]
    pd.testing.assert_frame_equal(
        left.combined_h3[scientific].reset_index(drop=True),
        right.combined_h3[scientific].reset_index(drop=True),
        check_exact=True,
    )
    pd.testing.assert_frame_equal(
        left.combined_observers.reset_index(drop=True),
        right.combined_observers.reset_index(drop=True),
        check_exact=True,
    )


@pytest.mark.parametrize("source_crs", ["EPSG:4326", "EPSG:5070"])
@pytest.mark.parametrize("nodata", [None, -9999, float("nan")])
def test_original_global_warp_chunks_preserve_cross_crs_pixels(
    tmp_path, caplog, source_crs, nodata
):
    import logging
    import re
    from dataclasses import replace

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    from viewshed_toolkit._internal.geo.raster import reproject_raster
    from viewshed_toolkit.pipeline.prepare.elevation.windows import ensure_projected_dem_window

    app = load_app_config(ROOT / "configs/salish_sea.yaml")
    source = tmp_path / "source.tif"
    transform = (
        from_origin(-123.06, 48.06, 0.0003, 0.0003)
        if source_crs == "EPSG:4326"
        else from_origin(-2029873, 3099663, 8.113975, 8.113975)
    )
    rows, columns = np.mgrid[:181, :191]
    values = (20 + 7 * np.sin(rows / 4) + columns / 2).astype("float32")
    if nodata is not None:
        values[50:70, 60:80] = nodata
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=191,
        height=181,
        count=1,
        dtype="float32",
        crs=source_crs,
        transform=transform,
        nodata=nodata,
    ) as raster:
        raster.write(values, 1)
    global_path = tmp_path / "global.tif"
    with caplog.at_level(logging.DEBUG, logger="rasterio._err"), rasterio.Env(CPL_DEBUG=True):
        reproject_raster(
            source,
            global_path,
            "EPSG:32610",
            30,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
    chunks = []
    for record in caplog.records:
        match = re.search(r"GDALWarpKernel\(\).*Dst=(\d+),(\d+),(\d+)x(\d+)", record.getMessage())
        if match:
            chunks.append([int(value) for value in match.groups()])
    with rasterio.open(global_path) as global_raster:
        transform, width, height = (
            global_raster.transform,
            global_raster.width,
            global_raster.height,
        )
    assert chunks == [[0, 0, width, height]]
    payload = {
        "schema_version": 1,
        "method": "rasterio_global_gdal_chunks_v1",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_bytes": source.stat().st_size,
        "transform": list(transform),
        "shape": [height, width],
        "crs": "EPSG:32610",
        "destination_block_size": app.raster.block_size,
        "rasterio_version": rasterio.__version__,
        "gdal_version": rasterio.__gdal_version__,
        "resampling": "bilinear",
        "transformer_tolerance": 0.125,
        "warp_memory_mib": 64,
        "num_threads": 1,
        "producer_reference_sha256": hashlib.sha256(global_path.read_bytes()).hexdigest(),
        "chunks": chunks,
    }
    plan = tmp_path / "chunk-plan.json"
    plan.write_text(json.dumps(payload))
    app = replace(
        app,
        paths=replace(
            app.paths, regional_dem_path=source, projected_dem_path=tmp_path / "unused_global.tif"
        ),
        batch=replace(
            app.batch,
            warp_chunk_plan_path=str(plan),
            warp_chunk_plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        ),
    )
    window = Window(7, 9, 21, 19)
    with rasterio.open(global_path) as global_raster:
        expected = global_raster.read(1, window=window)
        bounds = global_raster.window_bounds(window)
        expected_transform = global_raster.window_transform(window)
    tiled = ensure_projected_dem_window(app, bounds)
    with rasterio.open(tiled) as raster:
        assert raster.transform == expected_transform
        np.testing.assert_array_equal(raster.read(1), expected, strict=True)
    assert ensure_projected_dem_window(app, bounds) == tiled
    # Reject an oversized original context even when the requested crop is tiny.
    guarded = replace(app, batch=replace(app.batch, max_batch_aoi_pixels=500))
    with pytest.raises(ValueError, match="too large"):
        ensure_projected_dem_window(guarded, bounds)
    # Pinned receipt mutation fails before returning a previously cached output.
    plan.write_text(json.dumps({**payload, "transformer_tolerance": 0.0}))
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        ensure_projected_dem_window(app, bounds)
    assert not app.paths.projected_dem_path.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "gap",
        "outside",
        "fractional",
        "boolean",
        "resampler",
        "tolerance",
        "version",
        "extra",
        "source",
    ],
)
def test_warp_chunk_receipt_contract_rejects_invalid_partitions_and_method(damage):
    from viewshed_toolkit.pipeline.contracts.warp import validate_global_warp_chunk_plan

    expected = {
        "transform": [30, 0, 500000, 0, -30, 5400000, 0, 0, 1],
        "shape": [100, 100],
        "resampling": "bilinear",
        "transformer_tolerance": 0.125,
        "gdal_version": "current",
        "source_sha256": "a" * 64,
    }
    payload = {
        **expected,
        "producer_reference_sha256": "b" * 64,
        "chunks": [[0, 0, 100, 50], [0, 50, 100, 50]],
    }
    validate_global_warp_chunk_plan(payload, expected=expected)
    if damage == "duplicate":
        payload["chunks"] = [[0, 0, 100, 50], [0, 0, 100, 50]]
    elif damage == "gap":
        payload["chunks"] = [[0, 0, 100, 99]]
    elif damage == "outside":
        payload["chunks"] = [[0, 0, 101, 100]]
    elif damage == "fractional":
        payload["chunks"][0][0] = 0.0
    elif damage == "boolean":
        payload["chunks"][0][0] = False
    elif damage == "resampler":
        payload["resampling"] = "nearest"
    elif damage == "tolerance":
        payload["transformer_tolerance"] = 0
    elif damage == "version":
        payload["gdal_version"] = "other"
    elif damage == "source":
        payload["source_sha256"] = "c" * 64
    else:
        payload["extra"] = "silently ignored"
    with pytest.raises(ValueError):
        validate_global_warp_chunk_plan(payload, expected=expected)
