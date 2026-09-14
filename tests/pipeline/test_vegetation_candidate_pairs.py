from __future__ import annotations

from pathlib import Path

import polars as pl

from viewshed_toolkit.pipeline.weights.vegetation.candidate_pairs import (
    candidate_pair_counts,
    materialize_candidate_pairs,
)
from viewshed_toolkit.pipeline.weights.vegetation.pair_chunks import (
    iter_candidate_pair_chunks,
)
from viewshed_toolkit.pipeline.weights.vegetation.path_config import (
    VegetationPathConfig,
)
from viewshed_toolkit.pipeline.weights.vegetation.summarize import (
    cleanup_vegetation_intermediates,
)


def _config(tmp_path: Path, source: Path) -> VegetationPathConfig:
    return VegetationPathConfig(
        raw={
            "filtering": {
                "only_visible_pairs": True,
                "min_terrain_visibility_support": 0.2,
                "max_distance_km": 2.0,
                "min_clear_sky_weight": 0.3,
            },
            "geometry": {"use_pair_geometry": False},
        },
        config_dir=tmp_path,
        resolution_m=30,
        inputs={"clear_sky_pairs": source},
        outputs={
            "candidate_pairs": tmp_path / "VEGETATION_CANDIDATE_PAIRS.parquet",
            "pair_chunks": tmp_path / "pair_chunks",
        },
        columns={
            "source_id": "source_h3",
            "target_id": "target_h3",
            "distance_km": "distance_km",
            "clear_sky_weight": "clear_weight",
        },
        nodata={},
        raw_config={"run": {"version": "test"}},
        run_version="test",
        config_hash="candidate-test",
    )


def test_candidate_pairs_are_filtered_once_and_streamed_without_refiltering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "clear_sky.parquet"
    pl.DataFrame(
        {
            "source_h3": ["a", "b", "c", "d", "e", "f"],
            "target_h3": ["t"] * 6,
            "source_type": ["land", "water", "land", "land", "land", "land"],
            "terrain_visible_clear_sky": [True, True, False, True, True, True],
            "weight_terrain": [0.5, 0.5, 0.5, 0.1, 0.5, 0.5],
            "clear_weight": [0.5, 0.5, 0.5, 0.5, 0.5, 0.2],
            "distance_km": [1.0, 1.0, 1.0, 1.0, 3.0, 1.0],
            "geometry": [b"unused"] * 6,
        }
    ).write_parquet(source)
    cfg = _config(tmp_path, source)

    assert candidate_pair_counts(cfg) == {
        "raw_pair_count": 6,
        "after_visibility_filter": 4,
        "after_terrain_support_filter": 3,
        "after_distance_filter": 2,
        "after_min_weight_filter": 1,
    }
    artifact = materialize_candidate_pairs(cfg)
    reused = materialize_candidate_pairs(cfg)

    assert artifact.row_count == 1
    assert artifact.reused is False
    assert reused.reused is True
    candidate = pl.read_parquet(artifact.path)
    assert candidate["source_h3"].to_list() == ["a"]
    assert candidate["chunk_parent_h3"].to_list() == ["a"]
    assert "geometry" not in candidate.columns

    totals: dict[str, int] = {}
    chunks = list(
        iter_candidate_pair_chunks(
            cfg,
            chunk_size=1,
            count_totals=totals,
        )
    )
    assert [chunk["source_h3"].tolist() for chunk in chunks] == [["a"]]
    assert totals == artifact.filter_counts

    chunk_path = cfg.outputs["pair_chunks"] / "chunk_000000.parquet"
    chunk_path.parent.mkdir()
    chunk_path.write_bytes(b"scratch")
    cleanup_vegetation_intermediates(cfg)
    assert artifact.path.exists()
    assert not chunk_path.exists()
