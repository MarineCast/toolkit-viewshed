from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl

from viewshed_toolkit.pipeline.contracts.artifacts import FINAL_SCHEMAS
from viewshed_toolkit.pipeline.weights.terrain import cleanup


def test_combine_partitions_preserves_complete_clear_sky_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    partition_dir = tmp_path / "partitions"
    partition_dir.mkdir()
    partition_path = partition_dir / "source_h3_cell=source.parquet"
    pl.DataFrame(
        {
            "source_h3": ["source"],
            "target_h3": ["target"],
            "weight_terrain": [0.4],
            "aggregation_method": ["analytic_open_water_sample_kernel_v2"],
            "target_water_sample_count": [12],
        }
    ).write_parquet(partition_path)

    lookup_path = tmp_path / "lookup.parquet"
    pl.DataFrame(
        {
            "source_h3": ["source"],
            "target_h3": ["target"],
            "distance_km": [1.0],
            "source_type": ["water"],
        }
    ).write_parquet(lookup_path)

    terrain_path = tmp_path / "terrain.parquet"
    clear_sky_path = tmp_path / "clear_sky.parquet"
    artifact_paths = SimpleNamespace(
        ocean_source_target_clear_sky=clear_sky_path,
        source_target_clear_sky=tmp_path / "unused_land_clear_sky.parquet",
    )
    app = SimpleNamespace(config_path=tmp_path / "config.yaml")

    monkeypatch.setattr(cleanup, "_source_type_for_app", lambda _app: "water")
    monkeypatch.setattr(
        cleanup,
        "_partitioned_visibility_dir_for_app",
        lambda _app: partition_dir,
    )
    monkeypatch.setattr(cleanup, "expected_partition_metadata", lambda _app: {})
    monkeypatch.setattr(
        cleanup,
        "partition_metadata_matches",
        lambda _path, _metadata: True,
    )
    monkeypatch.setattr(cleanup, "_area_lookup_path_for_app", lambda _app: lookup_path)
    monkeypatch.setattr(cleanup, "_terrain_weights_final_path", lambda _app: terrain_path)
    monkeypatch.setattr(cleanup, "final_artifact_paths", lambda _path: artifact_paths)

    result = cleanup.combine_partitions(app)
    clear_sky = pl.read_parquet(clear_sky_path)

    assert clear_sky.columns == list(FINAL_SCHEMAS["source_target_clear_sky"])
    assert clear_sky.item(0, "aggregation_method") == ("analytic_open_water_sample_kernel_v2")
    assert clear_sky.item(0, "target_water_sample_count") == 12
    assert result["source_target_clear_sky_rows"] == 1
