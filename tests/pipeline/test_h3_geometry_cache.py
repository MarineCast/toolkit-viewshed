from __future__ import annotations

from pathlib import Path

import h3
import polars as pl
from shapely.geometry import box

from viewshed_toolkit.pipeline.config.distance import DistanceRuntime
from viewshed_toolkit.pipeline.prepare.area import geometry


def _runtime(tmp_path: Path) -> DistanceRuntime:
    return DistanceRuntime(
        config_path=tmp_path / "viewshed.yaml",
        config_dir=tmp_path,
        raw_config={"water_viewing": {"target_area_equal_area_crs": "EPSG:6933"}},
        data_dir=tmp_path,
        output_dir=tmp_path / "output",
        source_resolution=7,
        target_resolution=7,
        run_version="test",
        config_hash="test",
        lookup_path=tmp_path / "lookup.parquet",
        viewshed_max_distance_km=30.0,
        bbox_wgs84=(-123.0, 47.0, -122.0, 49.0),
        projected_crs="EPSG:32610",
    )


def test_h3_geometry_artifact_persists_reusable_projected_geometry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    cells = [
        h3.latlng_to_cell(48.0, -122.7, 7),
        h3.latlng_to_cell(48.1, -122.6, 7),
    ]
    monkeypatch.setattr(
        geometry.domains,
        "load_water_domain",
        lambda _runtime, **_kwargs: box(-123.0, 47.0, -122.0, 49.0),
    )

    path = geometry.ensure_h3_geometry_artifact(runtime, cells)
    first_mtime = path.stat().st_mtime_ns
    frame = pl.read_parquet(path)

    assert path.name == "H3_GEOMETRY_H3R7.parquet"
    assert tuple(frame.columns) == geometry.H3_GEOMETRY_SCHEMA
    assert frame.schema["geometry_wgs84"] == pl.Binary
    assert frame.schema["geometry_projected"] == pl.Binary
    assert frame.schema["water_geometry_projected"] == pl.Binary
    assert frame.get_column("water_area_m2").min() > 0.0

    projected = geometry.load_h3_geometry_lookup(path, "geometry_projected")
    assert geometry.load_h3_geometry_lookup(path, "geometry_projected") is projected
    assert set(projected) == set(cells)
    assert all(item.area > 0.0 for item in projected.values())

    # A later stage requesting a subset reuses the complete grid artifact.
    assert geometry.ensure_h3_geometry_artifact(runtime, cells[:1]) == path
    assert path.stat().st_mtime_ns == first_mtime


def test_h3_geometry_artifact_expands_without_losing_existing_cells(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    cells = [
        h3.latlng_to_cell(48.0, -122.7, 7),
        h3.latlng_to_cell(48.1, -122.6, 7),
    ]
    monkeypatch.setattr(
        geometry.domains,
        "load_water_domain",
        lambda _runtime, **_kwargs: box(-123.0, 47.0, -122.0, 49.0),
    )

    path = geometry.ensure_h3_geometry_artifact(runtime, cells[:1])
    geometry.ensure_h3_geometry_artifact(runtime, cells[1:])

    assert set(pl.read_parquet(path).get_column("h3_cell")) == set(cells)


def test_local_water_clips_preserve_native_geometry_holes_and_order():
    import geopandas as gpd
    import numpy as np
    import shapely
    from shapely.geometry import Polygon

    cells = gpd.GeoSeries(
        [box(x, y, x + 0.08, y + 0.08) for x in np.arange(0, 1, 0.07) for y in (0, 0.1, 0.3)],
        crs=4326,
        index=np.arange(45) * 3,
    )
    water = Polygon(
        [(0, -0.1), (1.2, -0.1), (0.9, 0.5), (0.4, 0.15), (0, 0.5)],
        holes=[[(0.1, 0.05), (0.1, 0.25), (0.25, 0.25), (0.25, 0.05)]],
    )
    expected = cells.intersection(water)
    actual = geometry._intersect_water_by_local_tiles(cells, water)
    assert actual.index.equals(cells.index) and actual.crs == cells.crs
    assert shapely.equals_exact(shapely.normalize(actual), shapely.normalize(expected), 1e-12).all()
    np.testing.assert_allclose(actual.to_crs(6933).area, expected.to_crs(6933).area, atol=1e-5)
    reversed_result = geometry._intersect_water_by_local_tiles(cells.iloc[::-1], water)
    assert actual.to_wkb().tolist() == reversed_result.iloc[::-1].to_wkb().tolist()


def test_invalid_fast_water_clip_uses_exact_overlay(monkeypatch):
    import geopandas as gpd
    from shapely.geometry import Polygon

    cells = gpd.GeoSeries([box(0, 0, 1, 1)], crs=4326)
    water = box(0.5, -0.5, 1.5, 1.5)
    monkeypatch.setattr(
        geometry.shapely,
        "clip_by_rect",
        lambda *args: Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]),
    )
    actual = geometry._intersect_water_by_local_tiles(cells, water)
    assert actual.iloc[0].equals(cells.intersection(water).iloc[0])


def test_water_only_domain_skips_land_and_matches_complete_factory(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.prepare.area import domains

    runtime = _runtime(tmp_path)
    water = box(-122.9, 47.1, -122.5, 48.8)
    land = box(-122.4, 47.1, -122.1, 48.8)
    monkeypatch.setattr(domains, "_natural_earth_land_path", lambda **kw: tmp_path / "land.shp")

    def load(path, **kwargs):
        return (water if kwargs["label"] == "Water" else land), str(path)

    monkeypatch.setattr(domains, "_load_optional_clipped_union", load)
    expected = domains.load_land_water_domains(runtime).water_domain

    def water_only(path, **kwargs):
        assert kwargs["label"] == "Water"
        return water, str(path)

    monkeypatch.setattr(domains, "_load_optional_clipped_union", water_only)
    assert domains.load_water_domain(runtime).equals(expected)


def test_water_only_domain_preserves_land_fallback(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from viewshed_toolkit.pipeline.prepare.area import domains

    runtime = _runtime(tmp_path)
    expected = box(-123, 47, -122, 49)
    monkeypatch.setattr(domains, "_load_optional_clipped_union", lambda *a, **kw: (None, "missing"))
    monkeypatch.setattr(
        domains, "load_land_water_domains", lambda *a, **kw: SimpleNamespace(water_domain=expected)
    )
    assert domains.load_water_domain(runtime) is expected
