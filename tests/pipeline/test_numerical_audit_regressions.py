"""Regression cases from the 2026-11-04 bounded numerical audit."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import polars as pl
import pytest
import yaml
from shapely.geometry import MultiPolygon, Point, box

from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.prepare.area.sampling import sample_points_in_source_geometry
from viewshed_toolkit.pipeline.weights.canopy_visibility import (
    make_terrain_variant_app,
    run_dual_surface_canopy_weights,
)
from viewshed_toolkit.pipeline.weights.terrain import gdal, los


def _config_path(tmp_path: Path, **viewshed_overrides) -> Path:
    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    raw["viewshed"].update(viewshed_overrides)
    raw["paths"].update(
        water_polygon_path=str(tmp_path / "water.parquet"),
        output_dir=str(tmp_path / "work"),
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("dem_resolution_m", 0),
        ("dem_resolution_m", 0.5),
        ("dem_resolution_m", float("nan")),
        ("max_distance_m", -1),
        ("max_distance_m", float("inf")),
        ("earth_radius_m", 0),
        ("observer_eye_height_m", -1),
        ("target_height_m", float("nan")),
        ("aoi_margin_m", -1),
        ("minimum_canopy_height_m", -1),
        ("observer_canopy_clearance_radius_m", float("inf")),
        ("curvature_coefficient", float("nan")),
        ("sea_level_m", float("inf")),
        ("crs_projected", "EPSG:4326"),
        ("crs_projected", "EPSG:2263"),
        ("crs_projected", "not-a-crs"),
    ],
)
def test_invalid_metric_config_fails_before_runtime_paths(tmp_path, name, value):
    config = _config_path(tmp_path, **{name: value})
    with pytest.raises(ValueError, match=f"viewshed.{name}"):
        load_app_config(config)
    assert not (tmp_path / "work").exists()


def test_metric_config_allows_zero_heights_flat_earth_and_negative_sea_level(tmp_path):
    config = _config_path(
        tmp_path,
        observer_eye_height_m=0,
        target_height_m=0,
        curvature_coefficient=0,
        aoi_margin_m=0,
        sea_level_m=-2,
    )
    app = load_app_config(config)
    assert app.viewshed.target_height_m == 0
    assert app.viewshed.sea_level_m == -2


def test_xrspatial_canopy_rejected_by_loader_and_variant_before_work(tmp_path):
    config = _config_path(tmp_path, backend="xrspatial", surface_model="canopy")
    with pytest.raises(ValueError, match="does not support grounded canopy"):
        load_app_config(config)
    config = _config_path(tmp_path, backend="xrspatial", surface_model="bare_earth")
    app = load_app_config(config)
    with pytest.raises(ValueError, match="does not support grounded canopy"):
        make_terrain_variant_app(app, surface_model="canopy", terrain_dir=tmp_path / "variant")
    with pytest.raises(ValueError, match="does not support grounded canopy"):
        run_dual_surface_canopy_weights(config)
    assert not (tmp_path / "variant").exists()
    assert not app.paths.output_dir.exists()


def test_xrspatial_canopy_dispatch_rejects_direct_dataclass_bypass(monkeypatch):
    def unexpected_backend(**kwargs):
        pytest.fail("Unsupported canopy reached the xrspatial backend")

    monkeypatch.setattr(los, "run_xrspatial_viewshed_to_bool_array", unexpected_backend)
    with pytest.raises(ValueError, match="does not support grounded canopy"):
        los.run_viewshed_backend_to_bool_array(
            backend="xrspatial",
            context=None,
            xrspatial_dem_da=None,
            observer_x=0,
            observer_y=0,
            observer_height_m=1.7,
            target_height_m=1,
            max_distance_m=1000,
            curvature_coefficient=0,
            earth_radius_m=6378137,
            app=SimpleNamespace(viewshed=SimpleNamespace(surface_model="canopy")),
            source_cell="s",
            sample_index=1,
            observer_id="o",
        )


@pytest.mark.parametrize("spacing", [1.0, 10.0, 30.0])
def test_xrspatial_offsets_are_exact_at_real_projected_coordinates(spacing):
    coordinates = 5_400_000.0 - np.arange(10) * spacing
    assert los._coord_start_index(coordinates, coordinates[3]) == 3
    assert los._coord_start_index(coordinates, coordinates[3] + 1e-8) == 3
    with pytest.raises(ValueError, match="uniquely align"):
        los._coord_start_index(coordinates, coordinates[3] + spacing / 2)
    with pytest.raises(ValueError, match="uniquely align"):
        los._coord_start_index(np.array([coordinates[3], coordinates[3]]), coordinates[3])


@pytest.mark.parametrize("requested", [1, 2, 5])
def test_multipart_order_preserves_same_nested_observer_design(requested):
    first = box(500000, 5400000, 500100, 5400100)
    second = box(500200, 5400000, 500300, 5400100)
    designs = [
        sample_points_in_source_geometry(
            "source",
            MultiPolygon(parts),
            requested,
            geometry_crs="EPSG:32610",
            projected_crs="EPSG:32610",
            max_design_points=5,
        )
        for parts in ([first, second], [second, first])
    ]
    np.testing.assert_array_equal(
        designs[0][["lon", "lat"]].to_numpy(), designs[1][["lon", "lat"]].to_numpy()
    )
    full = sample_points_in_source_geometry(
        "source",
        MultiPolygon([first, second]),
        5,
        geometry_crs="EPSG:32610",
        projected_crs="EPSG:32610",
        max_design_points=5,
    )
    np.testing.assert_array_equal(
        designs[0][["lon", "lat"]].to_numpy(), full[["lon", "lat"]].to_numpy()[:requested]
    )


@pytest.mark.parametrize("cutoff_km", [1.0, 5.0])
def test_water_sample_range_keeps_full_denominator_and_exact_boundary(monkeypatch, cutoff_km):
    app = SimpleNamespace(
        raw_config={
            "water_viewing": {"land_mask": {"land_buffer_m": 0, "target_samples_per_cell": 3}}
        },
        viewshed=SimpleNamespace(
            observer_eye_height_m=1.7,
            target_height_m=1,
            curvature_coefficient=0,
            earth_radius_m=6378137,
            max_distance_m=1000,
        ),
    )
    monkeypatch.setattr(gdal, "_projected_h3_bounds_for_app", lambda app: None)
    monkeypatch.setattr(gdal, "_target_entirely_beyond_horizon", lambda *a, **kw: False)
    monkeypatch.setattr(
        gdal,
        "_projected_water_target_samples_for_app",
        lambda *a, **kw: (Point(500, 0), Point(1000, 0), Point(1500, 0)),
    )
    monkeypatch.setattr(
        gdal, "_projected_land_exclusion_for_app", lambda *a, **kw: box(10000, 10000, 11000, 11000)
    )
    result = gdal._water_land_mask_supports_for_targets(
        app,
        target_cells=["target"],
        projected_source_points=[Point(0, 0)],
        domain_geometries=SimpleNamespace(water_domain=box(-1, -1, 1, 1)),
        distance_weight_config=DistanceWeightConfig(
            selected_model="piecewise", piecewise_near_km=2, piecewise_far_km=5
        ),
        distance_weight_max_km=cutoff_km,
        target_area_lookup={"target": 10000},
    )["target"]
    assert result["target_water_sample_count"] == 3
    assert result["visible_observer_pixel_count_sum"] == 2
    assert result["joint_los_fraction"] == pytest.approx(2 / 3)
    assert result["distance_weighted_los_fraction"] == pytest.approx(2 / 3)


def test_land_partition_reuse_tracks_lookup_content_and_ignores_only_worker_count(tmp_path):
    app = load_app_config(_config_path(tmp_path))
    lookup = gdal._area_lookup_path_for_app(app, require_exists=False)
    lookup.parent.mkdir(parents=True)
    one = pl.DataFrame({"source_h3": ["s"], "target_h3": ["t1"], "source_type": ["land"]})
    one.write_parquet(lookup)
    initial = gdal.expected_partition_metadata(app)
    partition = tmp_path / "source.parquet"
    pd.DataFrame({"source_h3": ["s"], "target_h3": ["t1"], "weight_terrain": [0.5]}).to_parquet(
        partition
    )
    gdal._write_partition_metadata_sidecar(partition, initial)
    assert gdal.partition_metadata_matches(partition, initial)

    raw = deepcopy(app.raw_config)
    raw["batch"]["max_workers"] = 1
    one_worker = replace(app, raw_config=raw, batch=replace(app.batch, max_workers=1))
    assert gdal.expected_partition_metadata(one_worker) == initial
    assert gdal.partition_metadata_matches(partition, gdal.expected_partition_metadata(one_worker))
    raw = deepcopy(raw)
    raw["batch"]["batch_size_cells"] += 1
    assert gdal.expected_partition_metadata(replace(app, raw_config=raw)) != initial

    pl.concat([one, one.with_columns(pl.lit("t2").alias("target_h3"))]).write_parquet(lookup)
    changed = gdal.expected_partition_metadata(app)
    assert changed != initial
    assert not gdal.partition_metadata_matches(partition, changed)


def test_worker_tuning_reuses_partitions_and_native_rebuild_is_identical(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal")
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit import run_component_stage, run_components
    from viewshed_toolkit.pipeline.config import stable_config_hash
    from viewshed_toolkit.pipeline.weights.terrain import batches

    config = coastal_fixture(tmp_path)
    outputs = run_components(config, target="dem", run_id="one-worker")
    serial = pl.read_parquet(outputs["build-dem-weights"]).sort("source_h3", "target_h3")
    app = load_app_config(config)
    raw = deepcopy(app.raw_config)
    raw["batch"]["max_workers"] = 4
    tuned = replace(
        app,
        raw_config=raw,
        config_hash=stable_config_hash(raw),
        batch=replace(app.batch, max_workers=4),
    )

    def unexpected_los(**kwargs):
        pytest.fail("Changing only worker count recomputed a valid LOS partition")

    with monkeypatch.context() as reuse:
        reuse.setattr(batches, "run_viewshed_backend_to_bool_array", unexpected_los)
        run_component_stage(tuned, "build-dem-weights")
    rebuilt_path = run_component_stage(tuned, "build-dem-weights", overwrite=True)
    parallel = pl.read_parquet(rebuilt_path).sort("source_h3", "target_h3")
    assert serial.equals(parallel)


def test_land_raster_algorithm_change_invalidates_partitions_and_components(tmp_path, monkeypatch):
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.config import apply_source_type_policy
    from viewshed_toolkit.pipeline.prepare.area import raster_stack
    from viewshed_toolkit.pipeline.weights.components import _surface_contract
    from viewshed_toolkit import run_component_stage

    config = coastal_fixture(tmp_path)
    run_component_stage(config, "build-source-target-lookup")
    run_component_stage(config, "download-dem")
    run_component_stage(config, "prepare-dem")
    app = load_app_config(config)
    land = gdal.expected_partition_metadata(app)
    water_app = apply_source_type_policy(app, "water")
    water = gdal.expected_partition_metadata(water_app)
    component = _surface_contract(app, "land")
    partition = tmp_path / "legacy-partition.parquet"
    partition.write_bytes(b"data")
    legacy = dict(land)
    legacy.pop("canonical_raster_stack_algorithm_version")
    gdal._write_partition_metadata_sidecar(partition, legacy)
    assert not gdal.partition_metadata_matches(partition, land)
    gdal._write_partition_metadata_sidecar(partition, land)
    assert gdal.partition_metadata_matches(partition, land)
    monkeypatch.setattr(raster_stack, "CANONICAL_RASTER_STACK_ALGORITHM_VERSION", "next")
    assert not gdal.partition_metadata_matches(partition, gdal.expected_partition_metadata(app))
    assert _surface_contract(app, "land") != component
    assert gdal.expected_partition_metadata(water_app) == water

    from viewshed_toolkit.pipeline.prepare.area import geometry

    water_component = _surface_contract(water_app, "water")
    monkeypatch.setattr(geometry, "H3_GEOMETRY_ALGORITHM_VERSION", "next_geometry")
    assert gdal.expected_partition_metadata(water_app) != water
    assert _surface_contract(water_app, "water") != water_component
