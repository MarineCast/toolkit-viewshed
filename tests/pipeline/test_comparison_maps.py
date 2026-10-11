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
            observer_latitude=48.0,
            observer_longitude=-123.0,
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
    assert rows[0]["observer_latitude"] is None
    assert rows[0]["observer_longitude"] is None
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


@pytest.mark.parametrize("joint", [False, True])
def test_scenario_to_composite_caption_transitions(tmp_path, joint):
    """Execute the packaged renderer on both composites from every FABDEM exact scenario."""
    from copy import deepcopy
    from pathlib import Path
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required to execute offline renderer transition regressions")
    data = payload()
    polygon = data["viewpoints"]["s"]["rows"][0]["polygon"]
    data.update(
        bounds=[-1, -1, 2, 2],
        land=dict(type="GeometryCollection", geometries=[]),
        terrain_reference="Retained pilot terrain",
        points={},
    )
    for product in ("s", "t", "u"):
        data["viewpoints"][product] = deepcopy(data["viewpoints"]["s"])
        for row in data["viewpoints"][product]["rows"]:
            row.update(
                source_h3=product,
                weight_terrain=0.5,
                weight_distance=0.8,
                weight_vegetation=None,
                weight_combined=None,
                combined_lower=0.2,
                combined_upper=0.4,
                distance_km=1,
            )
        data["composite_sources"][product] = dict(
            name=product,
            target_sampling="Retained centers",
            samples=[dict(sample_id=str(i), lon=0.1 * i, lat=0.1 * i) for i in range(5)],
        )
        data["source_polygons"][product] = polygon
    for product, source in (("p1", "s"), ("p2", "t")):
        data["viewpoints"][product] = deepcopy(data["viewpoints"][source])
        for row in data["viewpoints"][product]["rows"]:
            row.update(
                n_observers=1,
                target_sample_count=sum(
                    row[k]
                    for k in (
                        "canopy_clear_observations",
                        "canopy_zero_observations",
                        "canopy_unknown_observations",
                    )
                ),
            )
        data["points"][product] = dict(name=product, lon=0, lat=0)
    data["scenarios"] = {
        scenario: {p: deepcopy(data["viewpoints"][p]) for p in ("p1", "p2")}
        for scenario in ("-1", "0", "1")
    }
    if joint:
        data.update(
            primary_visibility="above_ground_obstruction",
            obstruction_reference="Joint canopy/building surface",
        )
        for item in data["viewpoints"].values():
            for row in item["rows"]:
                for component in ("obstruction", "building"):
                    for status in ("clear", "zero", "unknown"):
                        row[component + "_" + status + "_observations"] = row[
                            "canopy_" + status + "_observations"
                        ]
                row.update(
                    weight_obstruction=None,
                    weight_obstruction_combined=None,
                    obstruction_combined_lower=0.2,
                    obstruction_combined_upper=0.4,
                    obstruction_status="computed_above_ground_obstruction_model",
                    combined_status="partial_unknown_paths",
                )
    prepared = prepare_comparison_payload(data)
    for product in ("s", "t", "u"):
        assert all(
            row["observer_latitude"] is None and row["observer_longitude"] is None
            for row in prepared["viewpoints"][product]["rows"]
        )
        assert len(prepared["composite_sources"][product]["samples"]) == 5
    for product in ("p1", "p2"):
        assert all(
            row["observer_latitude"] == 48.0 and row["observer_longitude"] == -123.0
            for row in prepared["viewpoints"][product]["rows"]
        )
    export_comparison_maps(data, tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_comparison_maps.js"
    result = subprocess.run(
        [node, str(script), str(tmp_path)], capture_output=True, text=True, check=True
    )
    checks = json.loads(result.stdout)
    assert checks["results"][2]["scenario_to_composite_transitions"] == 18
    assert all(r["footer_links_preserved"] >= 3 for r in checks["results"])
    assert all(r["joint_mode"] == joint for r in checks["results"])


def test_ratio_aggregation_excludes_baseline_na_but_preserves_missing_applicable():
    data = payload()
    rows = data["viewpoints"]["s"]["rows"]
    rows[0].update(
        weight_terrain=0,
        weight_vegetation=None,
        weight_combined=0,
        combined_lower=0,
        combined_upper=0,
    )
    rows[1].update(
        target_sample_count=2,
        canopy_zero_observations=10,
        weight_terrain=0.5,
        weight_vegetation=0,
        weight_combined=0,
        combined_lower=0,
        combined_upper=0,
    )
    group = prepare_comparison_payload(data)["source_rows"][0]
    assert group["weight_vegetation_sum"] == 0
    assert group["weight_vegetation_applicable_count"] == 1
    assert group["weight_vegetation_missing_count"] == 0
    rows[1]["weight_vegetation"] = None
    group = prepare_comparison_payload(data)["source_rows"][0]
    assert group["weight_vegetation_sum"] is None
    assert group["weight_vegetation_missing_count"] == 1
    rows[1]["weight_terrain"] = 0
    group = prepare_comparison_payload(data)["source_rows"][0]
    assert group["weight_vegetation_sum"] is None
    assert group["weight_vegetation_applicable_count"] == 0


def test_joint_counts_preserve_canopy_and_building_diagnostics():
    data = payload()
    data["primary_visibility"] = "above_ground_obstruction"
    for row in data["viewpoints"]["s"]["rows"]:
        total = sum(
            row[k]
            for k in (
                "canopy_clear_observations",
                "canopy_zero_observations",
                "canopy_unknown_observations",
            )
        )
        row.update(
            obstruction_clear_observations=0,
            obstruction_zero_observations=total // 2,
            obstruction_unknown_observations=total - total // 2,
            building_clear_observations=total // 4,
            building_zero_observations=total // 4,
            building_unknown_observations=total // 2,
        )
    current = prepare_comparison_payload(data)
    row = current["viewpoints"]["s"]["rows"][0]
    assert row["ui_known_only_mean"] == 0.5
    assert row["ui_obstruction_known_only_mean"] == 0
    assert row["ui_obstruction_mean"] is None
    assert row["ui_obstruction_mean_upper"] == 0.5
    source = current["source_rows"][0]
    assert source["ui_obstruction_known_only_mean"] == 0
    assert source["ui_building_known_only_mean"] == 0.5
    assert current["results"][0]["obstruction_count_pooled"]["complete"] is None
    data["viewpoints"]["s"]["rows"][0]["building_clear_observations"] += 1
    with pytest.raises(ValueError, match="Diagnostic comparison population"):
        prepare_comparison_payload(data)
