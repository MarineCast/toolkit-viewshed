"""Tiny, acquisition-free persistent preparation orchestration regressions."""

import json
from pathlib import Path

import h3
import pytest
from affine import Affine
from pydantic import ValidationError
from pyproj import Transformer
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from viewshed_toolkit.pipeline.api import transmission_tiles as api
from viewshed_toolkit.pipeline.config.transmission_tiles import (
    TransmissionTileConfig,
    load_transmission_tile_config,
)


def recipe(tmp_path):
    asset = dict(path="absent", sha256="0" * 64)
    return dict(
        schema_version=1,
        method="worldcover_chm_intersection_transmission_v2",
        store_dir=str(tmp_path / "store"),
        score_dir=str(tmp_path / "scores"),
        provisioned_metadata=asset,
        source_inventory=asset,
        reporting_water=asset,
        global_transform=[10.0, 0.0, 400000.0, 0.0, -10.0, 5500000.0],
        global_shape=[30000, 30000],
    )


def test_load_readonly_resolves_paths(tmp_path):
    payload = recipe(tmp_path)
    payload["store_dir"] = "store"
    payload["score_dir"] = "scores"
    p = tmp_path / "recipe.json"
    p.write_text(json.dumps(payload))
    cfg = load_transmission_tile_config(p)
    assert cfg.store_dir == str(tmp_path / "store")
    assert not Path(cfg.store_dir).exists()
    assert cfg.provisioned_metadata.path == str(tmp_path / "absent")


@pytest.mark.parametrize(
    "key,value",
    [
        ("observer_workers", 9),
        ("source_samples", 14),
        ("target_stride", 1),
        ("wall_seconds", 181.0),
        ("extra", 1),
        ("global_shape", [7, 9]),
    ],
)
def test_strict_limits(tmp_path, key, value):
    payload = recipe(tmp_path)
    payload[key] = value
    with pytest.raises(ValidationError):
        TransmissionTileConfig.model_validate_json(json.dumps(payload))


def test_plan_full_cell_geometry_phase_and_parent_order(tmp_path, monkeypatch):
    cfg = TransmissionTileConfig.model_validate_json(json.dumps(recipe(tmp_path)))
    cell = h3.latlng_to_cell(48.5, -123.0, 7)
    polygon = transform(
        Transformer.from_crs(4326, 32610, always_xy=True).transform,
        Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]),
    )
    water = polygon.buffer(500).difference(polygon)
    context = dict(
        grid=Affine(*cfg.global_transform),
        cells=[cell],
        land=polygon,
        water=water,
        reporting_water=water,
        metadata=dict(input_sha256={}),
    )
    monkeypatch.setattr(api, "implementation_identity", lambda: {"fixture": "stable"})
    plan = api._plan(cfg, context)
    source = plan["sources"][0]
    assert shape(source["source_land"]).equals(polygon)
    assert set(map(tuple, source["required_tile_keys"])) <= set(map(tuple, plan["required_keys"]))
    old = Affine(*source["original_transform"])
    win = Affine(*source["window_transform"])
    assert ((win.c - old.c) / 10) % 6 == 0
    assert ((old.f - win.f) / 10) % 6 == 0
    assert all(r % 384 == c % 384 == 0 for r, c in plan["required_keys"])
    cfg2 = cfg.model_copy(
        update=dict(
            observer_workers=4,
            distance_weight=dict(
                selected_model="logistic",
                logistic_d50_km=8.0,
                logistic_slope_km=2.7,
                normalize_at_zero=True,
                hard_cutoff_km=30.0,
            ),
        )
    )
    assert api._plan(cfg2, context)["identity"] == plan["identity"]
    # A new absence-assumption metadata pin must invalidate old prepared identity.
    cfg3 = cfg.model_copy(
        update=dict(
            provisioned_metadata=cfg.provisioned_metadata.model_copy(update=dict(sha256="1" * 64))
        )
    )
    changed = api._plan(cfg3, context)
    assert changed["identity"] != plan["identity"]
    assert changed["identity"]["metadata_sha256"] == "1" * 64


