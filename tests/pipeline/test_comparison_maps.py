import json
import re

import pytest

from viewshed_toolkit import export_comparison_maps
from viewshed_toolkit.pipeline.visualization.comparison_maps import prepare_comparison_payload
from viewshed_toolkit.pipeline.weights.comparison_mean import comparison_mean


def payload():
    def row(target, clear, blocked, unknown):
        return dict(
            source_h3="s",
            target_h3=target,
            cell=target,
            polygon=[[0, 0], [1, 0], [0, 1], [0, 0]],
            n_observers=5,
            target_sample_count=(clear + blocked + unknown) // 5,
            canopy_clear_observations=clear,
            canopy_zero_observations=blocked,
            canopy_unknown_observations=unknown,
            bare_clear_observations=clear + unknown,
            observer_scope="single_point",
            viewpoint_id="exact",
        )

    return dict(
        viewpoints={"s": dict(rows=[row("a", 40, 40, 20), row("b", 0, 0, 0)])},
        composite_sources={"s": {}},
        source_polygons={"s": []},
    )


def test_partial_empty_and_known_only():
    assert comparison_mean(40, 40, 20) == dict(
        total=100, known=80, lower=0.4, upper=0.6, complete=None, known_only=0.5
    )
    assert comparison_mean(0, 0, 0)["complete"] is None
    assert comparison_mean(0, 0, 10)["known_only"] is None
    assert comparison_mean(0, 10, 0)["complete"] == 0


@pytest.mark.parametrize("counts", [(-1, 0, 0), (1.5, 0, 0), (True, 0, 0)])
def test_invalid_counts(counts):
    with pytest.raises(ValueError):
        comparison_mean(*counts)


def test_rebuild_summaries_scope_and_preserve_input():
    original = payload()
    data = prepare_comparison_payload(original)
    rows = data["viewpoints"]["s"]["rows"]
    assert rows[0]["observer_scope"] == "source_cell_samples"
    assert rows[0]["viewpoint_id"] is None
    assert original["viewpoints"]["s"]["rows"][0]["observer_scope"] == "single_point"
    summary = data["results"][0]
    assert summary["ui_canopy_mean"]["valid"] == 0
    assert summary["ui_canopy_mean"]["full_sum"] is None
    assert summary["empty_target_populations"] == 1
    assert summary["ui_canopy_mean_interval_sum"] == [0.4, 0.6]
    assert data["source_rows"][0]["ui_canopy_mean_sum"] is None


def test_population_and_duplicate_validation():
    for change in ("count", "duplicate", "null"):
        data = payload()
        rows = data["viewpoints"]["s"]["rows"]
        if change == "count":
            rows[0]["target_sample_count"] = 99
        elif change == "duplicate":
            rows.append(rows[0].copy())
        else:
            rows[0]["target_h3"] = None
        with pytest.raises(ValueError):
            prepare_comparison_payload(data)


def test_pooled_vs_equal_pair_means_and_determinism():
    data = payload()
    rows = data["viewpoints"]["s"]["rows"]
    rows[0].update(
        canopy_clear_observations=100,
        canopy_zero_observations=0,
        canopy_unknown_observations=0,
        bare_clear_observations=100,
    )
    rows[1].update(target_sample_count=2, canopy_zero_observations=10)
    aggregate = prepare_comparison_payload(data)["source_rows"][0]
    assert aggregate["ui_canopy_mean"] == 100 / 110
    assert aggregate["ui_canopy_mean_mean_valid"] == 0.5
    assert aggregate["ui_canopy_mean_sum"] == 1
    rows.reverse()
    assert prepare_comparison_payload(data)["source_rows"][0] == aggregate


def test_export_roles_and_resources(tmp_path):
    paths = export_comparison_maps(payload(), tmp_path)
    assert [
        json.loads(
            re.search(
                r'<script id="payload" type="application/json">(.*?)</script>', p.read_text(), re.S
            )[1]
        )["kind"]
        for p in paths
    ] == ["target", "source", "point"]
    assert len({p.read_bytes() for p in paths}) == 3
    assert "source_cell_samples" in (tmp_path / "SOURCE_CELL_COMPARISON_MEANS.csv").read_text()


def test_all_empty_aggregate_has_no_mean():
    data = payload()
    data["viewpoints"]["s"]["rows"] = data["viewpoints"]["s"]["rows"][1:]
    result = prepare_comparison_payload(data)
    assert result["source_rows"][0]["ui_canopy_mean"] is None
    assert result["source_rows"][0]["ui_canopy_mean_sum"] is None
    assert result["results"][0]["ui_canopy_mean_interval_sum"] == [None, None]
