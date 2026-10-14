"""Acceptance-race, disk reservation and partial-finalization regressions."""

import json
from types import SimpleNamespace

import h3
import polars as pl
import pytest

from viewshed_toolkit.pipeline.api import transmission_tile_finalize_worker as finalize_worker
from viewshed_toolkit.pipeline.api import transmission_tile_workflow as workflow
from viewshed_toolkit.pipeline.contracts.transmission import (
    TRANSMISSION_PAIR_SCHEMA,
    digest,
    payload_hash,
)
from viewshed_toolkit.pipeline.contracts.transmission_tile_budget import reserve_tile_workflow_bytes


def test_checked_store_compares_inventory_membership_not_parent_traversal(monkeypatch):
    config = SimpleNamespace(
        store_dir="fixture",
        provisioned_metadata=SimpleNamespace(sha256="m"),
        source_inventory=SimpleNamespace(sha256="s"),
        reporting_water=SimpleNamespace(sha256="w"),
        global_transform=[10.0, 0.0, 0.0, 0.0, -10.0, 3000.0],
        global_shape=[3072, 3072],
        source_policy="full",
        max_distance_m=120.0,
        census_sources=1,
    )
    identity = dict(
        code={"c": "stable"},
        runtime={"r": "stable"},
        metadata_sha256="m",
        source_inventory_sha256="s",
        reporting_water_sha256="w",
        grid=config.global_transform,
        shape=config.global_shape,
        source_policy="full",
        planning_radius_m=120.0,
    )
    parent_order = [[0, 0], [384, 0], [0, 1536], [384, 1536]]
    plan = dict(
        identity=identity,
        required_keys=parent_order,
        sources=[{"source_h3": "fixture"}],
        census_sources=["fixture"],
    )

    class Store:
        def __init__(self):
            self.manifest = dict(identity=identity, required_keys=sorted(parent_order))

        def read_sidecar(self, name):
            return json.dumps(plan).encode()

    monkeypatch.setattr(workflow, "verified_inputs", lambda cfg: ({}, {}))
    monkeypatch.setattr(workflow, "PreparedTileStore", lambda root: Store())
    monkeypatch.setattr(workflow, "implementation_identity", lambda: {"c": "stable"})
    monkeypatch.setattr(workflow, "store_runtime_identity", lambda: {"r": "stable"})
    assert workflow.checked_store(config)[1] == plan


def fixture_completed_source(tmp_path):
    root = tmp_path / "score"
    folder = root / "sources" / "source"
    folder.mkdir(parents=True)
    source = h3.latlng_to_cell(48.0, -123.0, 7)
    folder.rename(root / "sources" / source)
    folder = root / "sources" / source
    row = dict(
        source_type="land",
        source_h3=source,
        target_h3=h3.latlng_to_cell(48.01, -123.0, 7),
        method="worldcover_chm_intersection_transmission_v2",
        support_status="known",
        target_support="partial_h3_in_observer_reach",
        lower=0.4,
        upper=0.4,
        average_viewability=0.4,
        input_unknown_fraction=0.0,
        out_of_range_fraction=0.0,
        target_water_pixel_count=10,
        sampled_target_points=10,
        observer_count=1,
        max_distance_m=120.0,
        sampling="stride2",
        census_checked=False,
        weight_terrain=0.5,
        weight_distance=0.9,
        distance_km=1.0,
        above_ground_lower=0.8,
        above_ground_upper=0.8,
        weight_above_ground=0.8,
        above_ground_status="known",
    )
    table = (
        pl.DataFrame([row])
        .select(list(TRANSMISSION_PAIR_SCHEMA))
        .cast(pl.Schema(TRANSMISSION_PAIR_SCHEMA))
    )
    table.write_parquet(folder / "pairs.parquet")
    table.write_parquet(root / "average_viewability.parquet")
    info = dict(
        source_h3=source,
        pair_count=1,
        source_state="complete",
        sampled_result_used=True,
        observers=[[0.0, 0.0, 2.0]],
    )
    (folder / "source.json").write_text(json.dumps(info))
    identity = {"fixture": "stable"}
    ih = payload_hash(identity)
    (root / "generation.json").write_text(json.dumps(dict(identity=identity, identity_hash=ih)))
    (root / "scoring-execution.json").write_text("{}")
    (folder / "receipt.json").write_text(
        json.dumps(
            dict(
                identity_hash=ih,
                source_h3=source,
                pairs_sha256=digest(folder / "pairs.parquet"),
                source_sha256=digest(folder / "source.json"),
            )
        )
    )
    return root, source


