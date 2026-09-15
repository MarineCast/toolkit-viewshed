"""Config-owned analysis directories and cross-region coast preparation."""

from pathlib import Path
from zipfile import ZipFile

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import box

from viewshed_toolkit import load_app_config, run_component_stage
from viewshed_toolkit.pipeline.config.case_study import CaseStudyConfig
from viewshed_toolkit.pipeline.prepare.area.case_study import prepare_case_geometry


def test_case_study_config_is_read_only_and_runtime_creates_directory(tmp_path):
    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    raw["case_study"] = {
        "name": "salish_sea",
        "analysis_root": str(tmp_path / "analysis"),
        "data_root": str(tmp_path / "data"),
    }
    raw["paths"]["final_output_dir"] = str(tmp_path / "data" / "salish_sea" / "outputs")
    config = tmp_path / "study.yaml"
    config.write_text(yaml.safe_dump(raw))
    load_app_config(config)
    assert not (tmp_path / "analysis").exists()
    run_component_stage(config, "resolve-area")
    assert (tmp_path / "analysis" / "salish_sea").is_dir()
    assert (tmp_path / "data" / "salish_sea").is_dir()
    with pytest.raises(ValueError):
        CaseStudyConfig(name="../outside")
    with pytest.raises(ValueError):
        CaseStudyConfig(name="salish_sea", unknown=True)


def test_global_land_clips_before_projection_and_water_is_cached(tmp_path):
    raw = yaml.safe_load(Path("configs/salish_sea_case_study.yaml").read_text())
    raw["case_study"]["data_root"] = str(tmp_path)
    raw["bbox_wgs84"] = dict(min_lon=-123, min_lat=48, max_lon=-122, max_lat=49)
    directory = tmp_path / "salish_sea" / "raw" / "land"
    directory.mkdir(parents=True)
    shape = directory / "ne_10m_land.shp"
    gpd.GeoDataFrame(geometry=[box(-170, -80, -122.5, 80)], crs=4326).to_file(shape)
    with ZipFile(directory / "ne_10m_land.zip", "w") as archive:
        for path in directory.glob("ne_10m_land.*"):
            if path.suffix != ".zip":
                archive.write(path, path.name)
    raw["paths"]["land_polygon_path"] = str(shape)
    raw["paths"]["water_polygon_path"] = str(tmp_path / "water.parquet")
    config = tmp_path / "study.yaml"
    config.write_text(yaml.safe_dump(raw))
    app = load_app_config(config)
    output = prepare_case_geometry(app)
    water = gpd.read_parquet(output)
    assert water.geometry.is_valid.all()
    assert not water.geometry.is_empty.any()
    modified = output.stat().st_mtime_ns
    assert prepare_case_geometry(app) == output
    assert output.stat().st_mtime_ns == modified


def test_aggregates_keep_denominators_and_conditional_canopy():
    import polars as pl

    from viewshed_toolkit.pipeline.finalize.aggregate import aggregate_frame

    frame = pl.DataFrame(
        {
            "source_h3": ["a", "b"],
            "target_h3": ["x", "x"],
            "weight_terrain": [0.0, 0.8],
            "weight_vegetation": [1.0, 0.25],
            "weight_distance": [0.4, 0.6],
            "weight_static_viewability": [0.0, 0.2],
        }
    )
    row = aggregate_frame(frame, "target_h3").row(0, named=True)
    assert row["candidate_pairs"] == 2
    assert row["positive_static_fraction"] == 0.5
    assert row["mean_weight_static_viewability"] == 0.1
    assert row["mean_canopy_given_terrain_support"] == 0.25
    empty_support = frame.with_columns(pl.lit(0.0).alias("weight_terrain"))
    assert (
        aggregate_frame(empty_support, "target_h3")["mean_canopy_given_terrain_support"][0] is None
    )


def test_vectorized_sampling_exactly_matches_scalar_reference():
    import json

    import numpy as np
    import shapely

    from viewshed_toolkit.pipeline.prepare.area.sampling import sample_points_in_source_geometry

    references = json.loads(Path("tests/fixtures/source_sampling_reference.json").read_text())
    for reference in references:
        result = sample_points_in_source_geometry(
            "reference",
            shapely.from_wkt(reference["geometry_wkt"]),
            10,
            include_centroid=reference["include_centroid"],
            geometry_crs=32610,
            projected_crs=32610,
            max_design_points=10,
        )
        np.testing.assert_array_equal(
            [[point.x, point.y] for point in result.geometry], reference["coordinates"]
        )


def test_relative_analysis_path_resolves_from_project_root():
    from viewshed_toolkit.pipeline.config.paths import REPO_ROOT, resolve_path

    study = CaseStudyConfig(name="salish_sea")
    assert (
        resolve_path(study.analysis_directory, REPO_ROOT / "configs")
        == REPO_ROOT / "analysis/salish_sea"
    )


def test_regional_validator_accepts_explicit_case_study_without_relabeling_bbox(tmp_path):
    from viewshed_toolkit.pipeline.api.regional import validate_region

    raw = yaml.safe_load(Path("configs/salish_sea_case_study.yaml").read_text())
    raw["paths"]["regional_dem_path"] = str(tmp_path / "absent.tif")
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(raw))
    report = validate_region(path, require_outputs=False)
    assert report["bbox"] == [-128.6, 46.0, -121.6, 51.1]
    assert not report["matches_canonical_model_area"]
    assert report["bbox_policy"] == "explicit_case_study"
    assert "DEM" in report["missing_inputs"]
