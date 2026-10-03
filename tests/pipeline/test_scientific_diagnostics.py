"""Independent tiny numerical oracles for directly measured LOS diagnostics."""


import numpy as np
import pandas as pd
import polars as pl
import pytest
import xarray as xr

from tests.pipeline.test_review_regressions import composition_fixture
from viewshed_toolkit.pipeline.weights.canopy_visibility import compose_dual_surface_artifacts
from viewshed_toolkit.pipeline.weights.terrain.gdal import _terrain_weight_partition_for_storage
from viewshed_toolkit.pipeline.weights.terrain.los import window_offsets_in_full_grid


@pytest.mark.parametrize("canopy_los", [[1, 0], [0, 1], [0, 0], [1, 1]])
def test_unweighted_canopy_is_not_weighted_retention(tmp_path, canopy_los):
    distances = np.array([0.9, 0.1])
    kwargs = composition_fixture(tmp_path)
    bare_j, canopy_j = 1.0, float(np.mean(canopy_los))
    bare_k = float(np.mean(distances))
    canopy_k = float(np.mean(np.array(canopy_los) * distances))
    for path, j, k in [
        (kwargs["bare_earth_partition_paths"][0], bare_j, bare_k),
        (kwargs["canopy_partition_paths"][0], canopy_j, canopy_k),
    ]:
        pl.DataFrame(
            {
                "source_h3": ["s"],
                "target_h3": ["t"],
                "weight_terrain": [k],
                "joint_los_fraction": [j],
            }
        ).write_parquet(path)
    result = compose_dual_surface_artifacts(**kwargs)
    row = pl.read_parquet(result.dual_surface_factors).row(0, named=True)
    assert row["bare_los_fraction"] == pytest.approx(bare_j)
    assert row["canopy_los_fraction"] == pytest.approx(canopy_j)
    assert row["weight_canopy_los_raw"] == pytest.approx(canopy_k)
    assert row["weight_vegetation"] == pytest.approx(canopy_k / bare_k)
    assert row["weight_terrain"] * row["weight_vegetation"] == pytest.approx(canopy_k)
    assert 0 <= row["weight_canopy_los_raw"] <= row["canopy_los_fraction"] <= 1


@pytest.mark.parametrize("value", [None, "bad", np.nan, np.inf, -0.1, 1.1])
def test_observed_invalid_diagnostics_fail_before_storage_coercion(value):
    frame = pd.DataFrame(
        {
            "source_h3_cell": ["s"],
            "target_h3_cell": ["t"],
            "distance_weighted_los_fraction": [0.2],
            "joint_los_fraction": [value],
        }
    )
    with pytest.raises(ValueError, match="diagnostic"):
        _terrain_weight_partition_for_storage(frame)


def test_legacy_missing_diagnostic_remains_unavailable():
    frame = pd.DataFrame(
        {"source_h3_cell": ["s"], "target_h3_cell": ["t"], "distance_weighted_los_fraction": [0.2]}
    )
    row = _terrain_weight_partition_for_storage(frame).iloc[0]
    assert pd.isna(row["joint_los_fraction"])
    assert not row["unweighted_los_observed"]


@pytest.mark.parametrize("step", [-30.0, 30.0])
def test_large_projected_northings_match_exact_grid_row(step):
    y = 5_400_000.0 + np.arange(8) * step
    full = xr.DataArray(
        np.zeros((8, 8)), dims=("y", "x"), coords={"y": y, "x": 500000.0 + np.arange(8) * 30.0}
    )
    window = full.isel(y=slice(3, 6), x=slice(2, 5))
    assert window_offsets_in_full_grid(window_da=window, full_da=full) == (3, 2)
    shifted = window.assign_coords(y=window.y + 15.0)
    with pytest.raises(ValueError, match="align"):
        window_offsets_in_full_grid(window_da=shifted, full_da=full)


