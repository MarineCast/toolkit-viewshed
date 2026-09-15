"""Synthetic coastal fixture at Admiralty Inlet coordinates; no downloaded data."""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import polars as pl
import pytest
import rasterio
import yaml
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box

from viewshed_toolkit import load_app_config, run_component_stage, run_components
from viewshed_toolkit.pipeline.api.registry import component_plan
from viewshed_toolkit.pipeline.config.datasets import DatasetConfig
from viewshed_toolkit.pipeline.contracts.components import component_path, validate_pairs
from viewshed_toolkit.pipeline.providers.base import Asset, FileProvider


def test_real_provider_discovery_is_deterministic():
    from viewshed_toolkit.pipeline.providers import get_provider

    bbox = (-122.78, 48.12, -122.75, 48.14)
    for name, release in (("usgs_3dep", "live"), ("global_canopy_height", "2020")):
        config = DatasetConfig(provider=name, version=release, resolution_m=30)
        provider = get_provider(name)
        first = provider.discover(bbox, config)
        assert first == provider.discover(bbox, config)
        assert first and all(asset.url.startswith("https://") for asset in first)
        if name == "usgs_3dep":
            assert all(len(asset.bbox) == 4 for asset in first)
            assert first[0].bbox[0] == bbox[0]


def coastal_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    raw.pop("area")
    raw["region"].update(
        name="synthetic_admiralty_inlet",
        bbox_wgs84=dict(min_lon=-122.78, min_lat=48.12, max_lon=-122.75, max_lat=48.14),
    )
    raw["viewshed"].update(
        dem_resolution_m=100,
        max_distance_m=1000,
        aoi_margin_m=100,
        observer_canopy_clearance_radius_m=100,
    )
    raw["h3"].update(
        source_resolution=8,
        target_resolution=8,
        sample_points_per_source_cell=2,
        min_sample_points_per_source_cell=1,
    )
    raw["batch"].update(max_workers=1, batch_size_cells=3)
    raw["distance_weight"]["hard_cutoff_km"] = 1
    raw["source_target_lookup"].update(max_distance_km_land=1, max_distance_km_water=1)
    raw["run"]["version"] = "synthetic_fixture_v1"
    raw["paths"] = {
        "land_polygon_path": str(root / "land.geojson"),
        "water_polygon_path": str(root / "water.parquet"),
        "regional_dem_path": str(root / "prepared" / "dem.tif"),
        "canopy_height_path": str(root / "prepared" / "chm.tif"),
        "raw_dem_dir": str(root / "raw_dem"),
        "output_dir": str(root / "work"),
        "final_output_dir": str(root / "final"),
        "map_dir": str(root / "maps"),
        "land_h3_path": str(root / "work" / "land.parquet"),
        "source_cells_path": str(root / "work" / "land.parquet"),
        "projected_dem_path": str(root / "work" / "projected.tif"),
    }
    raw["datasets"] = {
        name: {
            "provider": "local",
            "version": "synthetic_v1",
            "resolution_m": 100,
            "assets": [str(root / f"{name}.tif")],
        }
        for name in ("dem", "chm")
    }
    transformer = Transformer.from_crs(4326, 32610, always_xy=True)
    x, y = transformer.transform(-122.765, 48.13)
    x, y = round(x / 100) * 100, round(y / 100) * 100
    land = box(x - 5000, y - 5000, x, y + 5000)
    water = box(x, y - 5000, x + 5000, y + 5000)
    gpd.GeoDataFrame(geometry=[land], crs=32610).to_crs(4326).to_file(
        root / "land.geojson", driver="GeoJSON"
    )
    gpd.GeoDataFrame(geometry=[water], crs=32610).to_crs(4326).to_parquet(root / "water.parquet")
    rows, cols = np.indices((100, 100))
    dem = np.where(cols < 50, 5 + (50 - cols) * 0.5 + 5 * np.sin(rows / 7), 0).astype("float32")
    chm = np.where((cols > 40) & (cols < 49), 12, 0).astype("float32")
    for name, values in (("dem", dem), ("chm", chm)):
        with rasterio.open(
            root / f"{name}.tif",
            "w",
            driver="GTiff",
            width=100,
            height=100,
            count=1,
            dtype="float32",
            nodata=-9999,
            crs=32610,
            transform=from_origin(x - 5000, y + 5000, 100, 100),
        ) as out:
            out.write(values, 1)
    config = root / "fixture.yaml"
    config.write_text(yaml.safe_dump(raw))
    return config


