"""Boundary targets and missing cache coverage must survive the additional maps."""

import importlib.util
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from affine import Affine
from shapely.geometry import box


@pytest.fixture
def maps(monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "average_maps", scripts / "map_average_viewability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_population_keeps_water_targets_past_cache_and_respects_range(maps):
    transform = Affine(10, 0, 0, 0, -10, 60)
    observers = np.array([[15.0, 15.0, 0.0]])
    water = box(0, 0, 120, 60).union(box(6000, 0, 6100, 60))
    targets, rows, cols, inside = maps.target_population(water, transform, (6, 6), observers)
    assert len(targets) == 8
    assert inside.sum() == 4
    assert np.all(np.hypot(*(targets - observers[0, :2]).T) <= 5000)
    assert set(rows) == {0, 1}
    assert set(cols) == {0, 1, 2, 3}


def test_full_denominator_keeps_uncovered_and_out_of_range_comparisons(maps):
    # Two observers, one modeled target representing two pixels, one uncovered.
    result = maps.merge_coverage(
        {0: dict(lower=0.5, upper=0.5, unknown=0.0, out_of_range=0.5)},
        np.array([0]),
        np.array([[-1], [-2]]),
        np.array([0]),
        (np.array([2.0]), np.array([1.0])),
        np.array(["cell"]),
    )
    assert result["lower"][0] == pytest.approx(1 / 3)
    assert result["upper"][0] == pytest.approx(1 / 2)
    assert result["unknown"][0] == result["uncovered"][0] == pytest.approx(1 / 6)
    assert result["out_of_range"][0] == pytest.approx(1 / 2)


def test_wholly_uncovered_target_stays_in_pair_universe(maps):
    result = maps.merge_coverage(
        {},
        np.array([], int),
        np.array([[-1], [-1]]),
        np.array([0]),
        (np.array([]), np.array([3.0])),
        np.array(["cell"]),
    )
    assert result["lower"][0] == 0
    assert result["upper"][0] == result["unknown"][0] == 1
    assert result["uncovered"][0] == 1


def test_missing_target_support_fails_instead_of_becoming_zero(maps):
    with pytest.raises(ValueError, match="positive target support"):
        maps.merge_coverage(
            {},
            np.array([], int),
            np.empty((2, 0)),
            np.array([], int),
            (np.array([]), np.array([])),
            np.array(["cell"]),
        )


def test_actual_chart_sql_preserves_four_parts_and_area_weighting(maps):
    rows = [
        dict(
            site="Example",
            lower=0.4,
            upper=0.6,
            input_unknown_fraction=0.2,
            uncovered_fraction=0.1,
            target_water_pixel_count=3,
        ),
        dict(
            site="Example",
            lower=0.0,
            upper=1.0,
            input_unknown_fraction=1.0,
            uncovered_fraction=1.0,
            target_water_pixel_count=1,
        ),
    ]
    result = pl.SQLContext(target_scores=pl.DataFrame(rows)).execute(maps.PARTS_SQL).collect()
    values = dict(result.select("component", "fraction").iter_rows())
    assert values["Modeled visible support"] == pytest.approx(0.3)
    assert values["Reduced or blocked support"] == pytest.approx(0.3)
    assert values["Missing vegetation or roof inputs"] == pytest.approx(0.075)
    assert values["Outside retained obstacle coverage"] == pytest.approx(0.325)
    assert sum(values.values()) == pytest.approx(1)


def test_unknown_config_fields_rejected_before_io(maps):
    with pytest.raises(ValueError, match="Exactly"):
        maps.run(dict(trial_report="missing", output_dir="unused", typo=True))