def test_native_pin_mismatch_does_not_create_output(tmp_path):
    cfg = TransmissionTileConfig.model_validate_json(json.dumps(recipe(tmp_path)))
    with pytest.raises((ValueError, FileNotFoundError)):
        api.verified_inputs(cfg)
    assert not Path(cfg.store_dir).exists()


def test_prepare_once_resume_and_completed_validation(tmp_path, monkeypatch):
    import numpy as np

    from viewshed_toolkit.pipeline.contracts.transmission import DTYPES
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        PreparedTileStore,
        PreparedTileWriter,
    )

    cfg = TransmissionTileConfig.model_validate_json(json.dumps(recipe(tmp_path)))
    cfg = cfg.model_copy(update=dict(global_shape=[1536, 1536]))
    water = tmp_path / "water.wkb"
    water.write_bytes(Polygon([(0, 0), (1, 0), (1, 1)]).wkb)
    cfg = cfg.model_copy(
        update=dict(reporting_water=cfg.reporting_water.model_copy(update=dict(path=str(water))))
    )
    keys = [[0, 0], [0, 384], [384, 0]]
    plan = dict(
        identity=dict(fixture="v1", code={"fixture": "stable"}),
        required_keys=keys,
        sources=[],
        census_sources=[],
    )
    context = dict(
        grid=Affine(*cfg.global_transform),
        metadata=dict(
            input_sha256={},
            terrain_vrt="absent",
            building_subset="absent",
            building_inventory_policy="unverified",
            canopy_assets=[],
            worldcover_assets=[],
        ),
        land=None,
        water=None,
        reporting_water=Polygon([(0, 0), (1, 0), (1, 1)]),
    )
    monkeypatch.setattr(api, "provisioned_context", lambda _: context)
    monkeypatch.setattr(api, "_plan", lambda *_: plan)
    monkeypatch.setattr(api, "verified_inputs", lambda _: ({}, {}))
    monkeypatch.setattr(api, "implementation_identity", lambda: {"fixture": "stable"})
    calls = []

    class Builder:
        parent_builds = 1

        def __init__(self, *args, **kwargs):
            self.timings = {}
            assert kwargs["parent_capacity"] == 1

        def __call__(self, row, col, size):
            calls.append((row, col))
            return {name: np.zeros((size, size), dtype=dtype) for name, dtype in DTYPES.items()}

    monkeypatch.setattr(api, "CanonicalTileBuilder", Builder)
    arrays = {name: np.zeros((384, 384), dtype=dtype) for name, dtype in DTYPES.items()}
    with PreparedTileWriter(
        cfg.store_dir,
        plan["identity"],
        context["grid"],
        (1536, 1536),
        keys,
        max_bytes=cfg.max_store_bytes,
    ) as writer:
        writer.write_tile(0, 0, arrays)
    result = api.run_tile_preparation(cfg, stage="prepare", resume=True)
    assert result["status"] == "complete"
    assert calls == [(0, 384), (384, 0)]
    store = PreparedTileStore(cfg.store_dir, expected_identity=plan["identity"])
    store.validate()
    assert api.run_tile_preparation(cfg, stage="validate")["status"] == "validated"


def test_centroid_variance_bound_contains_all_observers_targets_and_rays():
    import math

    import numpy as np
    from shapely import get_coordinates
    from shapely.geometry import Polygon

    from viewshed_toolkit.pipeline.prepare.area.transmission_envelope import centroid_read_envelope

    cell = Polygon([(0, 0), (1700, 0), (1900, 600), (1100, 1400), (-200, 1100)])
    envelope = centroid_read_envelope(cell, 24140.16)
    center = np.asarray(envelope["centroid_xy"])
    assert envelope["raster_margin_m"] == 150.0
    for source in get_coordinates(cell):
        for angle in np.linspace(0, 2 * math.pi, 32):
            target = source + 24140.16 * np.array([math.cos(angle), math.sin(angle)])
            assert np.linalg.norm(target - center) <= envelope["radius_m"]
            for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                point = source + fraction * (target - source)
                assert np.linalg.norm(point - center) <= envelope["radius_m"]


