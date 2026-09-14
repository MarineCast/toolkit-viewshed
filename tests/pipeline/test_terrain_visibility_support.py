from __future__ import annotations

import numpy as np
import pandas as pd
import polars as pl
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.weights.distance.compute import (
    DistanceWeightConfig,
    distance_weight_values,
)
from viewshed_toolkit.pipeline.weights.terrain import summarize
from viewshed_toolkit.pipeline.weights.terrain.accumulators import TiledAccumulator
from viewshed_toolkit.pipeline.weights.terrain.summarize import (
    _accumulate_visible,
    add_terrain_visibility_support,
)


def _support_row(
    frame: pd.DataFrame,
    *,
    n_observer_points: int | None = None,
) -> dict[str, object]:
    return add_terrain_visibility_support(
        pl.from_pandas(frame, include_index=False),
        n_observer_points=n_observer_points,
    ).row(0, named=True)


@pytest.mark.skipif(
    not summarize._NUMBA_AVAILABLE,
    reason="numba is not installed",
)
@pytest.mark.parametrize(
    "cfg",
    [
        DistanceWeightConfig(
            selected_model="logistic",
            logistic_d50_km=9.0,
            logistic_slope_km=2.7,
            normalize_at_zero=True,
        ),
        DistanceWeightConfig(
            selected_model="exponential",
            exponential_lambda_km=8.0,
        ),
        DistanceWeightConfig(
            selected_model="piecewise",
            piecewise_full_weight_km=2.0,
            piecewise_zero_weight_km=20.0,
        ),
    ],
)
@pytest.mark.parametrize("with_observer_mask", [False, True])
def test_compiled_visible_accumulation_matches_numpy_reference(
    cfg: DistanceWeightConfig,
    with_observer_mask: bool,
) -> None:
    visible = np.asarray(
        [
            [True, False, True, False, True],
            [False, True, True, False, False],
            [True, False, False, True, True],
            [False, True, False, True, False],
        ],
        dtype=bool,
    )
    transform = Affine(30.0, 1.5, 480_000.0, -0.75, -30.0, 5_371_000.0)

    def arrays():
        shape = (8, 10)
        return (
            np.zeros(shape, dtype="uint16"),
            np.full(shape, np.inf, dtype="float32"),
            np.zeros(shape, dtype="int64"),
            np.zeros(shape, dtype="int64"),
            np.zeros(shape, dtype="float32"),
            np.zeros(shape, dtype="uint64") if with_observer_mask else None,
        )

    reference = arrays()
    compiled = arrays()
    common = dict(
        visible=visible,
        transform=transform,
        observer_x=480_743.125,
        observer_y=5_370_612.875,
        sample_index=3 if with_observer_mask else 65,
        distance_weight_config=cfg,
        distance_weight_max_km=30.0,
        row_offset=10,
        col_offset=20,
        storage_row_offset=2,
        storage_col_offset=3,
    )
    _accumulate_visible(
        **common,
        visible_count=reference[0],
        min_distance=reference[1],
        max_distance=reference[2],
        distance_sum=reference[3],
        distance_weight_sum=reference[4],
        observer_mask=reference[5],
        use_compiled=False,
    )
    _accumulate_visible(
        **common,
        visible_count=compiled[0],
        min_distance=compiled[1],
        max_distance=compiled[2],
        distance_sum=compiled[3],
        distance_weight_sum=compiled[4],
        observer_mask=compiled[5],
        use_compiled=True,
    )

    np.testing.assert_array_equal(compiled[0], reference[0])
    for actual, expected in zip(compiled[1:3], reference[1:3], strict=True):
        np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-5)
    np.testing.assert_allclose(compiled[3], reference[3], rtol=0.0, atol=1)
    np.testing.assert_allclose(compiled[4], reference[4], rtol=0.0, atol=1)
    if with_observer_mask:
        np.testing.assert_array_equal(compiled[5], reference[5])


