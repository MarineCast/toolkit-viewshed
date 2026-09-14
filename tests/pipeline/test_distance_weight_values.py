from __future__ import annotations

import warnings

import numpy as np
import polars as pl
import pytest

from viewshed_toolkit.pipeline.weights.distance.compute import (
    DistanceWeightConfig,
    distance_weight_expr,
    distance_weight_values,
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
            piecewise_full_weight_km=3.0,
            piecewise_zero_weight_km=30.0,
        ),
    ],
)
def test_numpy_distance_evaluator_matches_polars_expression(
    cfg: DistanceWeightConfig,
) -> None:
    distances = np.asarray([0.0, 1.0, 3.0, 5.0, 9.0, 30.0, 31.0])
    expected = (
        pl.DataFrame({"distance_km": distances})
        .with_columns(distance_weight_expr(cfg, max_distance_km=30.0))
        .get_column("weight_distance")
        .to_numpy()
    )

    actual = distance_weight_values(distances, cfg, max_distance_km=30.0)

    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-7)
    assert actual.dtype == np.dtype("float32")


def test_logistic_distance_evaluator_is_stable_for_extreme_distances() -> None:
    cfg = DistanceWeightConfig(
        selected_model="logistic",
        logistic_d50_km=9.0,
        logistic_slope_km=2.7,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        actual = distance_weight_values(
            np.asarray([0.0, 30.0, 10_000.0]),
            cfg,
            max_distance_km=30.0,
        )

    assert not caught
    np.testing.assert_array_equal(actual[-1:], np.asarray([0.0], dtype="float32"))