def test_explicit_30km_radius_preserves_default_and_rejects_larger(tmp_path):
    payload = recipe(tmp_path)
    assert (
        TransmissionTileConfig.model_validate_json(json.dumps(payload)).max_distance_m == 24140.16
    )
    payload["max_distance_m"] = 30000.0
    assert TransmissionTileConfig.model_validate_json(json.dumps(payload)).max_distance_m == 30000.0
    payload["max_distance_m"] = 30000.01
    with pytest.raises(ValidationError):
        TransmissionTileConfig.model_validate_json(json.dumps(payload))


def absence_metadata(tmp_path):
    from viewshed_toolkit.pipeline.contracts.transmission import digest

    subset = tmp_path / "buildings.sqlite"
    subset.write_bytes(b"qualified empty fixture")
    receipt = tmp_path / "acquisition.json"
    receipt.write_text(
        json.dumps(
            dict(
                status="completed_validated",
                subset_path=str(subset),
                subset_sha256=digest(subset),
                study_bbox=[-125.0, 45.0, -120.0, 50.0],
                files=[
                    dict(tile="w125_n50_w120_n45", role=role, status="validated", sha256="a" * 64)
                    for role in ["polygon", "odbl", "height"]
                ],
            )
        )
    )
    return dict(
        building_subset=str(subset),
        building_inventory_policy="assume_complete",
        input_sha256={str(subset): digest(subset), str(receipt): digest(receipt)},
        building_absence_qualification=dict(
            contract="gba_covered_absence_assumption_v1",
            user_approved=True,
            canopy_nodata_policy="retain_unknown",
            acquisition_receipt=dict(path=str(receipt), sha256=digest(receipt)),
        ),
    )


def test_empty_covered_inventory_is_zero_but_missing_footprint_height_is_unknown(tmp_path):
    from shapely.geometry import box

    from viewshed_toolkit.pipeline.prepare.elevation.buildings import rasterize_building_footprints

    coverage = api._building_absence_coverage(absence_metadata(tmp_path))
    assert coverage.covers(box(-123.4, 48.0, -123.2, 48.2))
    options = dict(
        shape=(6, 6),
        transform=Affine(10, 0, 0, 0, -10, 60),
        crs=3857,
        inventory_policy="assume_complete",
        all_touched=False,
    )
    empty = rasterize_building_footprints([], **options)
    assert not empty.present.any() and not empty.unknown.any()
    assert (empty.lower_height == 0).all()
    missing = rasterize_building_footprints([(box(20, 20, 40, 40).wkb, None)], **options)
    assert missing.present.any()
    assert (missing.unknown == missing.present).all()
    assert missing.missing_height_footprint_count == 1


@pytest.mark.parametrize(
    "damage", ["failed", "missing_role", "missing_receipt", "subset", "pin", "approval", "canopy"]
)
def test_absence_requires_successful_pinned_acquisition(tmp_path, damage):
    from viewshed_toolkit.pipeline.contracts.transmission import digest

    metadata = absence_metadata(tmp_path)
    evidence = metadata["building_absence_qualification"]
    receipt = Path(evidence["acquisition_receipt"]["path"])
    payload = json.loads(receipt.read_text())
    if damage == "failed":
        payload["files"][0]["status"] = "failed"
    elif damage == "missing_role":
        payload["files"].pop()
    elif damage == "missing_receipt":
        receipt.unlink()
    elif damage == "subset":
        payload["subset_sha256"] = "b" * 64
    elif damage == "pin":
        metadata["input_sha256"].pop(str(receipt))
    elif damage == "approval":
        evidence["user_approved"] = False
    elif damage == "canopy":
        evidence["canopy_nodata_policy"] = "zero"
    if damage in {"failed", "missing_role", "subset"}:
        receipt.write_text(json.dumps(payload))
        evidence["acquisition_receipt"]["sha256"] = digest(receipt)
        metadata["input_sha256"][str(receipt)] = digest(receipt)
    with pytest.raises((ValueError, FileNotFoundError)):
        api._building_absence_coverage(metadata)


