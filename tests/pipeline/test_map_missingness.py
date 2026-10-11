from __future__ import annotations

import json

import folium
import h3
import numpy as np
import pandas as pd
import polars as pl
import pytest

from viewshed_toolkit.pipeline.visualization.data import (
    ViewshedMapConfig,
    _aggregate_edges,
    _prepare_map_edges,
    aggregate_target_weight_sums,
)
from viewshed_toolkit.pipeline.visualization.interactive_maps import (
    _add_h3_metric_layer,
    _add_h3_value_hover,
    _write_geojson_asset,
)


def edges() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "source_h3": ["a", "b", "a", "b", "a", "b"],
            "target_h3": ["complete", "complete", "partial", "partial", "unknown", "unknown"],
            "weight_distance": [0.0, 0.8, 0.2, 0.4, 0.5, 0.6],
            "weight_terrain": [0.0, 0.5, 0.5, 0.5, 0.5, None],
            "weight_vegetation": [1.0, 0.0, 0.4, None, None, None],
            "net_static_weight": [0.0, 0.0, 0.2, None, None, None],
        }
    )


def test_unknown_partial_zero_and_not_applicable_remain_distinct(tmp_path):
    result = aggregate_target_weight_sums(edges()).set_index("target_h3")
    metric = "target_static_kernel_sum"
    assert result.loc["complete", metric] == 0
    assert result.loc["complete", metric + "_valid_count"] == 2
    assert result.loc["complete", metric + "_mean_valid"] == 0
    assert pd.isna(result.loc["partial", metric])
    assert result.loc["partial", metric + "_available"] == pytest.approx(0.2)
    assert result.loc["partial", metric + "_mean_valid"] == pytest.approx(0.2)
    assert result.loc["partial", metric + "_coverage_fraction"] == 0.5
    assert result.loc["partial", metric + "_status"] == "partial"
    assert pd.isna(result.loc["unknown", metric])
    assert pd.isna(result.loc["unknown", metric + "_available"])
    assert result.loc["unknown", metric + "_status"] == "unavailable"
    assert result.loc["complete", "vegetation_weight_sum_valid_count"] == 1
    assert result.loc["complete", "vegetation_weight_sum_not_applicable_count"] == 1
    path = tmp_path / "values.parquet"
    result.reset_index().to_parquet(path, index=False)
    saved = pl.read_parquet(path).filter(pl.col("target_h3") == "unknown")
    assert saved[metric].item() is None
    reordered = aggregate_target_weight_sums(edges().reverse()).set_index("target_h3")
    pd.testing.assert_frame_equal(result, reordered)


def test_source_and_target_aggregations_share_coverage_contract():
    frame = _prepare_map_edges(edges().lazy())
    source = _aggregate_edges(frame, by="source_h3")
    row = source.filter(pl.col("source_h3") == "a").row(0, named=True)
    assert row["source_static_kernel_sum"] is None
    assert row["source_static_kernel_sum_valid_count"] == 2
    assert row["source_static_kernel_sum_missing_count"] == 1
    assert row["source_static_kernel_sum_mean_valid"] == pytest.approx(0.1)
    target = _aggregate_edges(frame, by="target_h3").to_pandas()
    generic = aggregate_target_weight_sums(edges())
    pd.testing.assert_frame_equal(
        target[generic.columns.intersection(target.columns)],
        generic[generic.columns.intersection(target.columns)],
    )


def test_water_neutral_vegetation_is_not_an_observed_value():
    water = edges().with_columns(
        pl.lit("water").alias("source_type"), pl.lit(1.0).alias("weight_vegetation")
    )
    result = aggregate_target_weight_sums(water)
    assert result["vegetation_weight_sum"].isna().all()
    assert (result["vegetation_weight_sum_valid_count"] == 0).all()
    assert (result["vegetation_weight_sum_missing_count"] == 0).all()
    assert (result["vegetation_weight_sum_not_applicable_count"] == 2).all()
    assert (result["vegetation_weight_sum_status"] == "not_applicable").all()


def test_nan_is_missing_and_infinite_out_of_range_or_duplicate_inputs_rejected():
    nan = edges().with_columns(pl.lit(float("nan")).alias("net_static_weight"))
    assert aggregate_target_weight_sums(nan)["target_static_kernel_sum"].isna().all()
    for value in [float("inf"), -0.1, 1.1]:
        with pytest.raises(ValueError, match="finite weights"):
            aggregate_target_weight_sums(
                edges().with_columns(pl.lit(value).alias("weight_terrain"))
            )
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_target_weight_sums(pl.concat([edges(), edges().head(1)]))
    with pytest.raises(ValueError, match="one source role"):
        aggregate_target_weight_sums(
            edges().with_columns(pl.Series("source_type", ["land", "water"] * 3))
        )
    assert aggregate_target_weight_sums(edges().head(0)).empty


def test_html_and_external_geojson_keep_null_coverage_and_distinct_style(tmp_path):
    cells = sorted(h3.grid_disk(h3.latlng_to_cell(48.15, -123.12, 7), 1))[:3]
    metric = "target_static_kernel_sum"
    frame = pd.DataFrame(
        {
            "target_h3": cells,
            metric: [np.nan, 0.0, 0.4],
            metric + "_status": ["unavailable", "complete", "complete"],
            metric + "_valid_count": [0, 2, 2],
            metric + "_missing_count": [2, 0, 0],
        }
    )
    map_ = folium.Map(location=[48.15, -123.12])
    gdf = _add_h3_metric_layer(
        map_,
        frame,
        h3_column="target_h3",
        metric=metric,
        name="fixture",
        output_path=tmp_path / "map.html",
        config=ViewshedMapConfig(),
        show=True,
    )
    layer = next(c for c in map_._children.values() if isinstance(c, folium.GeoJson))
    unknown, zero, positive = layer.data["features"]
    assert unknown["properties"][metric] is None
    assert unknown["properties"][metric + "_valid_count"] == 0
    assert layer.style_function(unknown)["dashArray"] == "4 3"
    assert layer.style_function(unknown)["fillOpacity"] > 0
    assert layer.style_function(zero)["fillOpacity"] == 0
    assert layer.style_function(positive)["fillOpacity"] > 0
    asset = _write_geojson_asset(gdf, tmp_path / "values.geojson")
    assert json.loads(asset.path.read_text())["features"][0]["properties"][metric] is None
    _add_h3_value_hover(
        map_, gdf, metric=metric, caption="Fixture", output_path=tmp_path / "map.html"
    )
    html = map_.get_root().render()
    assert '"target_static_kernel_sum": null' in html
    assert '"target_static_kernel_sum_status": "unavailable"' in html
    assert "NaN" not in html
