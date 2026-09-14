from __future__ import annotations

import json

import folium
import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import polars as pl
import pytest
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from viewshed_toolkit.pipeline.visualization import (
    ViewshedMapConfig,
    add_click_to_copy_coordinates,
    aggregate_target_weight_sums,
    export_viewshed_weight_maps,
    generalize_viewshed_support_geometry,
    quantile_visibility_classes,
    select_source_for_viewshed_map,
)
from viewshed_toolkit.pipeline.visualization.static_maps import (
    _masked_gaussian_filter,
)


def _h3_polygon(cell: str) -> Polygon:
    return Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)])


def _edges() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source_h3": ["source-a", "source-b", "source-a", "source-b"],
            "target_h3": ["target-1", "target-1", "target-2", "target-2"],
            "weight_distance": [0.8, 0.6, 0.4, 0.2],
            "weight_vegetation": [1.0, 0.5, 1.0, 0.25],
            "weight_terrain": [0.5, 0.0, 0.25, 0.75],
            "net_static_weight": [0.4, 0.0, 0.1, 0.0375],
        }
    )


def test_target_map_aggregation_sums_factors_and_combined_weights() -> None:
    aggregate = aggregate_target_weight_sums(_edges()).set_index("target_h3")

    assert aggregate.loc["target-1", "distance_weight_sum"] == pytest.approx(1.4)
    assert aggregate.loc["target-1", "vegetation_weight_sum"] == pytest.approx(1.0)
    assert aggregate.loc["target-1", "terrain_weight_sum"] == pytest.approx(0.5)
    assert aggregate.loc["target-1", "target_static_kernel_sum"] == pytest.approx(0.4)
    assert aggregate.loc["target-2", "target_static_kernel_sum"] == pytest.approx(0.1375)
    assert aggregate.loc["target-2", "terrain_visible_source_count"] == 2


def test_selected_map_defaults_to_source_with_most_visible_targets() -> None:
    edges = _edges()
    edges.loc[
        edges["source_h3"].eq("source-b") & edges["target_h3"].eq("target-1"), "weight_terrain"
    ] = 0.4
    edges.loc[
        edges["source_h3"].eq("source-a") & edges["target_h3"].eq("target-2"), "weight_terrain"
    ] = 0.0
    source_cells = pd.DataFrame({"h3_cell": ["source-a", "source-b"]})

    selected = select_source_for_viewshed_map(edges, source_cells)

    assert selected == "source-b"


def test_selected_map_honors_explicit_source() -> None:
    source_cells = pd.DataFrame({"h3_cell": ["source-a", "source-b"]})

    selected = select_source_for_viewshed_map(
        _edges(), source_cells, requested_source_h3="source-a"
    )

    assert selected == "source-a"


def test_selected_map_prefers_canopy_visible_targets_when_available() -> None:
    edges = _edges()
    edges["weight_canopy_los"] = [0.5, 0.25, 0.0, 0.75]
    source_cells = pd.DataFrame({"h3_cell": ["source-a", "source-b"]})

    selected = select_source_for_viewshed_map(edges, source_cells)

    assert selected == "source-b"


def test_map_aggregation_and_selection_accept_parquet_paths(tmp_path) -> None:
    edges = _edges()
    edges["weight_canopy_los"] = [0.5, 0.25, 0.0, 0.75]
    path = tmp_path / "edges.parquet"
    pl.from_pandas(edges, include_index=False).write_parquet(path)
    source_cells = pd.DataFrame({"h3_cell": ["source-a", "source-b"]})

    aggregate = aggregate_target_weight_sums(path).set_index("target_h3")
    selected = select_source_for_viewshed_map(path, source_cells)

    assert aggregate.loc["target-1", "target_static_kernel_sum"] == pytest.approx(0.4)
    assert aggregate.loc["target-2", "terrain_visible_source_count"] == 2
    assert selected == "source-b"


def test_quantile_visibility_classes_span_poor_to_high_support() -> None:
    values = np.concatenate(([np.nan, 0.0, -1.0], np.arange(1.0, 101.0)))

    classes, breaks, positive_count = quantile_visibility_classes(
        values,
        class_count=10,
    )

    assert classes[:3].tolist() == [0, 0, 0]
    assert classes[3:].min() == 1
    assert classes[3:].max() == 10
    assert np.all(np.diff(classes[3:]) >= 0)
    assert len(breaks) == 11
    assert breaks[0] == pytest.approx(1.0)
    assert breaks[-1] == pytest.approx(100.0)
    assert positive_count == 100


def test_quantile_visibility_classes_rejects_unsupported_class_count() -> None:
    with pytest.raises(ValueError, match="between 2 and 254"):
        quantile_visibility_classes(np.array([1.0]), class_count=1)


