from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from viewshed_toolkit._internal.config.paths import project_root
from viewshed_toolkit.pipeline.config import (
    apply_source_type_policy,
    load_app_config,
)
from viewshed_toolkit.pipeline.config.paths import (
    dem_path_from_config,
    output_dir_from_config,
    raw_dem_dir_from_config,
    resolve_existing_or_relative_path,
    resolve_path,
)


@pytest.mark.parametrize(
    "config_path",
    [
        "configs/salish_sea.yaml",
    ],
)
def test_shipped_viewshed_configs_load_from_arbitrary_working_directory(
    config_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = load_app_config(config_path)
    monkeypatch.chdir(tmp_path)

    actual = load_app_config(config_path)

    assert actual.paths.water_polygon_path == expected.paths.water_polygon_path
    assert actual.paths.regional_dem_path == expected.paths.regional_dem_path
    assert actual.paths.output_dir == expected.paths.output_dir


def test_loading_config_does_not_create_runtime_directories(tmp_path: Path) -> None:
    root = project_root()
    water_path = (
        root / "data/processed/domain/environmental_layer/seascape/spatial_support/"
        "water_geometry/TERRITORIAL_WATER_POLYGON.parquet"
    )
    output_dir = tmp_path / "never-created-by-load"
    config = {
        "run": {"name": "pure-load", "version": "pure-load-v1"},
        "region": {
            "bbox_wgs84": {
                "min_lon": -124.0,
                "min_lat": 48.0,
                "max_lon": -123.0,
                "max_lat": 49.0,
            }
        },
        "viewshed": {"dem_resolution_m": 30},
        "h3": {"source_resolution": 8, "target_resolution": 8},
        "paths": {
            "water_polygon_path": str(water_path),
            "output_dir": str(output_dir),
            "final_visibility_path": str(output_dir / "terrain.parquet"),
            "partitioned_visibility_dir": str(output_dir / "partitions"),
            "manifest_path": str(output_dir / "manifest.csv"),
            "projected_dem_path": str(output_dir / "cache" / "dem.tif"),
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    app = load_app_config(config_path)

    assert app.paths.output_dir == output_dir
    assert not output_dir.exists()


def test_path_helpers_honor_config_without_mislabeling_dem_resolution(
    tmp_path: Path,
) -> None:
    raw = {
        "viewshed": {"dem_resolution_m": 30},
        "paths": {
            "regional_dem_path": str(tmp_path / "DEM_30M.tif"),
            "raw_dem_dir": str(tmp_path / "raw-dem"),
            "output_dir": str(tmp_path / "work"),
        },
    }

    assert dem_path_from_config(raw, tmp_path, 30) == tmp_path / "DEM_30M.tif"
    assert dem_path_from_config(raw, tmp_path, 10) != tmp_path / "DEM_30M.tif"
    assert dem_path_from_config(raw, tmp_path, 10).name == "DEM_10M.tif"
    assert raw_dem_dir_from_config(raw, tmp_path) == tmp_path / "raw-dem"
    assert output_dir_from_config(raw, tmp_path) == tmp_path / "work"
    assert not (tmp_path / "raw-dem").exists()
    assert not (tmp_path / "work").exists()


def test_legacy_config_relative_data_syntax_is_repo_root_relative() -> None:
    resolved = resolve_path("../data/example.parquet", project_root() / "config/data")

    assert resolved == (project_root() / "data/example.parquet").resolve()


def test_canonical_input_path_resolves_current_spatial_support_contract() -> None:
    canonical = Path(
        "data/processed/domain/environmental_layer/seascape/spatial_support/"
        "water_geometry/TERRITORIAL_WATER_POLYGON.parquet"
    )
    resolved = resolve_existing_or_relative_path(canonical, project_root())

    assert resolved == (project_root() / canonical).resolve()


def test_canonical_viewshed_config_uses_full_pixel_aggregation() -> None:
    app = load_app_config("configs/salish_sea.yaml")

    assert app.h3.source_sampling_mode == "active_fraction"
    assert app.h3.sample_points_per_source_cell == 10
    assert app.h3.min_sample_points_per_source_cell == 5
    assert app.h3.aggregation_mode == "full"
    assert app.h3.pixel_stride == 1


def test_water_source_policy_uses_compact_fixed_sampling_without_changing_land() -> None:
    base = load_app_config("configs/salish_sea.yaml")

    water = apply_source_type_policy(base, "water")
    land = apply_source_type_policy(base, "land")

    assert water.source_type == "water"
    assert water.h3.source_sampling_mode == "fixed"
    assert water.h3.sample_points_per_source_cell == 3
    assert water.h3.min_sample_points_per_source_cell == 3
    assert water.observer_height_class == "small_craft"
    assert water.viewshed.observer_eye_height_m == pytest.approx(2.5)
    assert water.viewshed.target_height_m == pytest.approx(1.5)
    assert land.h3 == base.h3
    assert land.viewshed == base.viewshed


def test_water_source_policy_rejects_unknown_height_class() -> None:
    base = load_app_config("configs/salish_sea.yaml")

    with pytest.raises(ValueError, match="default_observer_height_class"):
        apply_source_type_policy(
            replace(base, observer_height_class="not_configured"),
            "water",
        )


def test_config_rejects_observer_design_larger_than_exact_bitmask(tmp_path: Path) -> None:
    water_path = (
        project_root() / "data/processed/domain/environmental_layer/seascape/spatial_support/"
        "water_geometry/TERRITORIAL_WATER_POLYGON.parquet"
    )
    config = {
        "run": {"name": "too-many-observers", "version": "test"},
        "region": {
            "bbox_wgs84": {
                "min_lon": -124.0,
                "min_lat": 48.0,
                "max_lon": -123.0,
                "max_lat": 49.0,
            }
        },
        "viewshed": {"dem_resolution_m": 30},
        "h3": {
            "source_resolution": 8,
            "target_resolution": 8,
            "sample_points_per_source_cell": 64,
        },
        "paths": {
            "water_polygon_path": str(water_path),
            "output_dir": str(tmp_path / "output"),
        },
    }
    config_path = tmp_path / "too-many.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="must be <= 63"):
        load_app_config(config_path)
