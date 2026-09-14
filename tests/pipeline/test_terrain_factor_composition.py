from __future__ import annotations

from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from viewshed_toolkit.pipeline.contracts.artifacts import (
    FINAL_SCHEMAS,
    final_artifact_paths_from_raw,
)
from viewshed_toolkit.pipeline.weights.canopy_visibility import (
    CANOPY_VISIBILITY_CONTRACT_VERSION,
    compose_dual_surface_artifacts,
    scan_terrain_partitions,
)
from viewshed_toolkit.pipeline.weights.terrain.gdal import (
    _terrain_weight_partition_for_storage,
)


def test_selected_terrain_sources_are_pruned_before_parquet_scan(tmp_path: Path) -> None:
    partition_dir = tmp_path / "partitions" / "source=land"
    partition_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "source_h3": ["s1"],
            "target_h3": ["t1"],
            "weight_terrain": [0.75],
        }
    ).write_parquet(partition_dir / "source_h3_cell=s1.parquet")
    # This deliberately invalid Parquet file proves the unselected source is
    # never opened or included in a full-directory scan.
    (partition_dir / "source_h3_cell=s2.parquet").write_text(
        "not parquet",
        encoding="utf-8",
    )

    selected = scan_terrain_partitions(
        tmp_path / "partitions",
        selected_sources={"s1"},
    ).collect()

    assert selected.select("source_h3", "target_h3").to_dicts() == [
        {"source_h3": "s1", "target_h3": "t1"}
    ]


def test_missing_selected_terrain_partition_fails_before_composition(
    tmp_path: Path,
) -> None:
    partition_dir = tmp_path / "partitions" / "source=land"
    partition_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "source_h3": ["s1"],
            "target_h3": ["t1"],
            "weight_terrain": [0.75],
        }
    ).write_parquet(partition_dir / "source_h3_cell=s1.parquet")

    with pytest.raises(FileNotFoundError, match="Missing 1 requested terrain partition"):
        scan_terrain_partitions(
            tmp_path / "partitions",
            selected_sources={"s1", "s2"},
        )


def test_scalable_dual_surface_artifacts_preserve_full_lookup(tmp_path: Path) -> None:
    raw = {
        "run": {"version": "test_v1"},
        "h3": {"source_resolution": 8, "target_resolution": 8},
        "paths": {"output_dir": str(tmp_path / "viewshed")},
    }
    paths = final_artifact_paths_from_raw(raw, tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["s1", "s1", "water-source"],
            "target_h3": ["t1", "t2", "t1"],
            "distance_km": [1.0, 2.0, 1.0],
            "source_type": ["land", "land", "water"],
        }
    ).write_parquet(paths.source_target_lookup)
    bare_path = tmp_path / "bare.parquet"
    canopy_path = tmp_path / "canopy.parquet"
    pl.DataFrame(
        {
            "source_h3": ["s1"],
            "target_h3": ["t1"],
            "weight_terrain": [0.8],
        }
    ).write_parquet(bare_path)
    pl.DataFrame(
        {
            "source_h3": ["s1"],
            "target_h3": ["t1"],
            "weight_terrain": [0.4],
        }
    ).write_parquet(canopy_path)

    result = compose_dual_surface_artifacts(
        lookup_path=paths.source_target_lookup,
        bare_earth_partition_paths=[bare_path],
        canopy_partition_paths=[canopy_path],
        paths=paths,
        raw_config=raw,
        config_dir=tmp_path,
        bare_config_hash="bare-hash",
        canopy_config_hash="canopy-hash",
        canopy_scenario_id="canopy-test-scenario",
        canopy_scenario={"canopy_resampling": "max"},
    )
    factors = pl.read_parquet(result.dual_surface_factors).sort("target_h3")

    assert factors.columns == list(FINAL_SCHEMAS["dual_surface_factors"])
    assert factors.height == 2
    assert factors["weight_terrain"].to_list() == pytest.approx([0.8, 0.0])
    assert factors["weight_canopy_los"].to_list() == pytest.approx([0.4, 0.0])
    assert factors["weight_vegetation"].to_list() == pytest.approx([0.5, 1.0])
    assert set(factors["source_type"].to_list()) == {"land"}
    assert set(factors["vegetation_status"].to_list()) == {"computed"}
    assert set(factors["vegetation_provenance"].to_list()) == {CANOPY_VISIBILITY_CONTRACT_VERSION}
    assert pl.read_parquet(result.terrain_weights).columns == list(FINAL_SCHEMAS["terrain_weights"])
    assert pl.read_parquet(result.canopy_los_weights).columns == list(
        FINAL_SCHEMAS["canopy_los_weights"]
    )
    assert pl.read_parquet(result.vegetation_weights).columns == list(
        FINAL_SCHEMAS["vegetation_weights"]
    )
    assert result.diagnostics["canopy_scenario_id"] == "canopy-test-scenario"


@pytest.mark.parametrize("duplicate_input", ["lookup", "bare", "canopy"])
def test_dual_surface_composition_validates_uniqueness_in_composition_pass(
    tmp_path: Path,
    duplicate_input: str,
) -> None:
    raw = {
        "run": {"version": "test_v1"},
        "h3": {"source_resolution": 8, "target_resolution": 8},
        "paths": {"output_dir": str(tmp_path / "viewshed")},
    }
    paths = final_artifact_paths_from_raw(raw, tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    lookup_rows = {
        "source_h3": ["s1"],
        "target_h3": ["t1"],
        "source_type": ["land"],
    }
    kernel_rows = {
        "source_h3": ["s1"],
        "target_h3": ["t1"],
        "weight_terrain": [0.5],
    }
    lookup = pl.DataFrame(lookup_rows)
    bare = pl.DataFrame(kernel_rows)
    canopy = pl.DataFrame(kernel_rows)
    if duplicate_input == "lookup":
        lookup = pl.concat([lookup, lookup])
    elif duplicate_input == "bare":
        bare = pl.concat([bare, bare])
    else:
        canopy = pl.concat([canopy, canopy])
    lookup.write_parquet(paths.source_target_lookup)
    bare_path = tmp_path / "bare.parquet"
    canopy_path = tmp_path / "canopy.parquet"
    bare.write_parquet(bare_path)
    canopy.write_parquet(canopy_path)

    with pytest.raises(ValueError, match="Duplicate source-target pairs"):
        compose_dual_surface_artifacts(
            lookup_path=paths.source_target_lookup,
            bare_earth_partition_paths=[bare_path],
            canopy_partition_paths=[canopy_path],
            paths=paths,
            raw_config=raw,
            config_dir=tmp_path,
            bare_config_hash="bare-hash",
            canopy_config_hash="canopy-hash",
        )


def test_terrain_partition_rejects_duplicate_keys_before_write() -> None:
    frame = pd.DataFrame(
        {
            "source_h3_cell": ["s1", "s1"],
            "target_h3_cell": ["t1", "t1"],
            "distance_weighted_los_fraction": [0.5, 0.5],
        }
    )

    with pytest.raises(ValueError, match="duplicate source-target pairs before write"):
        _terrain_weight_partition_for_storage(frame)