def test_visible_accumulation_uses_observer_to_pixel_distance_weights() -> None:
    visible = np.asarray([[True, True]], dtype=bool)
    visible_count = np.zeros((1, 2), dtype="uint16")
    min_distance = np.full((1, 2), np.inf, dtype="float32")
    max_distance = np.zeros((1, 2), dtype="float32")
    distance_sum = np.zeros((1, 2), dtype="int64")
    distance_weight_sum = np.zeros((1, 2), dtype="int64")
    observer_mask = np.zeros((1, 2), dtype="uint64")
    cfg = DistanceWeightConfig(
        selected_model="logistic",
        logistic_d50_km=1.0,
        logistic_slope_km=0.25,
        normalize_at_zero=True,
    )
    transform = Affine.translation(0.0, 0.0) * Affine.scale(1_000.0, -1_000.0)

    _accumulate_visible(
        visible,
        transform,
        observer_x=0.0,
        observer_y=-500.0,
        sample_index=1,
        visible_count=visible_count,
        min_distance=min_distance,
        max_distance=max_distance,
        distance_sum=distance_sum,
        distance_weight_sum=distance_weight_sum,
        distance_weight_config=cfg,
        distance_weight_max_km=5.0,
        observer_mask=observer_mask,
    )

    expected_distances_km = np.asarray([0.5, 1.5])
    expected_weights = distance_weight_values(expected_distances_km, cfg, 5.0)
    np.testing.assert_array_equal(visible_count, np.asarray([[1, 1]], dtype="uint16"))
    np.testing.assert_allclose(
        distance_sum[0] / summarize.DISTANCE_SUM_SCALE / 1_000.0,
        expected_distances_km,
    )
    np.testing.assert_allclose(
        distance_weight_sum[0] / summarize.DISTANCE_WEIGHT_SUM_SCALE,
        expected_weights,
        atol=0.5 / summarize.DISTANCE_WEIGHT_SUM_SCALE,
    )
    assert distance_weight_sum[0, 0] > distance_weight_sum[0, 1]


def test_tiled_accumulator_allocates_only_tiles_with_visible_pixels() -> None:
    cfg = DistanceWeightConfig(selected_model="exponential", exponential_lambda_km=8.0)
    transform = Affine.translation(0.0, 0.0) * Affine.scale(10.0, -10.0)
    visible = np.zeros((12, 12), dtype=bool)
    visible[1, 1] = True
    visible[10, 10] = True
    tiled = TiledAccumulator(
        (16, 16),
        row_offset=100,
        col_offset=200,
        n_observers=2,
        tile_size=8,
    )

    tiled.accumulate(
        visible=visible,
        row_start=100,
        col_start=200,
        transform=transform,
        observer_x=2_015.0,
        observer_y=-1_015.0,
        sample_index=1,
        distance_weight_config=cfg,
        distance_weight_max_km=30.0,
        use_compiled=False,
    )

    assert set(tiled.tiles) == {(0, 0), (1, 1)}
    assert tiled.allocated_pixel_count == 128
    assert tiled.allocated_pixel_count < 16 * 16
    assert tiled.visible_pixel_count == 2
    sparse = tiled.sparse_arrays()
    assert set(zip(sparse.rows.tolist(), sparse.cols.tolist(), strict=True)) == {
        (101, 201),
        (110, 210),
    }
    np.testing.assert_array_equal(sparse.visible_count, np.asarray([1, 1], dtype="uint16"))
    np.testing.assert_array_equal(sparse.observer_mask, np.asarray([1, 1], dtype="uint64"))


def test_tiled_accumulator_does_not_allocate_for_empty_observer_result() -> None:
    tiled = TiledAccumulator(
        (32, 32),
        row_offset=0,
        col_offset=0,
        n_observers=1,
        tile_size=8,
    )
    tiled.accumulate(
        visible=np.zeros((16, 16), dtype=bool),
        row_start=0,
        col_start=0,
        transform=Affine.identity(),
        observer_x=0.0,
        observer_y=0.0,
        sample_index=1,
        distance_weight_config=DistanceWeightConfig(),
        distance_weight_max_km=30.0,
        use_compiled=False,
    )

    assert tiled.tiles == {}
    assert tiled.memory_bytes == 0


def test_quantized_accumulation_is_independent_of_observer_completion_order() -> None:
    visible = np.asarray([[True, True, True]], dtype=bool)
    cfg = DistanceWeightConfig(
        selected_model="logistic",
        logistic_d50_km=1.0,
        logistic_slope_km=0.25,
        normalize_at_zero=True,
    )
    transform = Affine.translation(0.0, 0.0) * Affine.scale(500.0, -500.0)
    observers = [(1, 0.0, -250.0), (2, 1_000.0, -250.0), (3, 250.0, -250.0)]

    def accumulate(order: list[int]) -> tuple[np.ndarray, ...]:
        arrays = summarize._init_cumulative_arrays((1, 3), len(observers))
        for observer_index in order:
            sample_index, observer_x, observer_y = observers[observer_index]
            _accumulate_visible(
                visible,
                transform,
                observer_x=observer_x,
                observer_y=observer_y,
                sample_index=sample_index,
                visible_count=arrays[0],
                min_distance=arrays[1],
                max_distance=arrays[2],
                distance_sum=arrays[3],
                distance_weight_sum=arrays[4],
                distance_weight_config=cfg,
                distance_weight_max_km=5.0,
                observer_mask=arrays[5],
            )
        return arrays

    submitted_order = accumulate([0, 1, 2])
    completion_order = accumulate([2, 0, 1])

    for expected, actual in zip(submitted_order, completion_order, strict=True):
        np.testing.assert_array_equal(actual, expected)


