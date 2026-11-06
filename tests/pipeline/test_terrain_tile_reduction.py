from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest
import yaml
from affine import Affine

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import run_components
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.weights.terrain.accumulators import TiledAccumulator
from viewshed_toolkit.pipeline.weights.terrain.pixel_index import WaterPixelH3Index
from viewshed_toolkit.pipeline.weights.terrain.summarize import (
    DISTANCE_WEIGHT_SUM_SCALE,
    cumulative_visible_arrays_to_h3,
    grouped_visible_to_h3,
)


def reduction_fixture(tile_size=8, *, n_observers=4, distance_cutoff_km=30):
    rng = np.random.default_rng(307)
    shape = (45, 54)
    accumulator = TiledAccumulator(
        shape, row_offset=7, col_offset=9, n_observers=n_observers, tile_size=tile_size
    )
    transform = Affine(30, 0, 500_000, 0, -30, 5_400_000)
    for sample_index in range(1, n_observers + 1):
        visible = rng.random(shape) > 0.6
        visible[1, :] = True
        accumulator.accumulate(
            visible=visible,
            row_start=7,
            col_start=9,
            transform=transform,
            observer_x=500_400 + sample_index * 25,
            observer_y=5_399_500 - sample_index * 15,
            sample_index=sample_index,
            distance_weight_config=DistanceWeightConfig(),
            distance_weight_max_km=distance_cutoff_km,
            use_compiled=False,
        )
    codes = rng.integers(-1, 4, (70, 80), dtype="int32")
    index = WaterPixelH3Index(codes, tuple(f"target-{i}" for i in range(4)))
    area = pl.DataFrame(
        {
            "target_h3_cell": index.h3_cells,
            "target_water_pixel_count": [5000] * 4,
            "target_water_area_m2": [4_500_000.0] * 4,
            "target_water_area_km2": [4.5] * 4,
        }
    )
    return accumulator, index, area


def reference_result(accumulator, index, area, *, stride=1, allowed=None):
    sparse = accumulator.sparse_arrays(stride=stride)
    return cumulative_visible_arrays_to_h3(
        visible_count=sparse.visible_count,
        distance_weight_sum=sparse.distance_weight_sum,
        min_distance=sparse.min_distance,
        mean_distance=sparse.mean_distance,
        max_distance=sparse.max_distance,
        observer_mask=sparse.observer_mask,
        reference_raster_path=Path("unused.tif"),
        h3_resolution=7,
        target_water_area=area,
        pixel_stride=stride,
        aggregation_mode="full" if stride == 1 else "sampled",
        include_geometry=False,
        n_observer_points=accumulator.n_observers,
        sample_points_requested=accumulator.n_observers,
        sample_points_actual=accumulator.n_observers,
        allowed_target_h3=allowed,
        pixel_h3_index=index,
        reference_shape=index.code_grid.shape,
        reference_crs="EPSG:32610",
        pixel_area_m2=900,
        distance_weight_sum_scale=DISTANCE_WEIGHT_SUM_SCALE,
        sparse_global_rows=sparse.rows,
        sparse_global_cols=sparse.cols,
    )


def direct_result(accumulator, index, area, *, stride=1, allowed=None):
    grouped = accumulator.grouped_h3(index, stride=stride, allowed_target_h3=allowed)
    return grouped_visible_to_h3(
        grouped,
        h3_resolution=7,
        target_water_area=area,
        pixel_area_m2=900,
        stride=stride,
        aggregation_mode="full" if stride == 1 else "sampled",
        include_geometry=False,
        n_observer_points=accumulator.n_observers,
        sample_points_requested=accumulator.n_observers,
        sample_points_actual=accumulator.n_observers,
        allowed_target_h3=allowed,
    )


@pytest.mark.parametrize("stride", [1, 3])
@pytest.mark.parametrize("allowed", [None, {"target-0", "target-2"}, set()])
def test_tile_reduction_preserves_all_diagnostics_and_exact_physical_weights(stride, allowed):
    accumulator, index, area = reduction_fixture()
    expected = reference_result(accumulator, index, area, stride=stride, allowed=allowed)
    actual = direct_result(accumulator, index, area, stride=stride, allowed=allowed)
    expected = expected.sort_values("target_h3_cell").reset_index(drop=True)
    actual = actual.sort_values("target_h3_cell").reset_index(drop=True)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-6, atol=1e-7)
    for column in [
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
        "los_distance_weight_sum",
        "observer_mask",
        "distance_weighted_los_fraction",
        "joint_los_fraction",
        "union_visible_target_fraction",
        "any_observer_support_fraction",
    ]:
        if column in actual:
            np.testing.assert_array_equal(actual[column], expected[column])


