from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
        "load_land_water_domains",
        lambda _runtime, **_kwargs: SimpleNamespace(water_domain=box(-123.0, 47.0, -122.0, 49.0)),
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
        "load_land_water_domains",
        lambda _runtime, **_kwargs: SimpleNamespace(water_domain=box(-123.0, 47.0, -122.0, 49.0)),
    )

    path = geometry.ensure_h3_geometry_artifact(runtime, cells[:1])
    geometry.ensure_h3_geometry_artifact(runtime, cells[1:])

    assert set(pl.read_parquet(path).get_column("h3_cell")) == set(cells)