def test_distance_weighted_los_fraction_is_primary_terrain_weight() -> None:
    frame = pd.DataFrame(
        {
            "visible_from_n_points": [3],
            "visible_area_km2_approx_sum": [0.25],
            "target_water_area_km2": [0.5],
            "visible_observer_pixel_count_sum": [30],
            "los_distance_weight_sum": [18.0],
            "target_water_pixel_count": [100],
            "pixel_stride": [2],
            "terrain_visible_clear_sky": [True],
        }
    )

    result = _support_row(frame, n_observer_points=4)

    assert result["any_observer_support_fraction"] == pytest.approx(0.75)
    assert result["union_visible_target_fraction"] == pytest.approx(0.5)
    assert result["joint_los_fraction"] == pytest.approx(0.3)
    assert result["distance_weighted_los_fraction"] == pytest.approx(0.18)
    assert result["terrain_visibility_support"] == pytest.approx(0.18)
    assert result["observer_sample_fraction"] == pytest.approx(0.75)
    assert result["visible_area_fraction"] == pytest.approx(0.5)
    assert result["n_observers"] == 4


def test_distance_weighted_los_fraction_is_clipped_to_unit_interval() -> None:
    frame = pd.DataFrame(
        {
            "visible_from_n_points": [2],
            "visible_area_km2_approx_sum": [1.0],
            "target_water_area_km2": [1.0],
            "visible_observer_pixel_count_sum": [200],
            "los_distance_weight_sum": [150.0],
            "target_water_pixel_count": [10],
            "pixel_stride": [2],
            "terrain_visible_clear_sky": [True],
        }
    )

    result = _support_row(frame, n_observer_points=2)

    assert result["joint_los_fraction"] == pytest.approx(1.0)
    assert result["distance_weighted_los_fraction"] == pytest.approx(1.0)
    assert result["terrain_visibility_support"] == pytest.approx(1.0)


def test_joint_los_fraction_requires_reproducible_denominators() -> None:
    frame = pd.DataFrame(
        {
            "visible_from_n_points": [1],
            "visible_area_km2_approx_sum": [0.1],
            "target_water_area_km2": [0.2],
        }
    )

    with pytest.raises(ValueError, match="numerator/denominator"):
        add_terrain_visibility_support(
            pl.from_pandas(frame, include_index=False),
            n_observer_points=1,
        )


def test_actual_source_sample_count_has_denominator_priority() -> None:
    frame = pd.DataFrame(
        {
            "visible_from_n_points": [1],
            "sample_points_actual": [2],
            "sample_points_requested": [3],
            "sample_points_per_source_cell": [10],
            "visible_area_km2_approx_sum": [0.1],
            "target_water_area_km2": [0.2],
            "visible_observer_pixel_count_sum": [1],
            "los_distance_weight_sum": [1.0],
            "target_water_pixel_count": [1],
            "pixel_stride": [1],
            "terrain_visible_clear_sky": [True],
        }
    )

    result = _support_row(frame, n_observer_points=4)

    assert result["n_observers"] == 2
    assert result["any_observer_support_fraction"] == pytest.approx(0.5)
    assert result["joint_los_fraction"] == pytest.approx(0.5)


def test_visible_observer_count_cannot_exceed_actual_samples() -> None:
    frame = pd.DataFrame(
        {
            "visible_from_n_points": [3],
            "sample_points_actual": [2],
            "sample_points_requested": [3],
            "visible_area_km2_approx_sum": [0.1],
            "target_water_area_km2": [0.2],
            "visible_observer_pixel_count_sum": [3],
            "los_distance_weight_sum": [2.0],
            "target_water_pixel_count": [2],
            "pixel_stride": [1],
            "terrain_visible_clear_sky": [True],
        }
    )

    with pytest.raises(ValueError, match="exceeds.*denominator"):
        add_terrain_visibility_support(pl.from_pandas(frame, include_index=False))
