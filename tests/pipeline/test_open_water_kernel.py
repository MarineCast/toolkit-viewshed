from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import h3
import numpy as np
import polars as pl
import pytest
from shapely.geometry import Point, box
from shapely.ops import transform as shapely_transform

from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.weights.terrain import gdal


def _app(*, curvature_coefficient: float) -> SimpleNamespace:
    return SimpleNamespace(
        config_path=Path("fixture.yaml"),
        config_hash="fixture",
        raw_config={
            "water_viewing": {
                "land_mask": {
                    "land_buffer_m": 0.0,
                    "target_samples_per_cell": 2,
                }
            }
        },
        viewshed=SimpleNamespace(
            observer_eye_height_m=1.7,
            target_height_m=1.0,
            curvature_coefficient=curvature_coefficient,
            earth_radius_m=6_371_008.8,
            crs_projected="EPSG:3857",
        ),
    )


def _kernel(
    app: SimpleNamespace,
    *,
    source_points_wgs84: list[Point],
    domain_geometries: SimpleNamespace,
    distance_weight_config: DistanceWeightConfig,
    target_water_area_m2: float,
) -> dict[str, object]:
    transformer = gdal._projected_transformer_for_app(app)
    projected_sources = [
        shapely_transform(transformer.transform, point) for point in source_points_wgs84
    ]
    return gdal._water_land_mask_supports_for_targets(
        app,
        target_cells=["target"],
        projected_source_points=projected_sources,
        domain_geometries=domain_geometries,
        distance_weight_config=distance_weight_config,
        distance_weight_max_km=30.0,
        target_area_lookup={"target": target_water_area_m2},
    )["target"]


def test_configured_two_ended_refracted_horizon_has_expected_distance() -> None:
    horizon_m = gdal.refracted_horizon_distance_m(
        2.5,
        1.5,
        curvature_coefficient=0.85714,
        earth_radius_m=6_378_137.0,
    )

    assert horizon_m == pytest.approx(10_824.45, abs=0.1)
    assert horizon_m < 30_000.0


def test_refracted_horizon_boundary_and_disabled_flat_earth_cases() -> None:
    configured = gdal.refracted_horizon_distance_m(
        2.5,
        1.5,
        curvature_coefficient=0.85714,
        earth_radius_m=6_378_137.0,
    )
    assert gdal.refracted_horizon_distance_m(
        2.5,
        1.5,
        curvature_coefficient=0.0,
        earth_radius_m=6_378_137.0,
    ) == float("inf")
    assert configured == pytest.approx(
        gdal.refracted_horizon_distance_m(
            2.5,
            1.5,
            curvature_coefficient=0.85714,
            earth_radius_m=6_378_137.0,
        )
    )


def test_refracted_horizon_zero_height_is_impossible_range() -> None:
    assert (
        gdal.refracted_horizon_distance_m(
            0.0,
            0.0,
            curvature_coefficient=0.85714,
            earth_radius_m=6_378_137.0,
        )
        == 0.0
    )


def test_open_water_kernel_persists_recomputable_sample_numerators(
    monkeypatch,
) -> None:
    target_points = [Point(0.01, 0.0), Point(0.02, 0.0)]
    monkeypatch.setattr(
        gdal,
        "_water_geometry_sample_points",
        lambda *_args, **_kwargs: target_points,
    )
    monkeypatch.setattr(gdal, "cell_to_polygon", lambda _cell: box(0.005, -0.01, 0.025, 0.01))
    monkeypatch.setattr(
        gdal,
        "_projected_land_domain_for_app",
        lambda *_args, **_kwargs: box(1_000_000.0, 1_000_000.0, 1_001_000.0, 1_001_000.0),
    )
    domains = SimpleNamespace(
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        land_domain=box(10.0, 10.0, 11.0, 11.0),
    )
    distance_cfg = DistanceWeightConfig(selected_model="exponential", exponential_lambda_km=8.0)

    kernel = _kernel(
        _app(curvature_coefficient=0.0),
        source_points_wgs84=[Point(0.0, 0.0), Point(0.0, 0.001)],
        domain_geometries=domains,
        distance_weight_config=distance_cfg,
        target_water_area_m2=4_000_000.0,
    )

    assert kernel is not None
    assert kernel["aggregation_method"] == "water_land_mask_sample_kernel_v1"
    assert kernel["n_observers"] == 2
    assert kernel["target_water_sample_count"] == 2
    assert kernel["visible_observer_pixel_count_sum"] == 4
    assert kernel["joint_los_fraction"] == pytest.approx(1.0)
    assert kernel["distance_weighted_los_fraction"] == pytest.approx(
        kernel["los_distance_weight_sum"] / 4.0
    )
    assert kernel["target_water_area_km2"] == pytest.approx(4.0)


