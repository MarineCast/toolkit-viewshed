from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
from shapely import from_wkt
from shapely.geometry import MultiPolygon, Point, Polygon

from viewshed_toolkit.pipeline.config.loader import H3Config
from viewshed_toolkit.pipeline.prepare.area import (
    calculate_source_sample_count,
    prepare_source_samples,
    sample_points_in_source_geometry,
)
from viewshed_toolkit.pipeline.weights.terrain.gdal import (
    _metadata_values_match,
    _terrain_partition_config_hash,
    expected_partition_metadata,
)


def test_h3_sampling_defaults_preserve_fixed_mode() -> None:
    config = H3Config()
    assert config.source_sampling_mode == "fixed"
    assert config.sample_points_per_source_cell == 5
    assert config.min_sample_points_per_source_cell == 1
    assert config.aggregation_mode == "full"
    assert config.pixel_stride == 1


@pytest.mark.parametrize(
    ("source_type", "land_fraction", "water_fraction", "expected_fraction", "expected"),
    [
        ("land", 1.00, 0.00, 1.00, 10),
        ("land", 0.80, 0.20, 0.80, 8),
        ("land", 0.50, 0.50, 0.50, 5),
        ("land", 0.26, 0.74, 0.26, 3),
        ("land", 0.10, 0.90, 0.10, 1),
        ("land", 0.01, 0.99, 0.01, 1),
        ("water", 0.10, 0.90, 0.90, 9),
        ("water", 0.95, 0.25, 0.25, 3),
        ("water", 0.05, 1.5, 1.0, 10),
    ],
)
def test_active_fraction_source_sample_count(
    source_type: str,
    land_fraction: float,
    water_fraction: float,
    expected_fraction: float,
    expected: int,
) -> None:
    fraction, count = calculate_source_sample_count(
        source_type=source_type,
        land_fraction=land_fraction,
        water_fraction=water_fraction,
        sampling_mode="active_fraction",
        max_samples=10,
        min_samples=1,
    )
    assert fraction == pytest.approx(expected_fraction)
    assert count == expected


@pytest.mark.parametrize(("active_fraction", "expected"), [(0.25, 3), (0.15, 2)])
def test_active_fraction_rounding_is_half_up(active_fraction: float, expected: int) -> None:
    _, count = calculate_source_sample_count(
        source_type="land",
        land_fraction=active_fraction,
        water_fraction=1.0 - active_fraction,
        sampling_mode="active_fraction",
        max_samples=10,
        min_samples=1,
    )
    assert count == expected


def test_fixed_sampling_preserves_legacy_maximum() -> None:
    fraction, count = calculate_source_sample_count(
        source_type="land",
        land_fraction=None,
        water_fraction=None,
        sampling_mode="fixed",
        max_samples=7,
        min_samples=2,
    )
    assert fraction == 1.0
    assert count == 7


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"sampling_mode": "unknown"}, "sampling_mode"),
        ({"max_samples": 0}, "max_samples"),
        ({"min_samples": 0}, "min_samples"),
        ({"max_samples": 2, "min_samples": 3}, "min_samples"),
        ({"land_fraction": None}, "finite land_fraction"),
        ({"land_fraction": float("nan")}, "finite land_fraction"),
        ({"source_type": "shore"}, "source_type"),
    ],
)
def test_source_sample_count_validation(kwargs: dict[str, object], match: str) -> None:
    values: dict[str, object] = {
        "source_type": "land",
        "land_fraction": 0.5,
        "water_fraction": 0.5,
        "sampling_mode": "active_fraction",
        "max_samples": 10,
        "min_samples": 1,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=match):
        calculate_source_sample_count(**values)  # type: ignore[arg-type]


def test_geometry_sampling_returns_unique_points_inside_active_geometry() -> None:
    geometry = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    samples = sample_points_in_source_geometry("source", geometry, 10)

    assert len(samples) == 10
    assert samples[["lon", "lat"]].drop_duplicates().shape[0] == 10
    assert samples.geometry.map(geometry.covers).all()
    assert (samples["sample_points_requested"] == 10).all()
    assert (samples["sample_points_actual"] == 10).all()


