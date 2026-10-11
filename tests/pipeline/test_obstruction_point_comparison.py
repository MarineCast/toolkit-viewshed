import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from pyproj import Transformer


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/compare_obstruction_points.py"
    spec = importlib.util.spec_from_file_location("point_comparison", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_empty_support_and_invalid_states_are_not_reported_as_zero():
    m = module()
    empty = m.counts(np.array([], dtype="int8"))
    assert empty["total"] == 0 and empty["complete_mean"] is None
    assert empty["clear_lower"] is None and empty["clear_upper"] is None
    with pytest.raises(AssertionError):
        m.counts(np.array([2]))


def test_html_embeds_the_entire_population_and_rejects_count_drift(tmp_path):
    m = module()
    x, y = Transformer.from_crs(4326, 32610, always_xy=True).transform(-123.12, 48.15)
    values = np.array([0, -1, 1], dtype="int8")
    table = pl.DataFrame(
        dict(
            target_x_m=[x + 30, x + 60, x + 90],
            target_y_m=[y] * 3,
            target_h3=["a", "a", "b"],
            **{"test:joint": values},
        )
    )
    table.write_parquet(tmp_path / "Test.parquet")
    site = dict(
        name="Test",
        lat=48.15,
        lon=-123.12,
        land={
            "type": "Polygon",
            "coordinates": [
                [[-123.12, 48.15], [-123.13, 48.15], [-123.12, 48.16], [-123.12, 48.15]]
            ],
        },
        results={"test:joint": m.counts(values)},
    )
    report = dict(sites=[site])
    m.export_comparison(report, tmp_path)
    text = (tmp_path / "comparison.html").read_text()
    embedded = json.loads(
        re.search(r'<script id="payload" type="application/json">(.*?)</script>', text, re.S)[1]
    )
    targets = embedded["sites"][0]["targets"]
    assert targets["states"]["test:joint"] == [0, -1, 1]
    assert targets["dx"] == [30, 60, 90]
    assert targets["h3"] == ["a", "a", "b"]
    site["results"]["test:joint"]["clear"] += 1
    with pytest.raises(AssertionError):
        m.export_comparison(report, tmp_path)