def test_open_water_kernel_applies_horizon_to_each_sample(monkeypatch) -> None:
    monkeypatch.setattr(
        gdal,
        "_water_geometry_sample_points",
        lambda *_args, **_kwargs: [Point(0.2, 0.0)],
    )
    monkeypatch.setattr(gdal, "cell_to_polygon", lambda _cell: box(0.19, -0.01, 0.21, 0.01))
    monkeypatch.setattr(
        gdal,
        "_target_entirely_beyond_horizon",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        gdal,
        "_projected_land_domain_for_app",
        lambda *_args, **_kwargs: box(1_000_000.0, 1_000_000.0, 1_001_000.0, 1_001_000.0),
    )
    domains = SimpleNamespace(
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        land_domain=box(10.0, 10.0, 11.0, 11.0),
    )

    kernel = _kernel(
        _app(curvature_coefficient=0.85714),
        source_points_wgs84=[Point(0.0, 0.0)],
        domain_geometries=domains,
        distance_weight_config=DistanceWeightConfig(),
        target_water_area_m2=1_000_000.0,
    )

    assert kernel is not None
    assert kernel["joint_los_fraction"] == pytest.approx(0.0)
    assert kernel["distance_weighted_los_fraction"] == pytest.approx(0.0)
    assert kernel["visible_observer_pixel_count_sum"] == 0


def test_water_land_mask_kernel_blocks_land_obstructed_rays(monkeypatch) -> None:
    monkeypatch.setattr(
        gdal,
        "_water_geometry_sample_points",
        lambda *_args, **_kwargs: [Point(0.02, 0.0)],
    )
    monkeypatch.setattr(gdal, "cell_to_polygon", lambda _cell: box(0.01, -0.01, 0.03, 0.01))
    monkeypatch.setattr(
        gdal,
        "_projected_land_domain_for_app",
        lambda *_args, **_kwargs: box(900.0, -100.0, 1_100.0, 100.0),
    )
    domains = SimpleNamespace(
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        land_domain=box(10.0, 10.0, 11.0, 11.0),
    )

    kernel = _kernel(
        _app(curvature_coefficient=0.0),
        source_points_wgs84=[Point(0.0, 0.0)],
        domain_geometries=domains,
        distance_weight_config=DistanceWeightConfig(),
        target_water_area_m2=1_000_000.0,
    )

    assert kernel["terrain_visible_clear_sky"] is False
    assert kernel["joint_los_fraction"] == pytest.approx(0.0)
    assert kernel["distance_weighted_los_fraction"] == pytest.approx(0.0)


def test_water_land_mask_kernel_retains_clear_fraction_of_partially_blocked_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gdal,
        "_water_geometry_sample_points",
        lambda *_args, **_kwargs: [Point(0.02, 0.0), Point(0.02, 0.02)],
    )
    monkeypatch.setattr(
        gdal,
        "cell_to_polygon",
        lambda _cell: box(0.01, -0.01, 0.03, 0.03),
    )
    monkeypatch.setattr(
        gdal,
        "_projected_land_domain_for_app",
        lambda *_args, **_kwargs: box(900.0, -100.0, 1_100.0, 100.0),
    )
    domains = SimpleNamespace(
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        land_domain=box(10.0, 10.0, 11.0, 11.0),
    )

    kernel = _kernel(
        _app(curvature_coefficient=0.0),
        source_points_wgs84=[Point(0.0, 0.0)],
        domain_geometries=domains,
        distance_weight_config=DistanceWeightConfig(),
        target_water_area_m2=1_000_000.0,
    )

    assert kernel["joint_los_fraction"] == pytest.approx(0.5)
    assert kernel["union_visible_target_fraction"] == pytest.approx(0.5)
    assert 0.0 < kernel["distance_weighted_los_fraction"] < 0.5