def test_geometry_sampling_is_equivalent_from_wgs84_or_projected_input() -> None:
    projected_geometry = Polygon(
        [
            (480_000.0, 5_370_000.0),
            (481_000.0, 5_370_000.0),
            (481_000.0, 5_371_000.0),
            (480_000.0, 5_371_000.0),
        ]
    )
    wgs84_geometry = (
        gpd.GeoSeries([projected_geometry], crs="EPSG:32610").to_crs("EPSG:4326").iloc[0]
    )

    from_wgs84 = sample_points_in_source_geometry(
        "source",
        wgs84_geometry,
        7,
        geometry_crs="EPSG:4326",
        projected_crs="EPSG:32610",
    ).to_crs("EPSG:32610")
    from_projected = sample_points_in_source_geometry(
        "source",
        projected_geometry,
        7,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
    ).to_crs("EPSG:32610")

    wgs84_coordinates = list(zip(from_wgs84.geometry.x, from_wgs84.geometry.y))
    projected_coordinates = list(zip(from_projected.geometry.x, from_projected.geometry.y))
    assert np.allclose(wgs84_coordinates, projected_coordinates, atol=1e-6)


def test_geometry_sampling_designs_are_nested_across_requested_counts() -> None:
    geometry = Polygon(
        [
            (480_000.0, 5_370_000.0),
            (481_000.0, 5_370_000.0),
            (481_000.0, 5_371_000.0),
            (480_000.0, 5_371_000.0),
        ]
    )

    five_points = sample_points_in_source_geometry(
        "source",
        geometry,
        5,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=5,
    )
    ten_points = sample_points_in_source_geometry(
        "source",
        geometry,
        10,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=10,
    )

    assert five_points["sample_id"].tolist() == ten_points["sample_id"].head(5).tolist()
    assert np.allclose(
        five_points[["lon", "lat"]].to_numpy(),
        ten_points[["lon", "lat"]].head(5).to_numpy(),
        atol=1e-12,
    )


def test_geometry_sampling_seeds_disconnected_components_before_refinement() -> None:
    mainland = Polygon(
        [
            (480_000.0, 5_370_000.0),
            (481_000.0, 5_370_000.0),
            (481_000.0, 5_371_000.0),
            (480_000.0, 5_371_000.0),
        ]
    )
    island = Polygon(
        [
            (481_400.0, 5_370_400.0),
            (481_440.0, 5_370_400.0),
            (481_440.0, 5_370_440.0),
            (481_400.0, 5_370_440.0),
        ]
    )
    geometry = MultiPolygon([mainland, island])

    two_points = sample_points_in_source_geometry(
        "source",
        geometry,
        2,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=10,
    ).to_crs("EPSG:32610")
    ten_points = sample_points_in_source_geometry(
        "source",
        geometry,
        10,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=10,
    ).to_crs("EPSG:32610")

    assert any(mainland.covers(point) for point in two_points.geometry)
    assert any(island.covers(point) for point in two_points.geometry)
    assert np.allclose(
        two_points[["lon", "lat"]].to_numpy(),
        ten_points[["lon", "lat"]].head(2).to_numpy(),
        atol=1e-12,
    )


def test_geometry_sampling_rejects_design_smaller_than_request() -> None:
    geometry = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])

    with pytest.raises(ValueError, match="max_design_points"):
        sample_points_in_source_geometry(
            "source",
            geometry,
            6,
            max_design_points=5,
        )


def test_geometry_sampling_rejects_geographic_distance_crs() -> None:
    geometry = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])

    with pytest.raises(ValueError, match="projected coordinate reference system"):
        sample_points_in_source_geometry(
            "source",
            geometry,
            5,
            projected_crs="EPSG:4326",
        )


def test_geometry_sampling_does_not_pad_with_duplicate_points() -> None:
    samples = sample_points_in_source_geometry("source", Point(0.0, 0.0), 5)

    assert len(samples) == 1
    assert samples[["lon", "lat"]].drop_duplicates().shape[0] == 1
    assert samples.iloc[0]["sample_points_requested"] == 5
    assert samples.iloc[0]["sample_points_actual"] == 1