def test_absence_rejects_unsupported_acquisition_area(tmp_path):
    coverage = api._building_absence_coverage(absence_metadata(tmp_path))
    with pytest.raises(ValueError, match="outside acquired coverage"):
        api._require_building_coverage(coverage, (0, 0, 100, 100))


def canopy_gap_metadata(tmp_path):
    import numpy as np
    import rasterio

    from viewshed_toolkit.pipeline.contracts.transmission import digest

    path = tmp_path / "canopy.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=2,
        width=2,
        count=1,
        dtype="uint8",
        crs=4326,
        transform=Affine(2.5, 0, -125, 0, -2.5, 50),
        nodata=255,
    ) as dst:
        dst.write(np.array([[0, 255], [8, 0]], dtype="uint8"), 1)
    return dict(
        canopy_gap_policy="assume_zero_acquired",
        canopy_assets=[dict(path=str(path))],
        input_sha256={str(path): digest(path)},
        canopy_gap_qualification=dict(
            contract="eth_acquired_gap_zero_assumption_v1",
            user_approved=True,
            basis="existing_pinned_native_rasters",
        ),
    )


def test_acquired_canopy_coverage_is_not_sensor_validity(tmp_path):
    from shapely.geometry import box

    metadata = canopy_gap_metadata(tmp_path)
    coverage = api._canopy_gap_coverage(metadata)
    assert coverage.covers(box(-123.3, 48.1, -123.2, 48.2))
    with pytest.raises(ValueError, match="outside acquired raster coverage"):
        api._require_canopy_coverage(coverage, (0, 0, 100, 100))


@pytest.mark.parametrize(
    "damage", ["missing_assets", "missing_file", "changed_bytes", "unapproved", "unqualified_basis"]
)
def test_failed_or_missing_canopy_acquisition_is_never_zero(tmp_path, damage):
    metadata = canopy_gap_metadata(tmp_path)
    if damage == "missing_assets":
        metadata["canopy_assets"] = []
    elif damage == "missing_file":
        Path(metadata["canopy_assets"][0]["path"]).unlink()
    elif damage == "changed_bytes":
        metadata["input_sha256"][metadata["canopy_assets"][0]["path"]] = "b" * 64
    elif damage == "unapproved":
        metadata["canopy_gap_qualification"]["user_approved"] = False
    else:
        metadata["canopy_gap_qualification"]["basis"] = "global_coverage_claim_only"
    with pytest.raises((ValueError, FileNotFoundError)):
        api._canopy_gap_coverage(metadata)


def test_canopy_gap_policy_metadata_changes_prepared_identity(tmp_path, monkeypatch):
    cfg = TransmissionTileConfig.model_validate_json(json.dumps(recipe(tmp_path)))
    cell = h3.latlng_to_cell(48.5, -123.0, 7)
    polygon = transform(
        Transformer.from_crs(4326, 32610, always_xy=True).transform,
        Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]),
    )
    water = polygon.buffer(500).difference(polygon)
    context = dict(
        grid=Affine(*cfg.global_transform),
        cells=[cell],
        land=polygon,
        water=water,
        reporting_water=water,
        metadata=dict(input_sha256={}),
    )
    monkeypatch.setattr(api, "implementation_identity", lambda: {"fixture": "stable"})
    original = api._plan(cfg, context)["identity"]
    metadata = canopy_gap_metadata(tmp_path)
    context["metadata"] = metadata
    changed_cfg = cfg.model_copy(
        update=dict(
            provisioned_metadata=cfg.provisioned_metadata.model_copy(update=dict(sha256="2" * 64))
        )
    )
    assumed = api._plan(changed_cfg, context)["identity"]
    assert original != assumed
    assert assumed["metadata_sha256"] != original["metadata_sha256"]