def test_generalized_support_rounds_boundary_without_expanding_extent() -> None:
    original = unary_union(
        [
            box(0.0, 0.0, 4.0, 4.0),
            box(4.0, 0.0, 5.0, 1.0),
            box(4.0, 2.0, 5.0, 3.0),
            box(-1.0, 3.0, 0.0, 4.0),
        ]
    )

    generalized = generalize_viewshed_support_geometry(
        original,
        smoothing_distance_m=0.8,
    )

    assert generalized.is_valid
    assert not generalized.is_empty
    assert generalized.difference(original).area == pytest.approx(0.0, abs=1e-9)
    assert generalized.length < original.length


def test_masked_gaussian_filter_does_not_leak_or_attenuate_at_boundary() -> None:
    values = np.ones((9, 9), dtype="float32")
    support = np.zeros((9, 9), dtype="uint8")
    support[2:7, 3:6] = 1

    smoothed = _masked_gaussian_filter(values, support, sigma_pixels=2.0)

    assert np.isnan(smoothed[support == 0]).all()
    assert np.allclose(smoothed[support == 1], 1.0, atol=1e-6)


def test_click_to_copy_coordinates_is_embedded_in_map_html() -> None:
    map_ = folium.Map(location=[48.1181, -123.4307])

    add_click_to_copy_coordinates(map_)
    html = map_.get_root().render()

    assert "Latitude, longitude" in html
    assert "Copy coordinates" in html
    assert "navigator.clipboard.writeText(coordinates)" in html
    assert "event.latlng.lat.toFixed(6)" in html


def test_map_export_reuses_external_geometry_and_raster_assets(tmp_path) -> None:
    source_h3 = h3.latlng_to_cell(48.1181, -123.4307, 7)
    targets = sorted(h3.grid_disk(source_h3, 1))[:3]
    edges = pd.DataFrame(
        {
            "source_h3": [source_h3] * len(targets),
            "target_h3": targets,
            "weight_distance": [0.9, 0.7, 0.5],
            "weight_vegetation": [0.8, 0.7, 0.6],
            "weight_terrain": [0.7, 0.5, 0.3],
            "weight_canopy_los": [0.6, 0.4, 0.2],
            "net_static_weight": [0.6, 0.4, 0.2],
        }
    )
    source_cells = gpd.GeoDataFrame(
        {"h3_cell": [source_h3]},
        geometry=[_h3_polygon(source_h3)],
        crs="EPSG:4326",
    )
    water_path = tmp_path / "water.parquet"
    gpd.GeoDataFrame(
        {"name": ["fixture-water"]},
        geometry=[gpd.GeoSeries([_h3_polygon(cell) for cell in targets]).union_all()],
        crs="EPSG:4326",
    ).to_parquet(water_path)
    output_dir = tmp_path / "maps"

    result = export_viewshed_weight_maps(
        edges,
        source_cells,
        water_path,
        output_dir,
        value_table_path=tmp_path / "target_values.parquet",
        center_lat=48.1181,
        center_lon=-123.4307,
        source_selection_radius_km=2.0,
        viewshed_radius_km=5.0,
        requested_source_h3=source_h3,
        config=ViewshedMapConfig(
            analysis_pixel_size_m=500.0,
            output_pixel_size_m=500.0,
            gaussian_sigma_km=0.5,
            support_boundary_smoothing_km=0.0,
            raster_padding_km=0.5,
            externalize_map_assets=True,
        ),
    )

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    target_asset = output_dir / "assets" / "target_h3_values.geojson"
    assert manifest["map_asset_mode"] == "external_shared"
    assert target_asset.is_file()
    assert len(json.loads(target_asset.read_text())["features"]) == len(targets)

    h3_html = result.maps["terrain"]["h3_html"].read_text(encoding="utf-8")
    smooth_html = result.maps["terrain"]["smoothed_html"].read_text(encoding="utf-8")
    assert "assets/target_h3_values.geojson" in h3_html
    assert "assets/target_h3_values.geojson" in smooth_html
    assert "data:image/png;base64" not in smooth_html
    assert "smoothed/smoothed_terrain_weight_sum.png" in smooth_html

    embedded_dir = tmp_path / "embedded_maps"
    embedded_result = export_viewshed_weight_maps(
        edges,
        source_cells,
        water_path,
        embedded_dir,
        value_table_path=tmp_path / "embedded_target_values.parquet",
        center_lat=48.1181,
        center_lon=-123.4307,
        source_selection_radius_km=2.0,
        viewshed_radius_km=5.0,
        requested_source_h3=source_h3,
        config=ViewshedMapConfig(
            analysis_pixel_size_m=500.0,
            output_pixel_size_m=500.0,
            gaussian_sigma_km=0.5,
            support_boundary_smoothing_km=0.0,
            raster_padding_km=0.5,
            externalize_map_assets=False,
        ),
    )
    embedded_manifest = json.loads(embedded_result.manifest.read_text(encoding="utf-8"))
    embedded_smooth_html = embedded_result.maps["terrain"]["smoothed_html"].read_text(
        encoding="utf-8"
    )
    assert embedded_manifest["map_asset_mode"] == "embedded"
    assert embedded_manifest["shared_geometry_assets"] == {}
    assert "data:image/png;base64" in embedded_smooth_html