def test_prepare_source_samples_records_adaptive_metadata() -> None:
    app = SimpleNamespace(
        source_type="land",
        viewshed=SimpleNamespace(crs_projected="EPSG:32610"),
        h3=SimpleNamespace(
            source_sampling_mode="active_fraction",
            sample_points_per_source_cell=10,
            min_sample_points_per_source_cell=1,
            include_centroid=True,
        ),
    )
    source = gpd.GeoDataFrame(
        {"h3_cell": ["source"], "land_fraction": [0.2], "water_fraction": [0.8]},
        geometry=[Point(-123.0, 48.5)],
        crs="EPSG:4326",
    )

    samples, diagnostics = prepare_source_samples(app, source)

    assert len(samples) == 1
    assert samples.iloc[0]["source_type"] == "land"
    assert samples.iloc[0]["active_source_fraction"] == pytest.approx(0.2)
    assert samples.iloc[0]["sample_points_requested"] == 2
    assert samples.iloc[0]["sample_points_actual"] == 1
    assert samples.iloc[0]["source_sampling_projected_crs"] == "EPSG:32610"
    assert samples.iloc[0]["source_sampling_candidate_grid_side"] == 32
    assert samples.iloc[0]["source_sampling_max_design_points"] == 10
    assert diagnostics.iloc[0]["duplicate_sample_coordinate_count"] == 0
    assert diagnostics.iloc[0]["source_sampling_projected_crs"] == "EPSG:32610"
    assert diagnostics.iloc[0]["source_sampling_candidate_grid_side"] == 32
    assert diagnostics.iloc[0]["source_sampling_max_design_points"] == 10
    assert diagnostics.iloc[0]["sampling_warning"]


def test_prepare_source_samples_adaptive_design_is_prefix_of_fixed_design() -> None:
    def sampling_app(mode: str) -> SimpleNamespace:
        return SimpleNamespace(
            source_type="land",
            viewshed=SimpleNamespace(crs_projected="EPSG:32610"),
            h3=SimpleNamespace(
                source_sampling_mode=mode,
                sample_points_per_source_cell=10,
                min_sample_points_per_source_cell=1,
                include_centroid=True,
            ),
        )

    source = gpd.GeoDataFrame(
        {
            "h3_cell": ["source"],
            "land_fraction": [0.5],
            "water_fraction": [0.5],
        },
        geometry=[
            Polygon(
                [
                    (480_000.0, 5_370_000.0),
                    (481_000.0, 5_370_000.0),
                    (481_000.0, 5_371_000.0),
                    (480_000.0, 5_371_000.0),
                ]
            )
        ],
        crs="EPSG:32610",
    )

    adaptive, _ = prepare_source_samples(sampling_app("active_fraction"), source)
    fixed, _ = prepare_source_samples(sampling_app("fixed"), source)

    assert len(adaptive) == 5
    assert len(fixed) == 10
    assert adaptive["sample_id"].tolist() == fixed["sample_id"].head(5).tolist()
    assert np.allclose(
        adaptive[["lon", "lat"]].to_numpy(),
        fixed[["lon", "lat"]].head(5).to_numpy(),
        atol=1e-12,
    )


def test_prepare_source_samples_repairs_projected_validation_geometry() -> None:
    # This geometry is valid in its source WGS84 representation but acquires a
    # microscopic self-intersection when transformed to the configured UTM
    # CRS. Sampling already repairs the projected geometry; validation must
    # evaluate the same repaired polygon rather than buffering the invalid one.
    projected_geometry = from_wkt(
        "MULTIPOLYGON (((297446.51544261305 5444395.77976076, "
        "298050.1177159834 5443557.307647555, "
        "297541.92314228823 5443800.473895211, "
        "297082.9712796614 5444204.961033375, "
        "297121.75194470445 5444348.576165313, "
        "297446.51544261305 5444395.77976076)), "
        "((295799.16690396087 5443584.697424654, "
        "296753.7632122154 5442854.992731674, "
        "296939.74058255856 5442823.902644456, "
        "296968.98926552606 5442886.576086301, "
        "296713.661622407 5442339.513644343, "
        "296928.8087137124 5442161.231071712, "
        "296212.36350227066 5442057.253695776, "
        "295641.3479190349 5442850.7174373865, "
        "295658.6412712043 5443318.469259317, "
        "295799.16690396087 5443584.697424654)))"
    )
    assert not projected_geometry.is_valid
    app = SimpleNamespace(
        source_type="land",
        viewshed=SimpleNamespace(crs_projected="EPSG:32610"),
        h3=SimpleNamespace(
            source_sampling_mode="active_fraction",
            sample_points_per_source_cell=10,
            min_sample_points_per_source_cell=1,
            include_centroid=True,
        ),
    )
    source = gpd.GeoDataFrame(
        {
            "h3_cell": ["source"],
            "land_fraction": [0.32906390160716514],
            "water_fraction": [0.6633191455279676],
        },
        geometry=[projected_geometry],
        crs="EPSG:32610",
    )

    samples, diagnostics = prepare_source_samples(app, source)

    assert len(samples) == 3
    assert diagnostics.iloc[0]["sample_points_actual"] == 3


