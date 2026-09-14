from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from viewshed_toolkit.pipeline.weights.terrain.gdal import (
    _terrain_weight_partition_for_storage,
)


def test_empty_terrain_partition_writes_h3_columns_as_strings(tmp_path) -> None:
    partition = _terrain_weight_partition_for_storage(pd.DataFrame())
    path = tmp_path / "empty_terrain_partition.parquet"

    partition.to_parquet(path, index=False)
    schema = pq.read_schema(path)

    source_type = schema.field("source_h3").type
    target_type = schema.field("target_h3").type
    assert pa.types.is_string(source_type) or pa.types.is_large_string(source_type)
    assert target_type == source_type


def test_nonempty_terrain_partition_uses_same_h3_schema(tmp_path) -> None:
    partition = _terrain_weight_partition_for_storage(
        pd.DataFrame(
            {
                "source_h3_cell": ["8828c24821fffff"],
                "target_h3_cell": ["8828c24823fffff"],
                "terrain_visibility_support": [0.75],
                "joint_los_fraction": [0.25],
                "distance_weighted_los_fraction": [0.15],
                "any_observer_support_fraction": [0.5],
                "union_visible_target_fraction": [0.75],
                "visible_observer_pixel_count_sum": [25],
                "los_distance_weight_sum": [15.0],
                "visible_sampled_pixel_count": [15],
                "n_observers": [4],
                "sample_points_requested": [5],
                "sample_points_actual": [4],
                "target_water_pixel_count": [100],
                "pixel_stride": [2],
            }
        )
    )
    path = tmp_path / "nonempty_terrain_partition.parquet"

    partition.to_parquet(path, index=False)
    schema = pq.read_schema(path)

    source_type = schema.field("source_h3").type
    target_type = schema.field("target_h3").type
    assert pa.types.is_string(source_type) or pa.types.is_large_string(source_type)
    assert target_type == source_type
    assert partition.loc[0, "weight_terrain"] == pytest.approx(0.15)
    assert partition.loc[0, "joint_los_fraction"] == pytest.approx(0.25)
    assert partition.loc[0, "distance_weighted_los_fraction"] == pytest.approx(0.15)
    assert partition.loc[0, "any_observer_support_fraction"] == pytest.approx(0.5)
    assert partition.loc[0, "union_visible_target_fraction"] == pytest.approx(0.75)
    assert pa.types.is_int64(schema.field("visible_observer_pixel_count_sum").type)
    assert pa.types.is_int64(schema.field("sample_points_requested").type)
    assert pa.types.is_int64(schema.field("sample_points_actual").type)
    assert pa.types.is_int64(schema.field("target_water_pixel_count").type)

    empty_partition = _terrain_weight_partition_for_storage(pd.DataFrame())
    assert empty_partition.dtypes.equals(partition.dtypes)
