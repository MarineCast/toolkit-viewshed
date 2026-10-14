"""Supported scenario contracts and real tiny-package scoring; no regional replay."""

import importlib.util
import json
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import polars as pl
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box, mapping

from viewshed_toolkit import (
    load_transmission_config,
    run_transmission,
    validate_transmission_product,
)
from viewshed_toolkit.pipeline.api import transmission as api
from viewshed_toolkit.pipeline.config.transmission import METHOD
from viewshed_toolkit.pipeline.contracts.transmission import (
    DTYPES,
    OBSERVER_GROUND_CONTRACT,
    PREPARED_CONTRACT,
    digest,
)
from viewshed_toolkit.pipeline.weights.transmission import calculate


def fixture_config(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    files = {}
    for name in (
        "coastline",
        "terrain_vrt",
        "terrain_provenance",
        "canopy_inventory",
        "worldcover_manifest",
        "gba_subset",
        "gba_manifest",
    ):
        path = inputs / name
        path.write_text("{}")
        files[name] = path
    files["canopy_inventory"].write_text('{"raw_canopy":[]}')
    files["worldcover_manifest"].write_text('{"assets":[]}')
    payload = dict(
        schema_version=2,
        method=METHOD,
        output_dir=str(tmp_path / "product"),
        source_bbox_wgs84=[-123.01, 48.0, -123.0, 48.01],
        inputs={key: dict(path=str(p), sha256=digest(p)) for key, p in files.items()},
        qualification=dict(
            terrain_vertical_reference="unverified",
            water_vertical_reference="declared_zero",
            vertical_units="m",
            canopy_height_reference="above_ground",
            building_height_reference="above_ground",
            source_epochs={
                k: "synthetic fixture"
                for k in ("terrain", "canopy", "buildings", "worldcover", "coastline")
            },
            source_licenses={
                k: "synthetic fixture"
                for k in ("terrain", "canopy", "buildings", "worldcover", "coastline")
            },
            empirical_status="uncalibrated_scenario",
        ),
        source_samples=3,
        max_distance_m=120.0,
        workers=1,
        memory_budget_gib=6.0,
        target_stride=1,
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    return path, payload


def small_prepared(config):
    """Caller-qualified tiny prepared-grid fixture; the scorer is never mocked."""
    root = Path(config["output_dir"])
    (root / "prepared").mkdir(parents=True)
    grid = Affine(10, 0, 499500, 0, -10, 5370500)
    shape = (90, 90)
    land = box(499910, 5370000, 499940, 5370030)
    water = box(499950, 5369940, 500060, 5370070)
    x, y = land.centroid.x, land.centroid.y
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(x, y)
    cell = h3.latlng_to_cell(lat, lon, 7)
    for key, dtype in DTYPES.items():
        data = np.zeros(shape, dtype=dtype)
        if key == "observer_ground":
            data[:] = 2
        if key == "classes":
            data[:] = 30
        np.save(root / "prepared" / f"{key}.npy", data, allow_pickle=False)
    (root / "prepared.json").write_text(
        json.dumps(
            dict(
                contract=PREPARED_CONTRACT,
                observer_ground_contract=OBSERVER_GROUND_CONTRACT,
                shape=list(shape),
                transform=list(grid)[:6],
            )
        )
    )
    (root / "source_cells.json").write_text(
        json.dumps([dict(source_h3=cell, source_land=mapping(land))])
    )
    for name, geometry in [("land_area", land), ("water_area", water)]:
        gpd.GeoDataFrame({"layer": [name]}, geometry=[geometry], crs=32610).to_parquet(
            root / f"{name}.parquet"
        )


def test_supported_package_scores_and_validates_small_generation(tmp_path, monkeypatch):
    request, payload = fixture_config(tmp_path)
    monkeypatch.setattr(api, "prepare", small_prepared)
    result = run_transmission(request)
    assert result["status"] == "complete_scenario"
    assert result["datum_status"] == "unqualified"
    assert result["census_checked_sources"] == 1
    assert result["pair_count"] > 0
    root = Path(payload["output_dir"])
    pairs = pl.read_parquet(root / "average_viewability.parquet")
    assert pairs["observer_count"].unique().to_list() == [3]
    assert pairs["method"].unique().to_list() == [METHOD]
    assert pairs["above_ground_status"].unique().to_list() == ["known"]
    assert pairs["weight_above_ground"].to_list() == [1.0] * pairs.height
    np.testing.assert_allclose(
        pairs["average_viewability"].to_list(), 1 - pairs["out_of_range_fraction"].to_numpy()
    )
    assert all(0 < x < 1 for x in pairs["weight_terrain"])
    assert validate_transmission_product(root)["pair_count"] == pairs.height
    assert run_transmission(request, stage="validate")["pair_count"] == pairs.height
    with pytest.raises(ValueError, match="new transmission"):
        run_transmission(request)
    # Source ground provenance survives publication and rejects a changed prepared surface.
    altered = np.load(root / "prepared/observer_ground.npy")
    altered[0, 0] = 5
    np.save(root / "prepared/observer_ground.npy", altered, allow_pickle=False)
    with pytest.raises(ValueError, match="Prepared transmission inputs changed"):
        validate_transmission_product(root)


def test_config_is_strict_read_only_and_resolves_paths(tmp_path):
    request, payload = fixture_config(tmp_path)
    cfg = load_transmission_config(request)
    assert cfg.method == METHOD
    assert not Path(cfg.output_dir).exists()
    for key, value in [("workers", True), ("schema_version", 1), ("method", "legacy"), ("typo", 1)]:
        request.write_text(json.dumps(dict(payload, **{key: value})))
        with pytest.raises(ValueError):
            load_transmission_config(request)
    assert not Path(cfg.output_dir).exists()


def test_input_change_rejected_before_preparation(tmp_path):
    request, payload = fixture_config(tmp_path)
    Path(payload["inputs"]["coastline"]["path"]).write_text("changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        run_transmission(request)
    assert not Path(payload["output_dir"]).exists()


def test_demo_calculation_has_exact_supported_path_parity():
    # Compare a known clear, roof-blocked, and genuinely uncertain canopy ray.
    path = Path(__file__).parents[2] / "scripts/extend_viewability_demo.py"
    spec = importlib.util.spec_from_file_location("demo_parity", path)
    import sys

    sys.path.insert(0, str(path.parent))
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    shape = (9, 30)
    data = {
        k: np.zeros(
            shape,
            dtype=(
                bool
                if "unknown" in k or k in ("qualified_water", "building_present")
                else "float32"
            ),
        )
        for k in (
            "ground",
            "canopy",
            "canopy_unknown",
            "building",
            "building_unknown",
            "building_present",
            "qualified_water",
        )
    }
    data["transform"] = Affine(10, 0, 0, 0, -10, 90)
    classes = np.full(shape, 30, dtype="uint8")
    observers = np.array([[15.0, 45.0, 2.0], [15.0, 55.0, 2.0]])
    targets = np.array([[285.0, 45.0], [285.0, 55.0], [285.0, 65.0]])
    codes, weights, cells = np.array([0, 0, 1]), np.array([2.0, 1.0, 1.0]), np.array(["a", "b"])
    reference = json.loads(
        (Path(__file__).parents[1] / "fixtures/direct_transmission_demo_reference.json").read_text()
    )["scenarios"]
    for mode in ("clear", "roof", "unknown", "transmission"):
        data["canopy"][:] = 0
        data["building"][:] = 0
        data["building_present"][:] = False
        data["canopy_unknown"][:] = False
        if mode == "roof":
            data["building"][:, 10] = 20
            data["building_present"][:, 10] = True
        if mode == "unknown":
            data["canopy_unknown"][:, 10] = True
        if mode == "transmission":
            data["canopy"][:, 10] = 15
        args = (data, classes, observers, targets, codes, weights, cells, 300)
        actual, _ = calculate(*args)
        legacy, _ = demo.calculate(*args)
        for key in ("lower", "upper", "unknown", "out_of_range"):
            np.testing.assert_array_equal(actual[key], legacy[key])
        assert actual["score"] == legacy["score"]
        for key in ("lower", "upper", "unknown", "out_of_range"):
            np.testing.assert_allclose(actual[key], reference[mode][key], atol=1e-12, rtol=0)
        assert actual["score"] == reference[mode]["score"]
    assert demo.calculate is calculate


def test_native_height_units_and_decode_state_are_enforced(tmp_path):
    import rasterio

    from viewshed_toolkit.pipeline.prepare.elevation.transmission import align

    path = tmp_path / "height.tif"
    grid = Affine(10, 0, 500000, 0, -10, 5370000)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        count=1,
        width=3,
        height=3,
        dtype="float32",
        crs=32610,
        transform=grid,
    ) as dst:
        dst.write(np.full((3, 3), 10.0, dtype="float32"), 1)
        dst.update_tags(height_units="ft")
    with pytest.raises(ValueError, match="metres"):
        align(path, (3, 3), grid, nodata=np.nan, dtype="float32")
    with rasterio.open(path, "r+") as dst:
        dst.update_tags(height_units="m")
        dst.scales = (0.1,)
    with pytest.raises(ValueError, match="scale/offset"):
        align(path, (3, 3), grid, nodata=np.nan, dtype="float32")


def test_empty_water_population_remains_explicit_and_has_no_zero_pairs(tmp_path, monkeypatch):
    request, payload = fixture_config(tmp_path)

    def empty_prepared(config):
        small_prepared(config)
        root = Path(config["output_dir"])
        gpd.GeoDataFrame(
            {"layer": ["water_area"]}, geometry=[box(1, 1, 2, 2)], crs=32610
        ).to_parquet(root / "water_area.parquet")

    monkeypatch.setattr(api, "prepare", empty_prepared)
    result = run_transmission(request)
    assert result["pair_count"] == 0
    assert result["empty_target_sources"] == 1
    assert pl.read_parquet(Path(payload["output_dir"]) / "average_viewability.parquet").is_empty()
    info = json.loads(next((Path(payload["output_dir"]) / "partitions").glob("*.json")).read_text())
    assert info["source_state"] == "no_target_population" and info["lower"] is None


def test_real_tiny_preparation_preserves_unflattened_ground(tmp_path):
    import sqlite3

    import rasterio
    from rasterio.warp import transform_bounds
    from shapely.geometry import Polygon
    from shapely.ops import transform as project

    from viewshed_toolkit.pipeline.prepare.transmission import load_cache

    request, payload = fixture_config(tmp_path)
    cell = h3.latlng_to_cell(48.0, -123.0, 7)
    boundary = Polygon([(x, y) for y, x in h3.cell_to_boundary(cell)])
    payload["source_bbox_wgs84"] = list(boundary.bounds)
    projected = project(Transformer.from_crs(4326, 32610, always_xy=True).transform, boundary)
    left, bottom, right, top = projected.buffer(600).bounds
    left, bottom = np.floor(np.array([left, bottom]) / 30) * 30
    right, top = np.ceil(np.array([right, top]) / 30) * 30
    grid = Affine(10, 0, left, 0, -10, top)
    size = (int((top - bottom) / 10), int((right - left) / 10))
    to_w = Transformer.from_crs(32610, 4326, always_xy=True).transform
    coast = tmp_path / "inputs/coastline.gpkg"
    mid = (left + right) / 2 + 4
    for name, geometry in [
        ("land_area", box(left - 100, bottom - 100, mid, top + 100)),
        ("water_area", box(mid, bottom - 100, right + 100, top + 100)),
    ]:
        gpd.GeoDataFrame({"layer": [name]}, geometry=[project(to_w, geometry)], crs=4326).to_file(
            coast, layer=name, driver="GPKG"
        )
    assets = {}
    for name, value, dtype, nodata in [
        ("terrain", 2.0, "float32", -9999.0),
        ("canopy", 0, "uint8", 255),
        ("classes", 30, "uint8", 0),
    ]:
        path = tmp_path / "inputs" / f"{name}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=size[0],
            width=size[1],
            count=1,
            dtype=dtype,
            transform=grid,
            crs=32610,
            nodata=nodata,
        ) as dst:
            dst.write(np.full(size, value, dtype=dtype), 1)
            if name != "classes":
                dst.update_tags(height_units="m")
        assets[name] = dict(
            path=str(path),
            sha256=digest(path),
            bounds=list(transform_bounds(32610, 4326, left, bottom, right, top)),
        )
    canopy = tmp_path / "inputs/canopy_inventory"
    canopy.write_text(json.dumps(dict(raw_canopy=[assets["canopy"]])))
    wc = tmp_path / "inputs/worldcover_manifest"
    wc.write_text(
        json.dumps(dict(assets=[assets["classes"]], bounds_wgs84=[-124.0, 47.0, -122.0, 49.0]))
    )
    subset = tmp_path / "inputs/buildings.sqlite"
    with sqlite3.connect(subset) as connection:
        connection.execute(
            "CREATE TABLE buildings (tile,key,source,id,region,height,variance,height_status,geometry_wkb,footprint_license,minx,maxx,miny,maxy)"
        )
    gba = tmp_path / "inputs/gba_manifest"
    gba.write_text(
        json.dumps(
            dict(
                status="completed_validated",
                subset_sha256=digest(subset),
                study_bbox=[-124.0, 47.0, -122.0, 49.0],
            )
        )
    )
    paths = {
        "coastline": coast,
        "terrain_vrt": Path(assets["terrain"]["path"]),
        "canopy_inventory": canopy,
        "worldcover_manifest": wc,
        "gba_subset": subset,
        "gba_manifest": gba,
    }
    for key, path in paths.items():
        payload["inputs"][key] = dict(path=str(path), sha256=digest(path))
    request.write_text(json.dumps(payload))
    result = run_transmission(request, stage="prepare")
    assert result["status"] == "prepared"
    root = Path(payload["output_dir"])
    metadata = json.loads((root / "prepared.json").read_text())
    arrays = load_cache(root, metadata)
    assert (arrays["ground"] == 0).any()
    assert np.all(arrays["observer_ground"] == 2)
    assert arrays["observer_ground"].shape == arrays["ground"].shape
    assert not (root / "manifest.json").exists()
