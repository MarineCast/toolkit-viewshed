"""Inspect component support with analytic populations, independent of real inputs."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from regional_weight_diagnostics import reduce_weights


def test_joint_distance_support_conditional_factor_and_not_applicable():
    # Two observers, three water samples. Sample 0 represents three pixels.
    # Terrain clear weighted mass = 3*.8 + 3*.4 + .2 = 3.8 of 8 comparisons.
    bare = np.array([[1, 0, 0], [1, 1, -2]])
    joint = np.array([[0.5, 0, 0], [1, -1, -2]])
    decay = np.array([[0.8, 0.2, 0.1], [0.4, 0.2, 0]])
    rows = reduce_weights(bare, joint, decay, np.array([0, 0, 1]), np.array([3, 1, 1]), 2)
    assert rows[0]["weight_terrain"] == pytest.approx(3.8 / 8)
    assert rows[0]["above_ground_lower"] == pytest.approx(2.4 / 3.8)
    assert rows[0]["above_ground_upper"] == pytest.approx(2.6 / 3.8)
    assert rows[0]["weight_above_ground"] is None
    assert rows[0]["above_ground_status"] == "unknown"
    assert rows[1]["weight_terrain"] == 0
    assert rows[1]["above_ground_status"] == "not_applicable"
    assert rows[1]["above_ground_lower"] is None
    assert rows[1]["weight_above_ground"] is None


def test_clear_zero_and_distance_applied_once():
    bare = np.ones((1, 2))
    decay = np.array([[0.5, 0.1]])
    rows = reduce_weights(bare, np.array([[1, 0]]), decay, np.array([0, 0]), np.ones(2), 1)
    assert rows[0]["weight_terrain"] == pytest.approx(0.3)
    assert rows[0]["weight_above_ground"] == pytest.approx(5 / 6)
    assert rows[0]["weight_terrain"] * rows[0]["weight_above_ground"] == pytest.approx(0.25)
    blocked = reduce_weights(bare, np.zeros((1, 2)), decay, np.array([0, 0]), np.ones(2), 1)[0]
    assert blocked["weight_above_ground"] == 0
    assert blocked["above_ground_status"] == "known"


def test_float32_distance_does_not_change_clear_transmission_or_invariants():
    bare = np.ones((15, 2))
    decay = np.full((15, 2), 0.3, dtype="float32")
    result = reduce_weights(bare, bare.astype("float64"), decay, np.array([0, 0]), np.ones(2), 1)[0]
    assert result["weight_above_ground"] == 1
    assert result["above_ground_lower"] == result["above_ground_upper"] == 1


def test_invalid_support_not_silently_clamped_or_normalized():
    for bare, joint, weights in [
        (np.zeros((1, 1)), np.ones((1, 1)), np.ones(1)),
        (np.ones((1, 1)), np.array([[np.nan]]), np.ones(1)),
        (np.ones((1, 1)), np.ones((1, 1)), np.zeros(1)),
    ]:
        with pytest.raises(ValueError):
            reduce_weights(bare, joint, np.ones((1, 1)), np.zeros(1, int), weights, 1)
    with pytest.raises(ValueError, match="positive support"):
        reduce_weights(
            np.ones((1, 1)), np.ones((1, 1)), np.ones((1, 1)), np.zeros(1, int), np.ones(1), 2
        )


def test_distance_configuration_is_snapshotted_and_changes_reject_completion(tmp_path, monkeypatch):
    import json

    import polars as pl
    import regional_weight_diagnostics as diagnostic

    from viewshed_toolkit.pipeline.config.schema import load_yaml_config

    pilot = tmp_path / "pilot"
    pilot.mkdir()
    (pilot / "prepared").mkdir()
    row = dict(source_type="land", source_h3="8728d180cffffff", target_h3="8728d1818ffffff")
    pl.DataFrame([row]).write_parquet(pilot / "average_viewability.parquet")
    (pilot / "runtime.json").write_text(
        json.dumps(
            dict(
                contract="bounded_regional_classified_viewability_pilot_v1",
                output_sha256=diagnostic.digest(pilot / "average_viewability.parquet"),
            )
        )
    )
    for name in ("prepared.json", "source_cells.json", "water_area.parquet"):
        (pilot / name).write_text("{}")
    distance = tmp_path / "distance.yaml"
    original = "bbox_wgs84: {min_lon: -123, min_lat: 48, max_lon: -122, max_lat: 49}\ndistance_weight:\n  logistic_d50_km: 9\n"
    distance.write_text(original)
    out = tmp_path / "out"

    original_loader = diagnostic.load_yaml_config

    def snapshot_loader(source):
        assert Path(source) != distance
        return original_loader(source)

    monkeypatch.setattr(diagnostic, "load_yaml_config", snapshot_loader)

    def fake_jobs(*_):
        saved = json.loads((out / "config.json").read_text())
        assert saved["distance_config"] != str(distance)
        assert load_yaml_config(saved["distance_config"])["distance_weight"]["logistic_d50_km"] == 9
        pl.DataFrame([dict(row, weight_terrain=0.3)]).write_parquet(out / "partitions/one.parquet")
        distance.write_text(original.replace("9\n", "19\n"))
        return {}

    monkeypatch.setattr(diagnostic, "run_jobs", fake_jobs)
    with pytest.raises(ValueError, match="Distance configuration changed"):
        diagnostic.run(
            dict(
                pilot=str(pilot),
                output_dir=str(out),
                distance_config=str(distance),
                workers=1,
                memory_budget_gib=1,
            )
        )
    assert not (out / "manifest.json").exists()
    assert not (out / "weights.parquet").exists()
    assert (out / "distance-config.yaml").read_text() == original
