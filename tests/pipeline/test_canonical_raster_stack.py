from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.prepare.area import raster_stack
from viewshed_toolkit.pipeline.prepare.elevation import terrain


def _write_raster(path: Path, values: np.ndarray, *, nodata: float) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=from_origin(500_000.0, 5_400_000.0, 30.0, 30.0),
        nodata=nodata,
    ) as destination:
        destination.write(values.astype("float32"), 1)
    return path


def test_canonical_water_contract_uses_configured_land_source(tmp_path: Path) -> None:
    water_path = tmp_path / "water.parquet"
    land_path = tmp_path / "land.shp"
    water_path.write_bytes(b"water fixture")
    land_path.write_bytes(b"land fixture")
    app = load_app_config("configs/salish_sea.yaml")
    app = replace(
        app,
        paths=replace(
            app.paths,
            water_polygon_path=water_path,
            land_polygon_path=land_path,
        ),
    )

    contract = raster_stack._canonical_water_source_contract(app)

    members = contract["natural_earth_land"]["members"]
    assert members
    assert all("sha256" in member for member in members)
    assert str(app.paths.land_polygon_path.resolve()) in {member["path"] for member in members}


def test_canonical_raster_stack_builds_reuses_and_invalidates_canopy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dem_values = np.arange(20, dtype="float32").reshape(4, 5) + 100.0
    canopy_values = np.full((4, 5), 7.0, dtype="float32")
    dem_path = _write_raster(tmp_path / "projected_dem.tif", dem_values, nodata=-9999.0)
    canopy_path = _write_raster(tmp_path / "canopy.tif", canopy_values, nodata=255.0)
    water_path = tmp_path / "water.parquet"
    water_projected = gpd.GeoDataFrame(
        {"source": ["test_water"]},
        geometry=[box(500_001.0, 5_399_881.0, 500_029.0, 5_399_999.0)],
        crs="EPSG:32610",
    )
    water_projected.to_crs("EPSG:4326").to_parquet(water_path)

    base = load_app_config("configs/salish_sea.yaml")
    app = replace(
        base,
        paths=replace(
            base.paths,
            regional_dem_path=dem_path,
            projected_dem_path=dem_path,
            water_polygon_path=water_path,
            canopy_height_path=canopy_path,
        ),
        viewshed=replace(
            base.viewshed,
            crs_projected="EPSG:32610",
            dem_resolution_m=30,
            canopy_resampling="nearest",
            canopy_nodata_policy="zero",
            minimum_canopy_height_m=0.0,
        ),
    )
    monkeypatch.setattr(raster_stack, "ensure_projected_regional_dem", lambda _app: dem_path)
    monkeypatch.setattr(
        raster_stack,
        "_canonical_water_source_contract",
        lambda _app: {
            "water_polygon": raster_stack._canonical_dataset_signature(water_path),
            "natural_earth_land": {"test_fixture": True},
            "water_fill_algorithm": "test_fixture",
        },
    )
    monkeypatch.setattr(
        raster_stack,
        "load_and_clip_water",
        lambda _config, _aoi: (
            water_projected.to_crs("EPSG:4326"),
            water_projected,
        ),
    )
    raster_stack._CANONICAL_RASTER_STACK_READY_THIS_PROCESS.clear()

    stack = raster_stack.ensure_canonical_raster_stack(app, include_canopy=True)
    with rasterio.open(stack.water_mask_path) as water_source:
        water = water_source.read(1) == 1
    with rasterio.open(stack.endpoint_dem_path) as endpoint_source:
        endpoint = endpoint_source.read(1)
    with rasterio.open(stack.base_canopy_surface_path) as base_source:
        base_surface = base_source.read(1)

    assert np.count_nonzero(water) == 4
    np.testing.assert_array_equal(endpoint[water], np.zeros(4, dtype="float32"))
    np.testing.assert_array_equal(endpoint[~water], dem_values[~water])
    np.testing.assert_array_equal(base_surface[water], endpoint[water])
    np.testing.assert_array_equal(
        base_surface[~water],
        endpoint[~water] + canopy_values[~water],
    )
    core_metadata = json.loads(stack.core_metadata_path.read_text())
    canopy_metadata = json.loads(stack.canopy_metadata_path.read_text())
    assert core_metadata["fingerprint"] == stack.core_fingerprint
    assert set(core_metadata["outputs"]) == {"water_mask", "endpoint_dem"}
    assert canopy_metadata["fingerprint"] == stack.canopy_fingerprint
    assert set(canopy_metadata["outputs"]) == {
        "aligned_canopy",
        "base_canopy_surface",
    }

    batch_bounds = (500_000.0, 5_399_910.0, 500_090.0, 5_400_000.0)
    legacy_dem_clip = terrain.clip_raster_to_bounds(
        dem_path,
        tmp_path / "legacy_dem_clip.tif",
        batch_bounds,
        overwrite=True,
    )
    legacy_water = terrain.rasterize_water_to_match_dem(
        water_projected,
        legacy_dem_clip,
        tmp_path / "legacy_water.tif",
        overwrite=True,
    )
    legacy_endpoint = terrain.flatten_water_pixels_to_sea_level(
        legacy_dem_clip,
        legacy_water,
        tmp_path / "legacy_endpoint.tif",
        overwrite=True,
    )
    canonical_water_clip = terrain.clip_raster_to_bounds(
        stack.water_mask_path,
        tmp_path / "canonical_water_clip.tif",
        batch_bounds,
        overwrite=True,
    )
    canonical_endpoint_clip = terrain.clip_raster_to_bounds(
        stack.endpoint_dem_path,
        tmp_path / "canonical_endpoint_clip.tif",
        batch_bounds,
        overwrite=True,
    )
    with (
        rasterio.open(legacy_water) as legacy_water_source,
        rasterio.open(canonical_water_clip) as canonical_water_source,
    ):
        assert legacy_water_source.transform == canonical_water_source.transform
        np.testing.assert_array_equal(
            legacy_water_source.read(1),
            canonical_water_source.read(1),
        )
    with (
        rasterio.open(legacy_endpoint) as legacy_endpoint_source,
        rasterio.open(canonical_endpoint_clip) as canonical_endpoint_source,
    ):
        assert legacy_endpoint_source.transform == canonical_endpoint_source.transform
        np.testing.assert_array_equal(
            legacy_endpoint_source.read(1),
            canonical_endpoint_source.read(1),
        )

    raster_stack._CANONICAL_RASTER_STACK_READY_THIS_PROCESS.clear()
    original_rasterize = raster_stack.rasterize_water_to_match_dem
    original_align = raster_stack.align_canopy_height_to_endpoint_dem
    monkeypatch.setattr(
        raster_stack,
        "rasterize_water_to_match_dem",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("core cache unexpectedly rebuilt")
        ),
    )
    monkeypatch.setattr(
        raster_stack,
        "align_canopy_height_to_endpoint_dem",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("canopy cache unexpectedly rebuilt")
        ),
    )
    reused = raster_stack.ensure_canonical_raster_stack(app, include_canopy=True)
    assert reused.core_metadata_path == stack.core_metadata_path
    assert reused.canopy_metadata_path == stack.canopy_metadata_path

    monkeypatch.setattr(raster_stack, "rasterize_water_to_match_dem", original_rasterize)
    monkeypatch.setattr(raster_stack, "align_canopy_height_to_endpoint_dem", original_align)
    _write_raster(canopy_path, np.full((4, 5), 9.0, dtype="float32"), nodata=255.0)
    raster_stack._CANONICAL_RASTER_STACK_READY_THIS_PROCESS.clear()
    invalidated = raster_stack.ensure_canonical_raster_stack(app, include_canopy=True)
    assert invalidated.core_metadata_path == stack.core_metadata_path
    assert invalidated.canopy_metadata_path != stack.canopy_metadata_path
