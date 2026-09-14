from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
import yaml

from analysis.case_studies.orcacast.observation_contracts import validate_product_contract
from viewshed_toolkit.pipeline.contracts.artifacts import (
    FINAL_SCHEMAS,
    final_artifact_paths_from_raw,
)
from viewshed_toolkit.pipeline.finalize import final_artifacts
from viewshed_toolkit.pipeline.finalize.final_artifacts import (
    _effective_physical_assumptions,
    build_observation_geometry_lazy,
    build_static_viewability_lazy,
    materialize_static_viewability_outputs,
    materialize_vegetation_weights_from_mapping,
)
from viewshed_toolkit.pipeline.weights.vegetation.summarize import (
    build_water_neutral_vegetation_weights,
)


def _config(tmp_path: Path) -> tuple[Path, object]:
    raw = {
        "run": {"version": "test_v1"},
        "region": {
            "bbox_wgs84": {
                "min_lon": -124.0,
                "min_lat": 48.0,
                "max_lon": -123.0,
                "max_lat": 49.0,
            }
        },
        "h3": {"source_resolution": 8, "target_resolution": 8},
        "paths": {"output_dir": str(tmp_path / "viewshed")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config_path, final_artifact_paths_from_raw(raw, tmp_path)


def test_model_finalizer_requires_explicit_dense_terrain_zeroes(
    tmp_path: Path,
) -> None:
    config_path, paths = _config(tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    paths.land_weights_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "distance_km": [1.0, 2.0],
            "source_type": ["land", "land"],
        }
    ).write_parquet(paths.source_target_lookup)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "weight_terrain": [0.6, 0.0],
        }
    ).write_parquet(paths.terrain_weights)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "weight_distance": [1.0, 0.8],
        }
    ).write_parquet(paths.distance_weights)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "source_type": ["land", "land"],
            "weight_vegetation": [0.5, 1.0],
            "vegetation_status": ["computed", "computed"],
        }
    ).write_parquet(paths.vegetation_weights)

    frame, report = build_static_viewability_lazy(config_path, source_type="land")
    result = frame.collect().sort("target_h3")

    assert result.height == 2
    assert result["weight_static_viewability"].to_list() == pytest.approx([0.3, 0.0])
    assert report["terrain"]["missing_pair_count"] == 0


def test_model_finalizer_rejects_missing_terrain_pair(tmp_path: Path) -> None:
    config_path, paths = _config(tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    paths.land_weights_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "distance_km": [1.0, 2.0],
            "source_type": ["land", "land"],
        }
    ).write_parquet(paths.source_target_lookup)
    pl.DataFrame({"source_h3": ["s"], "target_h3": ["t1"], "weight_terrain": [0.6]}).write_parquet(
        paths.terrain_weights
    )
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "weight_distance": [1.0, 0.8],
        }
    ).write_parquet(paths.distance_weights)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "source_type": ["land", "land"],
            "weight_vegetation": [0.5, 1.0],
            "vegetation_status": ["computed", "computed"],
        }
    ).write_parquet(paths.vegetation_weights)

    with pytest.raises(ValueError, match="land terrain.*exact coverage"):
        build_static_viewability_lazy(config_path, source_type="land")


def test_model_finalizer_rejects_missing_exact_factor(tmp_path: Path) -> None:
    config_path, paths = _config(tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    paths.land_weights_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "distance_km": [1.0, 2.0],
            "source_type": ["land", "land"],
        }
    ).write_parquet(paths.source_target_lookup)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "weight_terrain": [0.6, 0.0],
        }
    ).write_parquet(paths.terrain_weights)
    pl.DataFrame(
        {
            "source_h3": ["s", "s"],
            "target_h3": ["t1", "t2"],
            "weight_distance": [1.0, 0.8],
        }
    ).write_parquet(paths.distance_weights)
    pl.DataFrame(
        {
            "source_h3": ["s"],
            "target_h3": ["t1"],
            "source_type": ["land"],
            "weight_vegetation": [1.0],
            "vegetation_status": ["computed"],
        }
    ).write_parquet(paths.vegetation_weights)

    with pytest.raises(ValueError, match="land vegetation.*exact coverage"):
        build_static_viewability_lazy(config_path, source_type="land")