def test_tile_reduction_is_independent_of_tile_partition_and_has_equivalent_fallback():
    small, index, area = reduction_fixture(8)
    large, _, _ = reduction_fixture(32)
    a = direct_result(small, index, area).sort_values("target_h3_cell").reset_index(drop=True)
    b = direct_result(large, index, area).sort_values("target_h3_cell").reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    assert small.grouped_h3(index).equals(small.grouped_h3(index, use_compiled=False))


def test_empty_tile_reduction_preserves_empty_schema_and_needs_no_raster():
    _, index, area = reduction_fixture()
    empty = TiledAccumulator((45, 54), row_offset=7, col_offset=9, n_observers=4)
    pd.testing.assert_frame_equal(
        direct_result(empty, index, area), reference_result(empty, index, area)
    )


def test_tile_reduction_rejects_grid_outside_accumulator_support():
    accumulator, _, _ = reduction_fixture()
    with pytest.raises(ValueError, match="target-code grid"):
        accumulator.grouped_h3(WaterPixelH3Index(np.zeros((5, 5), dtype="int32"), ("target",)))


@pytest.mark.parametrize("code", [-2, 4])
def test_tile_reduction_rejects_invalid_visible_target_codes(code):
    accumulator, index, _ = reduction_fixture()
    index.code_grid[8, 9] = code
    with pytest.raises(ValueError, match="invalid target code"):
        accumulator.grouped_h3(index)


@pytest.mark.parametrize("n_observers", [63, 64])
def test_observer_mask_boundary_and_zero_attenuation_preserve_los(n_observers):
    accumulator, index, area = reduction_fixture(
        n_observers=n_observers, distance_cutoff_km=0.00001
    )
    actual = (
        direct_result(accumulator, index, area).sort_values("target_h3_cell").reset_index(drop=True)
    )
    expected = (
        reference_result(accumulator, index, area)
        .sort_values("target_h3_cell")
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-6, atol=1e-7)
    assert (actual["joint_los_fraction"] > 0).all()
    assert (actual["distance_weighted_los_fraction"] == 0).all()
    if n_observers == 63:
        assert (actual["observer_mask"] == (1 << 63) - 1).all()
    else:
        assert "observer_mask" not in actual


def test_early_target_filter_preserves_native_weights_and_debug_raster_support(
    tmp_path, monkeypatch
):
    pytest.importorskip("osgeo.gdal")
    original = TiledAccumulator.grouped_h3
    outside_counts = []

    def inspect_accumulated_support(accumulator, index, **kwargs):
        allowed = kwargs["allowed_target_h3"]
        outside_codes = [i for i, cell in enumerate(index.h3_cells) if cell not in allowed]
        outside = 0
        for _, tile in accumulator.iter_tiles():
            codes = index.code_grid[
                tile.row_start : tile.row_start + tile.shape[0],
                tile.col_start : tile.col_start + tile.shape[1],
            ]
            outside += int(
                np.count_nonzero((tile.visible_count > 0) & np.isin(codes, outside_codes))
            )
        outside_counts.append(outside)
        return original(accumulator, index, **kwargs)

    monkeypatch.setattr(TiledAccumulator, "grouped_h3", inspect_accumulated_support)
    results = []
    totals = []
    for debug in (False, True):
        config = coastal_fixture(tmp_path / str(debug))
        raw = yaml.safe_load(config.read_text())
        raw["run"]["write_cumulative_rasters"] = debug
        raw["source_target_lookup"]["max_distance_km_land"] = 0.3
        config.write_text(yaml.safe_dump(raw))
        outside_counts.clear()
        paths = run_components(config, target="dem")
        results.append(pl.read_parquet(paths["build-dem-weights"]).sort("source_h3", "target_h3"))
        totals.append(sum(outside_counts))
    assert totals[0] == 0
    assert totals[1] > 0  # Full-water diagnostic rasters retain non-candidate pixels.
    assert results[0].equals(results[1])