def test_water_prefilter_uses_normalized_denominator_and_keeps_sparse_zeroes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lookup_path = tmp_path / "lookup.parquet"
    lookup_path.write_bytes(b"fixture")
    app = _app(curvature_coefficient=0.85714)
    app.source_type = "water"
    app.h3 = SimpleNamespace(output_resolution=8)
    app.viewshed.max_distance_m = 30_000.0

    monkeypatch.setattr(gdal, "_area_lookup_path_for_app", lambda _app: lookup_path)
    monkeypatch.setattr(
        gdal,
        "load_batch_lookup",
        lambda _app, _sources, **_kwargs: (
            {},
            {"source": {"zero": 20.0, "positive": 2.0, "blocked": 3.0}},
        ),
    )
    monkeypatch.setattr(
        gdal,
        "_water_terrain_domains_for_app",
        lambda _app: SimpleNamespace(land_domain=box(0, 0, 1, 1), water_domain=box(0, 0, 1, 1)),
    )
    monkeypatch.setattr(
        gdal.distance,
        "load_distance_weight_config",
        lambda _raw: DistanceWeightConfig(),
    )
    monkeypatch.setattr(
        gdal,
        "domain_target_water_area_by_h3",
        lambda *_args: pl.DataFrame(
            {
                "target_h3_cell": ["zero", "positive", "blocked"],
                "target_water_area_m2": [100.0, 200.0, 300.0],
            }
        ),
    )

    observed_areas: dict[str, float] = {}

    def fake_kernels(_app, *, target_cells, target_area_lookup, **_kwargs):
        for target_cell in target_cells:
            observed_areas[target_cell] = target_area_lookup[target_cell]
        return {
            target_cell: {
                "terrain_visibility_support": np.float32(
                    0.0 if target_cell in {"zero", "blocked"} else 0.25
                )
            }
            for target_cell in target_cells
        }

    monkeypatch.setattr(gdal, "_water_land_mask_supports_for_targets", fake_kernels)
    gdal._WATER_TERRAIN_PREFILTER_CACHE.clear()

    rows, dem_targets = gdal._water_terrain_prefilter_for_source(
        app,
        "source",
        source_points_wgs84=[Point(0.0, 0.0)],
    )

    assert rows["target_h3_cell"].to_list() == ["positive"]
    assert dem_targets == set()
    assert observed_areas == {"blocked": 300.0, "positive": 200.0, "zero": 100.0}

    cached_rows, cached_dem_targets = gdal._water_terrain_prefilter_for_source(
        app,
        "source",
        source_points_wgs84=[Point(0.0, 0.0)],
        target_distances_override={"zero": 20.0, "positive": 2.0, "blocked": 3.0},
    )
    assert cached_rows["target_h3_cell"].to_list() == ["positive"]
    assert cached_dem_targets == set()
    assert observed_areas == {"blocked": 300.0, "positive": 200.0, "zero": 100.0}