def test_forward_geometry_preserves_legacy_limitation_without_inventing_pure_los(
    tmp_path: Path,
) -> None:
    config_path, paths = _config(tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    paths.ocean_weights_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["water-source"],
            "target_h3": ["target"],
            "distance_km": [4.0],
            "source_type": ["water"],
        }
    ).write_parquet(paths.source_target_lookup)
    pl.DataFrame(
        {
            "source_h3": ["water-source"],
            "target_h3": ["target"],
            "terrain_binary": [False],
            "aggregation_method": ["legacy_fixture"],
            "unweighted_los_observed": [False],
            "any_observer_support_fraction": [0.5],
            "union_visible_target_fraction": [0.5],
            "joint_los_fraction": [0.5],
            "distance_weighted_los_fraction": [0.4],
            "los_distance_weight_sum": [0.4],
            "sample_points_requested": [1],
            "sample_points_actual": [1],
            "visible_sampled_pixel_count": [1],
            "visible_observer_pixel_count_sum": [1],
            "n_observers": [1],
            "target_water_pixel_count": [1],
            "target_water_sample_count": [1],
            "pixel_stride": [1],
            "visible_area_km2": [1.0],
            "target_water_area_km2": [1.0],
            "weight_terrain": [0.4],
        }
    ).write_parquet(paths.ocean_source_target_clear_sky)
    pl.DataFrame(
        {"source_h3": ["water-source"], "target_h3": ["target"], "weight_distance": [0.8]}
    ).write_parquet(paths.ocean_distance_weights)
    pl.DataFrame(
        {
            "source_h3": ["water-source"],
            "target_h3": ["target"],
            "source_type": ["water"],
            "weight_vegetation": [1.0],
            "vegetation_status": ["not_applicable"],
        }
    ).write_parquet(paths.ocean_vegetation_weights)
    lineage = {
        "GENERATION_ID": "fixture",
        "CONFIG_HASH": "a" * 64,
        "SOURCE_HASHES_JSON": "{}",
        "KNOWLEDGE_TIME_UTC": "2025-01-01T00:00:00+00:00",
        "SOURCE_VINTAGES_JSON": "{}",
        "HISTORICAL_RECONSTRUCTION": False,
    }
    lazy, _coverage = build_observation_geometry_lazy(
        config_path,
        source_type="water",
        lineage=lineage,
        component_provenance_json="{}",
    )
    result = lazy.collect()
    validate_product_contract(result, "static_geometry_r7")
    assert result["line_of_sight_support"].item() is None
    assert result["physical_viewability"].item() is None
    assert result["line_of_sight_state"].item() == "source_unavailable"
    assert result["vegetation_state"].item() == "not_applicable"
    assert result["distance_adjusted_viewability"].item() == pytest.approx(0.4)
    assert result["legacy_static_schema"].item()


def test_static_assumptions_are_source_specific() -> None:
    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text())
    land = _effective_physical_assumptions(raw, source_type="land")
    water = _effective_physical_assumptions(raw, source_type="water")
    assert land["observer_height_m"] == 1.7
    assert land["target_height_m"] == 1.0
    assert land["dem_applicable"] is True
    assert water["observer_height_m"] == 2.5
    assert water["target_height_m"] == 1.5
    assert water["dem_applicable"] is False
    assert water["canopy_applicable"] is False
    assert "dem_resolution_m" not in water
    assert water["h3"]["source_samples_per_cell"] == 3