def test_component_dag_independence():
    assert "download-chm" not in component_plan(("build-dem-weights",))
    assert "build-distance-weights" not in component_plan(("build-chm-weights",))
    assert not any(
        "dem" in name or "chm" in name for name in component_plan(("build-distance-weights",))
    )
    with pytest.raises(ValueError, match="Unknown"):
        component_plan(("typo",))


def test_provider_cache_and_checksum(tmp_path):
    config = coastal_fixture(tmp_path)
    provider = FileProvider()
    asset = Asset("dem", str(tmp_path / "dem.tif"))
    first = provider.download([asset], tmp_path / "cache")
    stamp = first.paths[0].stat().st_mtime_ns
    second = provider.download([asset], tmp_path / "cache")
    assert second.paths[0].stat().st_mtime_ns == stamp
    first.paths[0].write_bytes(b"corrupt")
    provider.download([asset], tmp_path / "cache")
    with rasterio.open(first.paths[0]) as source:
        assert source.count == 1
    with pytest.raises(ValueError, match="Checksum"):
        provider.download([Asset("dem", asset.url, "0" * 64)], tmp_path / "cache")
    assert load_app_config(config).region.name == "synthetic_admiralty_inlet"


@pytest.mark.parametrize("values", [[None], [float("nan")], [-0.1], [1.1]])
def test_component_invalid_weights(values):
    frame = pl.DataFrame(
        {"source_h3": ["a"], "target_h3": ["b"], "w": values}, schema_overrides={"w": pl.Float64}
    )
    with pytest.raises(ValueError, match="finite"):
        validate_pairs(frame, ("w",))


def test_component_config_rejects_unknown():
    with pytest.raises(ValueError):
        DatasetConfig(provider="local", version="test", resolution_m=30, typo=True)
    with pytest.raises(ValueError, match="Unknown dataset provider"):
        DatasetConfig(provider="typo", version="test", resolution_m=30)