def _fake_app(config_hash: str, sampling_mode: str = "active_fraction") -> SimpleNamespace:
    return SimpleNamespace(
        raw_config={"viewshed": {}, "water_viewing": {}},
        config_hash=config_hash,
        source_type="land",
        observer_height_class=None,
        run=SimpleNamespace(version="test"),
        h3=SimpleNamespace(
            source_resolution=8,
            output_resolution=8,
            source_sampling_mode=sampling_mode,
            sample_points_per_source_cell=10,
            min_sample_points_per_source_cell=1,
            aggregation_mode="full",
            pixel_stride=1,
        ),
        viewshed=SimpleNamespace(
            crs_projected="EPSG:32610",
            observer_eye_height_m=1.7,
            target_height_m=1.0,
            max_distance_m=15_000.0,
            dem_resolution_m=10,
            backend="gdal",
            surface_model="bare_earth",
            canopy_resampling="max",
            canopy_nodata_policy="error",
            minimum_canopy_height_m=0.0,
            observer_canopy_clearance_radius_m=0.0,
            curvature_coefficient=0.85714,
            earth_radius_m=6_378_137.0,
        ),
        paths=SimpleNamespace(canopy_height_path="CHM_10M.tif"),
    )


def test_partition_metadata_tracks_sampling_contract_and_model_hash() -> None:
    expected = expected_partition_metadata(_fake_app("hash-a"))
    same_model_new_full_hash = expected_partition_metadata(_fake_app("hash-b"))

    assert expected["source_sampling_mode"] == "active_fraction"
    assert expected["max_sample_points_per_source_cell"] == 10
    assert expected["min_sample_points_per_source_cell"] == 1
    assert (
        expected["source_sampling_algorithm_version"]
        == "component_aware_projected_nested_maximin_v4"
    )
    assert expected["source_sampling_projected_crs"] == "EPSG:32610"
    assert expected["source_sampling_candidate_grid_side"] == 32
    assert expected["source_sampling_max_design_points"] == 10
    assert expected["terrain_partition_schema_version"] == "adaptive_active_fraction_v8"
    assert _metadata_values_match(expected, expected)
    assert same_model_new_full_hash == expected

    stale_hash = dict(expected, config_hash="hash-b")
    stale_mode = dict(expected, source_sampling_mode="fixed")
    stale_max = dict(expected, max_sample_points_per_source_cell=9)
    stale_min = dict(expected, min_sample_points_per_source_cell=2)
    stale_algorithm = dict(
        expected, source_sampling_algorithm_version="adaptive_active_fraction_v0"
    )
    stale_projected_crs = dict(expected, source_sampling_projected_crs="EPSG:3857")
    stale_design_max = dict(expected, source_sampling_max_design_points=9)
    stale_candidate_grid = dict(expected, source_sampling_candidate_grid_side=16)
    assert not _metadata_values_match(stale_hash, expected)
    assert not _metadata_values_match(stale_mode, expected)
    assert not _metadata_values_match(stale_max, expected)
    assert not _metadata_values_match(stale_min, expected)
    assert not _metadata_values_match(stale_algorithm, expected)
    assert not _metadata_values_match(stale_projected_crs, expected)
    assert not _metadata_values_match(stale_design_max, expected)
    assert not _metadata_values_match(stale_candidate_grid, expected)


def test_terrain_partition_hash_ignores_map_and_publishing_settings() -> None:
    app = _fake_app("hash-a")
    app.raw_config = {
        **app.raw_config,
        "static_maps": {"display_quantile": 0.98},
        "publishing": {"viewability": {"parallel_workers": 8}},
        "paths": {"map_dir": "outputs/effort/viewshed"},
    }
    changed = _fake_app("hash-b")
    changed.raw_config = {
        **changed.raw_config,
        "static_maps": {"display_quantile": 0.75},
        "publishing": {"viewability": {"parallel_workers": 2}},
        "paths": {"map_dir": "outputs/another-map-root"},
    }

    assert _terrain_partition_config_hash(app) == _terrain_partition_config_hash(changed)
