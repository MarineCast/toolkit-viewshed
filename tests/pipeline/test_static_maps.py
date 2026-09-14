from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import h3
import polars as pl
import yaml
from shapely.geometry import Polygon

from viewshed_toolkit.pipeline.visualization import (
    export_source_type_static_weight_map,
    export_static_weight_maps,
)


def _polygon(cell: str) -> Polygon:
    return Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)])


def _write_fixture(tmp_path: Path) -> Path:
    latitude = 48.135238
    longitude = -122.767230
    resolution = 7
    source = h3.latlng_to_cell(latitude, longitude, resolution)
    targets = sorted(h3.grid_disk(source, 1))
    water_path = tmp_path / "water.parquet"
    gpd.GeoDataFrame(
        {"name": ["fixture"]},
        geometry=[gpd.GeoSeries([_polygon(cell) for cell in targets]).union_all()],
        crs="EPSG:4326",
    ).to_parquet(water_path)

    output_dir = tmp_path / "viewshed"
    output_dir.mkdir()
    static_weights = pl.DataFrame(
        {
            "source_h3": [source] * len(targets),
            "target_h3": targets,
            "weight_distance": [0.9 - index * 0.05 for index in range(len(targets))],
            "weight_vegetation": [0.8 - index * 0.04 for index in range(len(targets))],
            "weight_terrain": [0.7 - index * 0.06 for index in range(len(targets))],
            "weight_static_viewability": [
                (0.7 - index * 0.06) * (0.8 - index * 0.04) for index in range(len(targets))
            ],
        }
    )
    static_weights.write_parquet(output_dir / "LAND_STATIC_WEIGHTS_R7.parquet")
    static_weights.with_columns(pl.lit(1.0).alias("weight_vegetation")).write_parquet(
        output_dir / "WATER_STATIC_WEIGHTS_R7.parquet"
    )

    config_path = tmp_path / "viewshed.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "run": {
                    "name": "static-map-fixture",
                    "version": "static-map-fixture-v1",
                    "write_maps": True,
                },
                "region": {
                    "name": "fixture",
                    "bbox_wgs84": {
                        "min_lon": -122.80,
                        "min_lat": 48.11,
                        "max_lon": -122.73,
                        "max_lat": 48.17,
                    },
                },
                "viewshed": {
                    "crs_projected": "EPSG:32610",
                    "max_distance_m": 5000,
                },
                "h3": {"source_resolution": resolution, "target_resolution": resolution},
                "batch": {},
                "raster": {},
                "paths": {
                    "water_polygon_path": str(water_path),
                    "output_dir": str(output_dir),
                    "map_dir": str(output_dir / "maps"),
                    "regional_dem_path": str(tmp_path / "dem.tif"),
                    "canopy_height_path": str(tmp_path / "chm.tif"),
                    "source_cells_path": str(tmp_path / "land_h3.parquet"),
                    "land_h3_path": str(tmp_path / "land_h3.parquet"),
                    "projected_dem_path": str(tmp_path / "projected_dem.tif"),
                    "final_visibility_path": str(tmp_path / "terrain.parquet"),
                    "partitioned_visibility_dir": str(tmp_path / "partitions"),
                    "manifest_path": str(tmp_path / "manifest.csv"),
                    "raw_dem_dir": str(tmp_path / "raw_dem"),
                },
                "static_maps": {
                    "selected_location": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    "analysis_pixel_size_m": 500,
                    "output_pixel_size_m": 500,
                    "gaussian_sigma_km": 0.5,
                    "support_boundary_smoothing_km": 0.0,
                    "raster_padding_km": 0.5,
                    "externalize_map_assets": True,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def test_static_map_export_writes_source_type_maps_with_all_static_layers(tmp_path) -> None:
    config_path = _write_fixture(tmp_path)

    result = export_static_weight_maps(config_path)

    html_files = sorted(result.selected_html.parent.glob("*.html"))
    assert result.selected_html.parent == tmp_path / "viewshed/maps/h3r7/static"
    assert html_files == sorted(
        [
            result.selected_html,
            result.land_aggregate_html,
            result.water_aggregate_html,
        ]
    )
    selected_html = result.selected_html.read_text(encoding="utf-8")
    land_aggregate_html = result.land_aggregate_html.read_text(encoding="utf-8")
    water_aggregate_html = result.water_aggregate_html.read_text(encoding="utf-8")
    for factor in (
        "Distance diagnostic",
        "Conditional vegetation support",
        "Terrain support",
        "Combined static weight",
    ):
        escaped_factor = factor.replace("—", r"\u2014")
        assert f"{escaped_factor} \\u2014 original H3" in selected_html
        assert f"{escaped_factor} \\u2014 smooth" in selected_html
        for source_label, aggregate_html in (
            ("Land-source", land_aggregate_html),
            ("Water-source", water_aggregate_html),
        ):
            assert (
                f"{source_label} target aggregate: {escaped_factor} \\u2014 original H3"
                in aggregate_html
            )
            assert (
                f"{source_label} target aggregate: {escaped_factor} \\u2014 smooth"
                in aggregate_html
            )
            assert (
                f"{source_label} source aggregate: {escaped_factor} \\u2014 original H3"
                in aggregate_html
            )
            assert (
                f"{source_label} source aggregate: {escaped_factor} \\u2014 smooth"
                in aggregate_html
            )
    assert "Configured point, source H3, and viewshed radius" in selected_html
    assert "Copy coordinates" in selected_html
    assert "weather" not in selected_html.lower()
    assert "daylight" not in selected_html.lower()
    # The canonical pages must work when opened directly as file:// URLs.
    # External Folium GeoJSON uses blocked XMLHttpRequests and renders blank.
    assert "$.ajax(" not in selected_html
    assert "$.ajax(" not in land_aggregate_html
    assert "$.ajax(" not in water_aggregate_html
    assert '"type": "FeatureCollection"' in selected_html
    assert '"type": "FeatureCollection"' in land_aggregate_html
    assert '"type": "FeatureCollection"' in water_aggregate_html

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["representations"] == ["original_h3", "smooth"]
    assert manifest["excluded_dynamic_factors"] == ["weather", "daylight", "lunar"]
    assert manifest["scientific_config_hash"]
    assert set(manifest["static_artifacts"]) == {"land", "water"}
    for source_type in ("land", "water"):
        alias_path = (
            result.manifest.parent / f"{source_type}_source_static_weight_map_manifest.json"
        )
        alias = json.loads(alias_path.read_text(encoding="utf-8"))
        assert alias["config_hash"] == manifest["config_hash"]
        assert alias["scientific_config_hash"] == manifest["scientific_config_hash"]
        assert alias["source_type"] == source_type
    assert manifest["source_types"] == ["land", "water"]
    assert manifest["aggregations"] == {
        "source": "sum across target H3 cells",
        "target": "sum across source H3 cells",
    }


def test_static_map_export_reuses_matching_outputs_without_overwrite(tmp_path) -> None:
    config_path = _write_fixture(tmp_path)

    first = export_static_weight_maps(config_path)
    second = export_static_weight_maps(config_path)

    assert second == first


def test_land_only_export_does_not_require_water_static_weights(tmp_path) -> None:
    config_path = _write_fixture(tmp_path)
    (tmp_path / "viewshed/WATER_STATIC_WEIGHTS_R7.parquet").unlink()

    result = export_source_type_static_weight_map(
        config_path,
        source_type="land",
    )

    assert result.source_type == "land"
    assert result.aggregate_html.is_file()
    assert result.target_aggregate_values.is_file()
    assert result.source_aggregate_values.is_file()
    assert result.manifest.is_file()
    assert not (
        result.aggregate_html.parent / "water_source_aggregate_static_weights.html"
    ).exists()
    html = result.aggregate_html.read_text(encoding="utf-8")
    assert "Land-source target aggregate" in html
    assert '"type": "FeatureCollection"' in html
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["source_type"] == "land"