def test_window_rejects_later_shifted_coordinate():
    full = xr.DataArray(
        np.zeros((8, 8)),
        dims=("y", "x"),
        coords={"y": 5400000.0 + np.arange(8) * 30.0, "x": 500000.0 + np.arange(8) * 30.0},
    )
    window = full.isel(y=slice(2, 5), x=slice(2, 5))
    shifted = window.assign_coords(y=[5400060.0, 5400090.0, 5400135.0])
    with pytest.raises(ValueError, match="align"):
        window_offsets_in_full_grid(window_da=shifted, full_da=full)


@pytest.mark.parametrize(
    "bare_k,canopy_k,bare_j,canopy_j",
    [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.5), (1e-20, 5e-21, 1.0, 0.5), (0.2, 0.3, 0.8, 0.8)],
)
def test_zero_tiny_and_raw_canopy_excess_remain_inspectable(
    tmp_path, bare_k, canopy_k, bare_j, canopy_j
):
    kwargs = composition_fixture(tmp_path)
    for path, j, k in [
        (kwargs["bare_earth_partition_paths"][0], bare_j, bare_k),
        (kwargs["canopy_partition_paths"][0], canopy_j, canopy_k),
    ]:
        pl.DataFrame(
            {
                "source_h3": ["s"],
                "target_h3": ["t"],
                "weight_terrain": [k],
                "joint_los_fraction": [j],
            }
        ).write_parquet(path)
    result = compose_dual_surface_artifacts(**kwargs)
    row = pl.read_parquet(result.dual_surface_factors).row(0, named=True)
    assert row["weight_canopy_los_raw"] == pytest.approx(canopy_k)
    assert row["canopy_los_fraction"] == pytest.approx(canopy_j)
    assert row["weight_vegetation"] == pytest.approx(
        min(canopy_k, bare_k) / bare_k if bare_k > 0 else 1.0
    )
    assert result.diagnostics["substantial_canopy_excess_pair_count"] == int(
        canopy_k - bare_k > 1e-6
    )


def test_geometry_uses_direct_canopy_los_not_weighted_ratio(tmp_path):
    import yaml
    from viewshed_toolkit.pipeline.finalize.final_artifacts import build_observation_geometry_lazy

    kwargs = composition_fixture(tmp_path)
    paths = kwargs["paths"]
    lookup = pl.read_parquet(paths.source_target_lookup).with_columns(
        pl.lit(1.0).alias("distance_km")
    )
    lookup.write_parquet(paths.source_target_lookup)
    for path, j, k in [
        (kwargs["bare_earth_partition_paths"][0], 1.0, 0.5),
        (kwargs["canopy_partition_paths"][0], 0.5, 0.45),
    ]:
        pl.DataFrame(
            {
                "source_h3": ["s"],
                "target_h3": ["t"],
                "weight_terrain": [k],
                "joint_los_fraction": [j],
            }
        ).write_parquet(path)
    compose_dual_surface_artifacts(**kwargs)
    pl.DataFrame(
        {
            "source_h3": ["s"],
            "target_h3": ["t"],
            "unweighted_los_observed": [True],
            "joint_los_fraction": [1.0],
            "distance_weighted_los_fraction": [0.5],
        }
    ).write_parquet(paths.source_target_clear_sky)
    pl.DataFrame({"source_h3": ["s"], "target_h3": ["t"], "weight_distance": [0.2]}).write_parquet(
        paths.distance_weights
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(kwargs["raw_config"]))
    lineage = {
        "GENERATION_ID": "test",
        "CONFIG_HASH": "test",
        "SOURCE_HASHES_JSON": "{}",
        "KNOWLEDGE_TIME_UTC": "2026-01-01T00:00:00Z",
        "SOURCE_VINTAGES_JSON": "{}",
        "HISTORICAL_RECONSTRUCTION": False,
    }
    frame, _ = build_observation_geometry_lazy(
        config, source_type="land", lineage=lineage, component_provenance_json="{}"
    )
    row = frame.collect().row(0, named=True)
    assert row["line_of_sight_support"] == 1.0
    assert row["physical_viewability"] == pytest.approx(0.5)
    assert row["vegetation_attenuation"] == pytest.approx(0.9)
    assert row["distance_adjusted_viewability"] == pytest.approx(0.45)
