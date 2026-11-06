"""Original ordered direct-transmission and conditional-distance reductions."""

from typing import Any

import numpy as np


def aggregate_support(values: Any, codes: Any, weights: Any, cell_count: Any) -> Any:
    """Full-population transmission bounds; no available-only renormalization."""
    if (
        values.ndim != 2
        or values.shape[1] != len(codes)
        or len(codes) != len(weights)
        or not np.isfinite(values).all()
        or (weights <= 0).any()
        or not ((values == -1) | (values == -2) | ((values >= 0) & (values <= 1))).all()
    ):
        raise ValueError("Aligned valid transmission states and positive weights required")
    total = np.bincount(codes, weights=weights, minlength=cell_count) * len(values)
    if (total <= 0).any():
        raise ValueError("Every target cell requires positive observer/target support")
    unknown = np.bincount(codes, weights=(values == -1).sum(0) * weights, minlength=cell_count)
    summed = np.bincount(
        codes, weights=np.maximum(values, 0).sum(0) * weights, minlength=cell_count
    )
    lower, upper = summed / total, (summed + unknown) / total
    return dict(
        score=[None if n else float(v) for v, n in zip(lower, unknown, strict=True)],
        lower=lower,
        upper=upper,
        unknown=unknown / total,
        out_of_range=np.bincount(
            codes, weights=(values == -2).sum(0) * weights, minlength=cell_count
        )
        / total,
    )


def reduce_weights(bare: Any, joint: Any, decay: Any, codes: Any, weights: Any, count: Any) -> Any:
    """Distance-integrated terrain; conditional above-ground bounds, never double decay."""
    decay = np.asarray(decay, dtype="float64")
    if (
        bare.shape != joint.shape
        or bare.shape != decay.shape
        or bare.ndim != 2
        or bare.shape[1] != len(codes)
        or len(weights) != len(codes)
        or not np.isfinite(decay).all()
        or ((decay < 0) | (decay > 1)).any()
        or not np.isfinite(weights).all()
        or (weights <= 0).any()
        or not np.isin(bare, [0, 1, -2]).all()
        or not ((joint == -1) | (joint == -2) | ((joint >= 0) & (joint <= 1))).all()
        or ((bare != 1) & (joint > 0)).any()
    ):
        raise ValueError("Invalid aligned ray support")
    total = np.bincount(codes, weights=weights, minlength=count) * len(bare)
    if (total <= 0).any():
        raise ValueError("Every target requires positive support")
    terrain_sum = np.bincount(
        codes, weights=((bare == 1) * decay).sum(0) * weights, minlength=count
    )
    lower_sum = np.bincount(
        codes, weights=(np.maximum(joint, 0) * decay).sum(0) * weights, minlength=count
    )
    missing_sum = np.bincount(
        codes, weights=((joint == -1) * (bare == 1) * decay).sum(0) * weights, minlength=count
    )
    if (lower_sum + missing_sum > terrain_sum + 1e-8).any():
        raise ValueError("Above-ground transmission exceeds terrain support")
    lower = np.divide(lower_sum, terrain_sum, out=np.zeros(count), where=terrain_sum > 0)
    upper = np.divide(
        lower_sum + missing_sum, terrain_sum, out=np.zeros(count), where=terrain_sum > 0
    )
    return [
        dict(
            weight_terrain=float(terrain_sum[i] / total[i]),
            above_ground_lower=float(np.clip(lower[i], 0, 1)) if terrain_sum[i] > 0 else None,
            above_ground_upper=float(np.clip(upper[i], 0, 1)) if terrain_sum[i] > 0 else None,
            weight_above_ground=(
                float(np.clip(lower[i], 0, 1))
                if terrain_sum[i] > 0 and missing_sum[i] == 0
                else None
            ),
            above_ground_status=(
                "not_applicable"
                if terrain_sum[i] == 0
                else "unknown" if missing_sum[i] > 0 else "known"
            ),
        )
        for i in range(count)
    ]