def test_paired_static_promotion_rolls_back_artifacts_and_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, paths = _config(tmp_path)
    paths.source_target_lookup.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["land-source", "water-source"],
            "target_h3": ["target", "target"],
            "distance_km": [1.0, 1.0],
            "source_type": ["land", "water"],
        }
    ).write_parquet(paths.source_target_lookup)
    for source_type in ("land", "water"):
        weight_dir = paths.land_weights_dir if source_type == "land" else paths.ocean_weights_dir
        weight_dir.mkdir(parents=True, exist_ok=True)
        source_h3 = f"{source_type}-source"
        pl.DataFrame(
            {"source_h3": [source_h3], "target_h3": ["target"], "weight_terrain": [0.5]}
        ).write_parquet(paths.weights_path("terrain_weights", source_type=source_type))
        pl.DataFrame(
            {"source_h3": [source_h3], "target_h3": ["target"], "weight_distance": [0.8]}
        ).write_parquet(paths.weights_path("distance_weights", source_type=source_type))
        pl.DataFrame(
            {
                "source_h3": [source_h3],
                "target_h3": ["target"],
                "source_type": [source_type],
                "weight_vegetation": [1.0],
                "vegetation_status": ["computed" if source_type == "land" else "not_applicable"],
            }
        ).write_parquet(paths.weights_path("vegetation_weights", source_type=source_type))

    originals = {}
    for output in (paths.land_static_weights, paths.water_static_weights):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"old:{output.name}".encode())
        sidecar = output.with_name(f"{output.stem}_metadata.json")
        sidecar.write_text(f"old-sidecar:{output.name}")
        originals[output] = output.read_bytes()
        originals[sidecar] = sidecar.read_bytes()

    real_writer = final_artifacts._write_static_artifact_metadata

    def fail_water_sidecar(output_path, **kwargs):
        if kwargs["source_type"] == "water":
            raise RuntimeError("simulated water sidecar failure")
        return real_writer(output_path, **kwargs)

    monkeypatch.setattr(final_artifacts, "_write_static_artifact_metadata", fail_water_sidecar)

    with pytest.raises(RuntimeError, match="simulated water sidecar failure"):
        materialize_static_viewability_outputs(config_path, overwrite=True)

    for path, content in originals.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("bad_value", [None, "bad", float("nan"), float("inf"), -0.1, 1.1])
def test_land_vegetation_materializer_rejects_invalid_weights(
    tmp_path: Path, bad_value: object
) -> None:
    _, paths = _config(tmp_path)
    input_path = tmp_path / "vegetation_input.parquet"
    pl.DataFrame(
        {
            "source_h3": ["s"],
            "target_h3": ["t"],
            "weight_vegetation": [bad_value],
        }
    ).write_parquet(input_path)
    raw = {
        "h3": {"source_resolution": 8},
        "paths": {"output_dir": str(paths.output_dir)},
    }

    with pytest.raises(ValueError, match="land vegetation contains"):
        materialize_vegetation_weights_from_mapping(
            [input_path],
            raw,
            config_dir=tmp_path,
            overwrite=True,
            source_type="land",
        )


def test_land_vegetation_materializer_rejects_failed_status(tmp_path: Path) -> None:
    _, paths = _config(tmp_path)
    input_path = tmp_path / "vegetation_input.parquet"
    pl.DataFrame(
        {
            "source_h3": ["s"],
            "target_h3": ["t"],
            "source_type": ["land"],
            "weight_vegetation": [None],
            "vegetation_status": ["failed"],
        },
        schema_overrides={"weight_vegetation": pl.Float32},
    ).write_parquet(input_path)
    raw = {
        "h3": {"source_resolution": 8},
        "paths": {"output_dir": str(paths.output_dir)},
    }

    with pytest.raises(ValueError, match="vegetation_status='computed'"):
        materialize_vegetation_weights_from_mapping(
            [input_path],
            raw,
            config_dir=tmp_path,
            overwrite=True,
            source_type="land",
        )


def test_water_vegetation_is_explicitly_neutral_and_not_applicable(
    tmp_path: Path,
) -> None:
    config_path, paths = _config(tmp_path)
    paths.ocean_weights_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "source_h3": ["water-source"],
            "target_h3": ["target"],
            "weight_terrain": [0.75],
        }
    ).write_parquet(paths.ocean_terrain_weights)

    build_water_neutral_vegetation_weights(config_path, overwrite=True)

    result = pl.read_parquet(paths.ocean_vegetation_weights)
    assert result.columns == list(FINAL_SCHEMAS["vegetation_weights"])
    assert result.to_dicts() == [
        {
            "source_h3": "water-source",
            "target_h3": "target",
            "source_type": "water",
            "weight_vegetation": 1.0,
            "vegetation_status": "not_applicable",
        }
    ]
