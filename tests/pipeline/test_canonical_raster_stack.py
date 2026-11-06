from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.features import rasterize
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, box

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


def test_multipart_water_burn_matches_single_feature_with_holes_and_edge_contacts(
    tmp_path: Path, monkeypatch
) -> None:
    dem = _write_raster(tmp_path / "dem.tif", np.ones((8, 10)), nodata=-9999.0)
    water = MultiPolygon(
        [
            Polygon(
                box(500_000, 5_399_880, 500_090, 5_400_000).exterior,
                [box(500_030, 5_399_910, 500_060, 5_399_940).exterior],
            ),
            box(500_150, 5_399_900, 500_240, 5_399_990),
        ]
    )
    with rasterio.open(dem) as source:
        expected = rasterize(
            [(water, 1)],
            out_shape=source.shape,
            transform=source.transform,
            all_touched=True,
            fill=0,
            dtype="uint8",
        )
    original = terrain.rasterize

    def bounded_burn(shapes, **kwargs):
        assert len(shapes) == 2
        assert all(part.geom_type == "Polygon" for part, _ in shapes)
        return original(shapes, **kwargs)

    monkeypatch.setattr(terrain, "rasterize", bounded_burn)
    output = terrain.rasterize_water_to_match_dem(
        gpd.GeoDataFrame(geometry=[water], crs="EPSG:32610"), dem, tmp_path / "water.tif"
    )
    with rasterio.open(output) as actual:
        np.testing.assert_array_equal(actual.read(1), expected)


@pytest.mark.parametrize("tile_size", [1, 3, 8, 2048])
def test_water_mask_is_independent_of_work_tiles(
    tmp_path: Path, monkeypatch, tile_size: int
) -> None:
    dem = _write_raster(tmp_path / "dem.tif", np.ones((8, 10)), nodata=-9999.0)
    water = MultiPolygon(
        [
            Polygon(
                box(500_000, 5_399_820, 500_120, 5_400_000).exterior,
                [box(500_030, 5_399_850, 500_090, 5_399_970).exterior],
            ),
            Polygon([(500_150, 5_399_760), (500_300, 5_399_970), (500_240, 5_400_000)]),
        ]
    )
    with rasterio.open(dem) as source:
        expected = rasterize(
            [(water, 1)],
            out_shape=source.shape,
            transform=source.transform,
            all_touched=True,
            fill=0,
            dtype="uint8",
        )
    monkeypatch.setattr(terrain, "_WATER_RASTERIZATION_TILE_SIZE", tile_size)
    output = terrain.rasterize_water_to_match_dem(
        gpd.GeoDataFrame(geometry=[water], crs="EPSG:32610"), dem, tmp_path / "water.tif"
    )
    with rasterio.open(output) as actual:
        np.testing.assert_array_equal(actual.read(1), expected)


def test_large_raster_domain_keeps_interior_pixels_after_geographic_projection() -> None:
    bounds = (39570.0, 5032950.0, 624810.0, 5749980.0)
    transform = Transformer.from_crs(32610, 4326, always_xy=True)
    old = gpd.GeoSeries([box(*bounds)], crs=32610).to_crs(4326).iloc[0]
    point = Point(*transform.transform(146955.0, 5745585.0))
    assert not old.covers(point)

    domain = raster_stack._raster_domain_wgs84(bounds, "EPSG:32610")
    assert domain.geometry.iloc[0].covers(point)
    for fraction in (0.1, 0.5, 0.9):
        x = bounds[0] + fraction * (bounds[2] - bounds[0])
        for y in (bounds[1] + 15, bounds[3] - 15):
            assert domain.geometry.iloc[0].covers(Point(*transform.transform(x, y)))
    np.testing.assert_allclose(domain.to_crs(32610).total_bounds, bounds, atol=1e-6, rtol=0)


def test_large_raster_domain_does_not_include_outside_pixels() -> None:
    bounds = (39570.0, 5032950.0, 624810.0, 5749980.0)
    transform = Transformer.from_crs(32610, 4326, always_xy=True)
    domain = raster_stack._raster_domain_wgs84(bounds, "EPSG:32610").geometry.iloc[0]
    for x, y in ((300000, bounds[3] + 30), (bounds[0] - 30, 5400000)):
        assert not domain.covers(Point(*transform.transform(x, y)))


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
    marine_path = terrain.rasterize_water_to_match_dem(
        water_projected, dem_path, tmp_path / "marine-mask.tif"
    )
    app = replace(app, paths=replace(app.paths, marine_water_mask_path=marine_path))
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

    # Rebuild changed heights without repeating identical water geometry work.
    _write_raster(dem_path, dem_values + 3, nodata=-9999.0)
    original_load_water = raster_stack.load_and_clip_water
    monkeypatch.setattr(
        raster_stack,
        "load_and_clip_water",
        lambda *_a: pytest.fail("height-only change repeated water geometry work"),
    )
    updated = raster_stack.ensure_canonical_raster_stack(app, include_canopy=False)
    assert updated.core_metadata_path != stack.core_metadata_path
    with rasterio.open(updated.water_mask_path) as source:
        np.testing.assert_array_equal(source.read(1), water.astype("uint8"))
    with rasterio.open(updated.endpoint_dem_path) as source:
        np.testing.assert_array_equal(source.read(1)[~water], (dem_values + 3)[~water])
        np.testing.assert_array_equal(source.read(1)[water], np.zeros(4))
    contract = json.loads(updated.core_metadata_path.read_text())["contract"]
    for field in ("water_sources", "domain_aoi_algorithm", "water_mask_algorithm_version"):
        changed = dict(contract)
        changed[field] = "changed"
        assert not raster_stack._reuse_water_mask_for_unchanged_grid(
            tmp_path / "canonical_domain_rasters/new/canonical_water_mask.tif", dem_path, changed
        )
    changed_grid = json.loads(json.dumps(contract))
    changed_grid["projected_dem"]["grid"]["width"] += 1
    assert not raster_stack._reuse_water_mask_for_unchanged_grid(
        tmp_path / "canonical_domain_rasters/new/canonical_water_mask.tif", dem_path, changed_grid
    )
    # Corrupt cached masks, including aliases of the same bytes, fail closed.
    with rasterio.open(updated.water_mask_path, "r+") as source:
        source.write(np.zeros((4, 5), dtype="uint8"), 1)
    _write_raster(dem_path, dem_values + 4, nodata=-9999.0)
    monkeypatch.setattr(raster_stack, "load_and_clip_water", original_load_water)
    rebuilt = raster_stack.ensure_canonical_raster_stack(app, include_canopy=False)
    with rasterio.open(rebuilt.water_mask_path) as source:
        np.testing.assert_array_equal(source.read(1), water.astype("uint8"))

    # No explicit marine evidence means preserve all water DEM heights.
    app = replace(app, paths=replace(app.paths, marine_water_mask_path=None))
    preserved = raster_stack.ensure_canonical_raster_stack(app, include_canopy=False)
    with rasterio.open(preserved.endpoint_dem_path) as source:
        np.testing.assert_array_equal(source.read(1), dem_values + 4)