@pytest.mark.parametrize("changed", [False, True])
def test_closing_store_validation_and_atomic_final_resume(tmp_path, monkeypatch, changed):
    root, source = fixture_completed_source(tmp_path)
    config = SimpleNamespace(
        store_dir=str(tmp_path / "store"), score_dir=str(root), max_store_bytes=10 * 1024**2
    )

    class Store:
        def validate(self):
            if changed:
                raise ValueError("Tile differs from completed manifest")

    monkeypatch.setattr(finalize_worker, "load_transmission_tile_config", lambda path: config)
    monkeypatch.setattr(
        finalize_worker,
        "checked_store",
        lambda cfg: (Store(), {"sources": [{"source_h3": source}]}, {}),
    )
    monkeypatch.setattr(
        finalize_worker, "scoring_identity", lambda cfg, store: {"fixture": "stable"}
    )
    if changed:
        with pytest.raises(ValueError, match="Tile differs"):
            finalize_worker.finalize("fixture")
        assert not (root / "complete.json").exists()
    else:
        before = digest(root / "average_viewability.parquet")
        finalize_worker.finalize("fixture")
        assert (root / "complete.json").exists()
        assert digest(root / "average_viewability.parquet") == before


def test_combined_store_and_score_reservation_rejects_before_write(tmp_path):
    store = tmp_path / "store"
    score = tmp_path / "score"
    store.mkdir()
    score.mkdir()
    (store / "tile").write_bytes(b"x" * 40)
    (score / "pair").write_bytes(b"x" * 30)
    cfg = SimpleNamespace(store_dir=str(store), score_dir=str(score), max_store_bytes=100)
    assert reserve_tile_workflow_bytes(cfg, 30) == 70
    with pytest.raises(ValueError, match="Combined"):
        reserve_tile_workflow_bytes(cfg, 31)


def test_consistent_prepared_completion_replacement_rejected_before_acceptance(
    tmp_path, monkeypatch
):
    root, source = fixture_completed_source(tmp_path)
    cfg = SimpleNamespace(
        store_dir=str(tmp_path / "store"), score_dir=str(root), max_store_bytes=10 * 1024**2
    )

    class Store:
        def validate(self):
            pass

    monkeypatch.setattr(finalize_worker, "load_transmission_tile_config", lambda path: cfg)
    monkeypatch.setattr(
        finalize_worker,
        "checked_store",
        lambda config: (Store(), {"sources": [{"source_h3": source}]}, {}),
    )
    monkeypatch.setattr(
        finalize_worker,
        "scoring_identity",
        lambda config, store: {"fixture": "consistent_but_replaced_completion"},
    )
    with pytest.raises(ValueError, match="completion"):
        finalize_worker.finalize("fixture")
    assert not (root / "complete.json").exists()


@pytest.mark.parametrize(
    "damage", [None, "tile", "source", "final", "generation", "inventory", "deadline"]
)
def test_draft_is_rechecked_before_parent_completion(tmp_path, monkeypatch, damage):
    root, source = fixture_completed_source(tmp_path)
    config = SimpleNamespace(
        store_dir=str(tmp_path / "store"), score_dir=str(root), max_store_bytes=10 * 1024**2
    )

    class Store:
        def validate(self):
            if damage == "tile":
                raise ValueError("Tile changed")

    checked = (Store(), {"sources": [{"source_h3": source}]}, {})
    identity = {"fixture": "stable"}
    monkeypatch.setattr(finalize_worker, "load_transmission_tile_config", lambda path: config)
    monkeypatch.setattr(finalize_worker, "checked_store", lambda cfg: checked)
    monkeypatch.setattr(finalize_worker, "scoring_identity", lambda cfg, store: identity)
    # The child closes the store and builds validated final bytes, but no marker.
    if damage == "tile":
        with pytest.raises(ValueError, match="Tile changed"):
            finalize_worker.finalize("fixture", publish=False, checked=checked)
        assert not (root / "complete.json").exists()
        return
    proposal = finalize_worker.finalize("fixture", publish=False, checked=checked)
    assert not (root / "complete.json").exists()
    monkeypatch.setattr(workflow, "checked_store", lambda cfg: checked)
    monkeypatch.setattr(workflow, "scoring_identity", lambda cfg, store: identity)
    if damage == "source":
        (root / "sources" / source / "source.json").write_text("{}")
    if damage == "final":
        with (root / "average_viewability.parquet").open("ab") as handle:
            handle.write(b"changed")
    if damage == "generation":
        (root / "generation.json").write_text(
            json.dumps(dict(identity={"changed": True}, identity_hash="wrong"))
        )
    if damage == "inventory":
        proposal["sources"] = {}
    if damage is not None:
        with pytest.raises((ValueError, RuntimeError)):
            workflow._publish_score_proposal(
                config,
                proposal,
                {"wall_seconds": 1.0},
                payload_hash(identity),
                deadline=0.0 if damage == "deadline" else None,
            )
        assert not (root / "complete.json").exists()
    else:
        result = workflow._publish_score_proposal(
            config, proposal, {"wall_seconds": 1.0}, payload_hash(identity)
        )
        assert result["pair_count"] == 1
        manifest = json.loads((root / "complete.json").read_text())
        assert manifest["execution"]["finalization"] == "same_supervised_child"