def test_water_prefilter_skips_cell_proven_wholly_beyond_horizon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lookup_path = tmp_path / "lookup.parquet"
    lookup_path.write_bytes(b"fixture")
    app = _app(curvature_coefficient=0.85714)
    app.source_type = "water"
    app.h3 = SimpleNamespace(output_resolution=8)
    app.viewshed.max_distance_m = 30_000.0
    target_cell = h3.latlng_to_cell(0.0, 0.2, 8)

    monkeypatch.setattr(gdal, "_area_lookup_path_for_app", lambda _app: lookup_path)
    monkeypatch.setattr(
        gdal,
        "_water_terrain_domains_for_app",
        lambda _app: SimpleNamespace(
            land_domain=box(10.0, 10.0, 11.0, 11.0),
            water_domain=box(-1.0, -1.0, 1.0, 1.0),
        ),
    )
    monkeypatch.setattr(
        gdal.distance,
        "load_distance_weight_config",
        lambda _raw: DistanceWeightConfig(),
    )
    monkeypatch.setattr(
        gdal,
        "domain_target_water_area_by_h3",
        lambda *_args: pl.DataFrame(
            {
                "target_h3_cell": [target_cell],
                "target_water_area_m2": [100.0],
            }
        ),
    )
    calls: list[str] = []

    def fail_if_called(_app, target_cell: str, **_kwargs):
        calls.append(str(target_cell))
        raise AssertionError("Far-horizon target should not enter the sampled kernel")

    monkeypatch.setattr(gdal, "_projected_water_target_samples_for_app", fail_if_called)
    gdal._WATER_TERRAIN_PREFILTER_CACHE.clear()
    gdal._PROJECTED_H3_BOUND_CACHE.clear()

    rows, dem_targets = gdal._water_terrain_prefilter_for_source(
        app,
        "source",
        source_points_wgs84=[Point(0.0, 0.0)],
        target_distances_override={target_cell: 22.0},
    )

    assert rows.empty
    assert dem_targets == set()
    assert calls == []


def test_open_water_kernel_reuses_projected_target_samples_across_sources(
    monkeypatch,
) -> None:
    sample_calls: list[str] = []

    def sampled(target_cell: str, *_args, **_kwargs):
        sample_calls.append(target_cell)
        return [Point(0.01, 0.0), Point(0.02, 0.0)]

    monkeypatch.setattr(gdal, "_water_geometry_sample_points", sampled)
    monkeypatch.setattr(
        gdal,
        "cell_to_polygon",
        lambda _cell: box(0.005, -0.01, 0.025, 0.01),
    )
    monkeypatch.setattr(
        gdal,
        "_projected_land_domain_for_app",
        lambda *_args, **_kwargs: box(1_000_000.0, 1_000_000.0, 1_001_000.0, 1_001_000.0),
    )
    domain = SimpleNamespace(
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        land_domain=box(10.0, 10.0, 11.0, 11.0),
    )
    app = _app(curvature_coefficient=0.0)
    gdal._PROJECTED_WATER_TARGET_SAMPLE_CACHE.clear()

    for source_cell, source_point in (
        ("source-a", Point(0.0, 0.0)),
        ("source-b", Point(0.0, 0.001)),
    ):
        kernel = _kernel(
            app,
            source_points_wgs84=[source_point],
            domain_geometries=domain,
            distance_weight_config=DistanceWeightConfig(),
            target_water_area_m2=100.0,
        )
        assert kernel is not None

    assert sample_calls == ["target"]


def test_cached_projected_water_geometry_samples_are_reprojected_for_metric_kernel(
    monkeypatch,
) -> None:
    """The sampler returns WGS84 even when its input geometry is projected."""

    app = _app(curvature_coefficient=0.0)
    projected_water = box(1_000.0, -100.0, 2_000.0, 100.0)
    monkeypatch.setattr(
        gdal,
        "_cached_h3_geometry_for_app",
        lambda _app, column: (
            {"target": projected_water} if column == "water_geometry_projected" else None
        ),
    )
    monkeypatch.setattr(
        gdal,
        "_water_geometry_sample_points",
        lambda *_args, **_kwargs: [Point(0.01, 0.0)],
    )
    gdal._PROJECTED_WATER_TARGET_SAMPLE_CACHE.clear()

    points = gdal._projected_water_target_samples_for_app(
        app,
        "target",
        water_domain=box(-1.0, -1.0, 1.0, 1.0),
        max_samples=1,
    )

    assert len(points) == 1
    assert points[0].x == pytest.approx(1_113.1949, rel=1e-6)
    assert points[0].y == pytest.approx(0.0, abs=1e-9)