def test_distance_pipeline_independent_of_rasters(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.api import components

    config = coastal_fixture(tmp_path)
    (tmp_path / "dem.tif").unlink()
    (tmp_path / "chm.tif").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("distance-only run touched a raster/LOS stage")

    with monkeypatch.context() as raster_guard:
        for name in (
            "download_dataset",
            "prepare_dataset",
            "build_dem_component",
            "build_chm_component",
            "compose_components",
        ):
            raster_guard.setattr(components, name, forbidden)
        outputs = run_components(config, target="distance")
    assert outputs["build-pair-distances"].exists()
    assert outputs["build-distance-weights"].exists()
    assert "prepare-dem" not in outputs
    assert "prepare-chm" not in outputs
    assert "compose-static-weights" not in outputs
    assert "export-maps" not in outputs
    run_component_stage(config, "build-dem-weights", source_type="water")


def test_dem_pipeline_independent_of_chm(tmp_path):
    pytest.importorskip("osgeo.gdal")
    config = coastal_fixture(tmp_path)
    (tmp_path / "chm.tif").unlink()
    outputs = run_components(config, target="dem")
    assert outputs["build-dem-weights"].exists()
    assert "download-chm" not in outputs
    assert not component_path(load_app_config(config), "distance").exists()


@pytest.mark.parametrize("keys", [([None], ["b"]), ([""], ["b"]), (["a", "a"], ["b", "b"])])
def test_component_pair_key_validation(keys):
    with pytest.raises(ValueError, match=r"key|pair"):
        validate_pairs(
            pl.DataFrame(
                {"source_h3": keys[0], "target_h3": keys[1]},
                schema_overrides={"source_h3": pl.String},
            )
        )


def test_small_coastal_pipeline(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal")
    config = coastal_fixture(tmp_path)
    outputs = run_components(config, run_id="fixture")
    app = load_app_config(config)
    final = pl.read_parquet(component_path(app, "static"))
    assert final.height > 1
    assert final["source_h3"].n_unique() > 1
    validate_pairs(
        final,
        ("weight_terrain", "weight_vegetation", "weight_distance", "weight_static_viewability"),
    )
    np.testing.assert_allclose(
        final["weight_static_viewability"], final["weight_terrain"] * final["weight_vegetation"]
    )
    assert outputs["validate"].exists()
    map_bytes = outputs["export-maps"].read_bytes()
    assert b"component-legend" in map_bytes
    assert b"Candidate source cells" in map_bytes
    assert b"Color cap:" in map_bytes
    assert b"calc(100vw - 24px)" in map_bytes
    run_component_stage(app, "export-maps")
    assert outputs["export-maps"].read_bytes() == map_bytes
    timestamps = {
        name: component_path(app, name).stat().st_mtime_ns for name in ("dem", "chm", "distance")
    }
    for stage in ("build-dem-weights", "build-chm-weights", "build-distance-weights"):
        run_component_stage(app, stage)
    assert timestamps == {name: component_path(app, name).stat().st_mtime_ns for name in timestamps}
    manifest = json.loads(
        (tmp_path / "final" / "components" / "manifests" / "fixture-land.json").read_text()
    )
    assert manifest["complete"]
    assert all(metric["wall_seconds"] > 0 for metric in manifest["metrics"])

    # Compare against the pre-refactor paired execution, including canopy grounding.
    from viewshed_toolkit.pipeline.weights.canopy_visibility import run_dual_surface_canopy_weights

    paired = run_dual_surface_canopy_weights(config)
    expected = pl.read_parquet(paired.dual_surface_factors).sort(["source_h3", "target_h3"])
    actual = final.sort(["source_h3", "target_h3"])
    assert actual.select("source_h3", "target_h3").equals(expected.select("source_h3", "target_h3"))
    np.testing.assert_allclose(actual["weight_terrain"], expected["weight_terrain"], atol=1e-7)
    np.testing.assert_allclose(
        actual["weight_vegetation"], expected["weight_vegetation"], atol=1e-7
    )

    # Water keeps its land-mask policy and canopy is explicitly not applicable.
    for stage in (
        "build-dem-weights",
        "build-chm-weights",
        "build-distance-weights",
        "compose-static-weights",
        "finalize",
        "validate",
    ):
        run_component_stage(app, stage, source_type="water")
    water = pl.read_parquet(component_path(app, "static", "water"))
    assert water.height > 0
    assert water["canopy_support"].unique().to_list() == ["not_applicable"]
    np.testing.assert_array_equal(water["weight_static_viewability"], water["weight_terrain"])

    import pandas as pd

    from viewshed_toolkit.pipeline.api import regional
    from viewshed_toolkit.pipeline.weights.terrain import runner

    monkeypatch.setattr(regional, "default_config_path", lambda: config)
    assert regional.validate_region(config)["valid"]
    with monkeypatch.context() as failed_run:
        failed_run.setattr(
            runner,
            "run_source_cells",
            lambda _: pd.DataFrame(
                {
                    "source_h3_cell": final["source_h3"].unique().to_list(),
                    "status": "failed",
                }
            ),
        )
        with pytest.raises(ValueError, match="successful completion"):
            run_component_stage(app, "build-dem-weights", overwrite=True)

    # Input mutations invalidate composition before any durable output is touched.
    static_path = component_path(app, "static")
    stamp = static_path.stat().st_mtime_ns
    with rasterio.open(app.paths.canopy_height_path, "r+") as chm:
        values = chm.read(1)
        values[values > 0] += 5
        chm.write(values, 1)
    with pytest.raises(ValueError, match="stale"):
        run_component_stage(app, "compose-static-weights")
    with pytest.raises(ValueError, match="Stale"):
        regional.validate_region(config)
    assert static_path.stat().st_mtime_ns == stamp

    with pytest.raises(FileExistsError, match="another logical run"):
        run_components(config, run_id="fixture", target="distance")
    assert static_path.stat().st_mtime_ns == stamp


def test_component_writer_rejects_invalid_h3_before_writing(tmp_path):
    from viewshed_toolkit.pipeline.contracts.components import write_component

    frame = pl.DataFrame({"source_h3": ["not-h3"], "target_h3": ["not-h3"], "weight": [0.5]})
    path = tmp_path / "invalid.parquet"
    with pytest.raises(ValueError, match="Invalid H3"):
        write_component(
            frame, path, {"source_h3_resolution": 8, "target_h3_resolution": 8}, weights=("weight",)
        )
    assert not path.exists()


def test_component_run_rejects_source_role_before_writing(tmp_path):
    config = coastal_fixture(tmp_path)
    with pytest.raises(ValueError, match="source_type"):
        run_components(config, source_type="../../escape")
    assert not (tmp_path / "final").exists()


def test_chm_preparation_independent_of_dem(tmp_path):
    pytest.importorskip("osgeo.gdal")
    config = coastal_fixture(tmp_path)
    (tmp_path / "dem.tif").unlink()
    run_component_stage(config, "download-chm")
    output = run_component_stage(config, "prepare-chm")
    assert output.exists()
    assert not load_app_config(config).paths.regional_dem_path.exists()


def test_failed_http_refresh_preserves_cached_source(tmp_path, monkeypatch):
    import requests

    coastal_fixture(tmp_path)
    content = (tmp_path / "dem.tif").read_bytes()

    class Response:
        fail = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_content(self, **kwargs):
            yield content[:100]
            if self.fail:
                raise requests.ConnectionError("interrupted test download")
            yield content[100:]

    response = Response()
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    provider = FileProvider()
    asset = Asset("test-dem", "https://example.invalid/dem.tif")
    output = provider.download([asset], tmp_path / "cache").paths[0]
    original = output.read_bytes()
    response.fail = True
    with pytest.raises(requests.ConnectionError):
        provider.download([asset], tmp_path / "cache", cache=False)
    assert output.read_bytes() == original
    assert not list(output.parent.glob("*.tmp.tif"))


@pytest.mark.parametrize("native_error", [False, True])
def test_metrics_preserve_disk_when_process_tree_is_unavailable(
    tmp_path, monkeypatch, native_error
):
    from types import SimpleNamespace

    import psutil

    from viewshed_toolkit._internal.performance import measure_stage

    class Process:
        def children(self, **kwargs):
            if native_error:
                raise PermissionError("process enumeration denied")
            raise psutil.AccessDenied()

        def memory_info(self):
            return SimpleNamespace(rss=1234)

    monkeypatch.setattr(psutil, "Process", Process)
    with measure_stage(tmp_path) as metrics:
        (tmp_path / "scratch").write_bytes(b"12345")
    assert metrics["peak_rss_bytes"] == 1234
    assert metrics["rss_scope"] == "parent_only"
    assert metrics["rss_sampling_partial"] is True
    assert metrics["peak_additional_work_disk_bytes"] == 5
    assert metrics["disk_sampling_partial"] is False

    def inaccessible(self):
        raise psutil.AccessDenied()

    monkeypatch.setattr(Process, "memory_info", inaccessible)
    with measure_stage(tmp_path) as unavailable:
        pass
    assert unavailable["peak_rss_bytes"] is None
    assert unavailable["peak_work_disk_bytes"] == 5


def test_component_dem_is_invariant_to_larger_batches(tmp_path):
    import numpy as np

    config = coastal_fixture(tmp_path)
    run_components(config, target="dem", run_id="small-batches")
    before = pl.read_parquet(component_path(load_app_config(config), "dem")).sort(
        ["source_h3", "target_h3"]
    )
    raw = yaml.safe_load(config.read_text())
    raw["batch"]["batch_size_cells"] = 49
    raw["batch"]["max_estimated_batch_memory_mb"] = 1536
    config.write_text(yaml.safe_dump(raw))
    run_components(config, target="dem", run_id="larger-batches", overwrite=True)
    after = pl.read_parquet(component_path(load_app_config(config), "dem")).sort(
        ["source_h3", "target_h3"]
    )
    assert before.select("source_h3", "target_h3").equals(after.select("source_h3", "target_h3"))
    np.testing.assert_allclose(before["weight_terrain"], after["weight_terrain"], rtol=0, atol=1e-7)


def test_canopy_discovery_excludes_only_tiles_without_mapped_land(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.api import acquisition
    from viewshed_toolkit.pipeline.contracts.components import component_root
    from viewshed_toolkit.pipeline.providers.base import DownloadResult

    config = coastal_fixture(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["datasets"]["chm"] = {
        "provider": "global_canopy_height",
        "version": "2020",
        "resolution_m": 10,
        "land_tiles_only": True,
    }
    config.write_text(yaml.safe_dump(raw))

    class Provider:
        def discover(self, bbox, settings):
            return [
                Asset("N48W123", "https://example.invalid/land.tif"),
                Asset("N48W129", "https://example.invalid/ocean.tif"),
            ]

        def download(self, assets, destination, **kwargs):
            assert [asset.id for asset in assets] == ["N48W123"]
            return DownloadResult((tmp_path / "chm.tif",), tuple(assets))

    monkeypatch.setattr(acquisition, "get_provider", lambda name: Provider())
    app = load_app_config(config)
    acquisition.download_dataset(app, "chm")
    manifest = json.loads((component_root(app) / "inputs/chm/download.json").read_text())
    assert manifest["excluded_assets"] == [
        {"id": "N48W129", "reason": "no_mapped_land_in_acquisition_area"}
    ]
    assert manifest["assets"][0]["id"] == "N48W123"
