"""Small regional experiment checks; never acquire sources or touch real outputs."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "regional_pilot", SCRIPTS / "regional_viewability_pilot.py"
)
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_source_window_preserves_parent_lattice_and_arrays(monkeypatch):
    monkeypatch.setattr(pilot, "RADIUS", 60)
    grid = Affine(10, 0, 0, 0, -10, 600)
    ground = np.arange(3600, dtype="float32").reshape(60, 60)
    previous = ground.copy()
    result = pilot.source_window({"ground": ground}, grid, box(210, 210, 240, 240))
    col, row = (~grid) * (result["transform"].c, result["transform"].f)
    h, w = result["ground"].shape
    np.testing.assert_array_equal(
        result["ground"], ground[int(row) : int(row) + h, int(col) : int(col) + w]
    )
    assert col % 3 == 0 and row % 3 == 0
    result["ground"][:] = -1
    np.testing.assert_array_equal(ground, previous)
    with pytest.raises(ValueError, match="exceeds"):
        pilot.source_window({"ground": ground}, grid, box(1, 1, 10, 10))


def pair():
    return dict(
        source_type="land",
        source_h3="8728d180cffffff",
        target_h3="8728d1a12ffffff",
        lower=0.2,
        upper=0.5,
        average_viewability=None,
        input_unknown_fraction=0.3,
        out_of_range_fraction=0.0,
        target_water_pixel_count=2000,
    )


def test_source_support_is_land_inside_the_requested_box():
    bbox = (-123.12, 48.075, -123.08, 48.10)
    project = Transformer.from_crs(4326, 32610, always_xy=True).transform
    domain = transform(project, box(*bbox))
    left, bottom, right, top = domain.bounds
    land = domain.intersection(box(left, bottom, (left + right) / 2, top))
    water = domain.difference(land)
    sources, _ = pilot.land_sources(bbox, land, water)
    assert [s["source_h3"] for s in sources] == sorted({s["source_h3"] for s in sources})
    for source in sources:
        active = shape(source["source_land"])
        assert active.difference(land).area < 1e-6
        assert active.difference(domain).area < 1e-6
        assert source["land_fraction"] >= 0.01
    with pytest.raises(ValueError, match="No eligible"):
        pilot.land_sources(bbox, box(0, 0, 10, 10), domain)


def test_combination_rejects_duplicates_missing_sources_and_hidden_unknown():
    row = pair()
    sources = [row["source_h3"]]
    pilot.validate_pairs(pl.DataFrame([row]), sources)
    for rows, expected, message in [
        ([row, row], sources, "identity"),
        ([row], [*sources, "8728d1a12ffffff"], "source"),
        ([dict(row, average_viewability=0.2)], sources, "bounds"),
        ([dict(row, upper=0.4)], sources, "bounds"),
        ([dict(row, source_h3=None)], sources, "identity"),
        ([dict(row, lower=float("nan"))], sources, "bounds"),
    ]:
        with pytest.raises(ValueError, match=message):
            pilot.validate_pairs(pl.DataFrame(rows), expected)


def test_mixed_shoreline_uses_separate_unflattened_observer_ground():
    from shapely.geometry import Point, mapping

    land = box(500000, 5370000, 500004, 5370050)
    ground = np.full((6, 6), 2.0, dtype="float32")
    data = dict(
        transform=Affine(10, 0, 500000, 0, -10, 5370060),
        ground=ground.copy(),
        observer_ground=ground.copy(),
        building_present=np.zeros((6, 6), bool),
    )
    data["ground"][:, 0] = 0
    source = dict(source_h3="8728d180cffffff", source_land=mapping(land))
    observers, _ = pilot.observer_design(source, data)
    assert len(observers) == 15
    assert all(land.covers(Point(x, y)) for x, y, _ in observers)
    np.testing.assert_array_equal(observers[:, 2], np.full(15, 2.0))
    assert not data["ground"][:, 0].any()
    data["observer_ground"][:, 0] = np.nan
    with pytest.raises(ValueError, match="unflattened"):
        pilot.observer_design(source, data)
