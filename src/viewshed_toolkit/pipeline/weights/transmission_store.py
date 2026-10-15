"""Source-dependent weights over immutable, already prepared direct-transmission tiles."""

from __future__ import annotations

import time
from typing import Any

import h3
import numpy as np
import polars as pl
from affine import Affine
from shapely.geometry import shape

from ..contracts.transmission import TRANSMISSION_PAIR_SCHEMA
from ..prepare.area.lookup import _haversine_expr
from ..prepare.area.transmission import (
    WaterTargetProvider,
    required_support_mask,
    target_design,
    validate_terrain_support,
)
from ..prepare.area.transmission_envelope import validate_cropped_envelope
from ..prepare.area.transmission_support_cache import SupportMaskCache
from ..prepare.transmission import observer_design
from .distance.compute import distance_weight_expr
from .transmission import calculate_prepared, prepare_calculation


def sampling_difference(sampled: dict[str, Any], census: dict[str, Any]) -> dict[str, float]:
    """Original weighted, unweighted and applicability census gates."""
    errors = [np.abs(sampled[key] - census[key]) for key in ("lower", "upper", "unknown")]
    for a, b in zip(sampled["diagnostics"], census["diagnostics"], strict=True):
        if (a["above_ground_status"] == "not_applicable") != (
            b["above_ground_status"] == "not_applicable"
        ):
            errors.append(np.array([1.0]))
        for key in ("weight_terrain", "above_ground_lower", "above_ground_upper"):
            if (a[key] is None) != (b[key] is None):
                errors.append(np.array([1.0]))
            elif a[key] is not None:
                errors.append(np.array([abs(a[key] - b[key])]))
    values = np.concatenate(errors)
    return {"maximum": float(values.max()), "p95": float(np.quantile(values, 0.95))}


def score_prepared_source(
    store: Any,
    source: dict[str, Any],
    reporting_water: Any,
    provider: WaterTargetProvider,
    config: Any,
    *,
    census: bool,
    water_class_policy: str,
    canopy_gap_policy: str = "retain_unknown",
    canopy_gap_qualified: bool = False,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Read a bounded source window; never warp, rasterize, write or mutate the store."""
    started = time.perf_counter()
    grid = Affine(*source["window_transform"])
    original = Affine(*source["original_transform"])
    validate_cropped_envelope(source["read_envelope"], grid, source["window_shape"])
    row0 = round((store.grid.f - grid.f) / 10)
    col0 = round((grid.c - store.grid.c) / 10)
    required = sorted(
        [row, col]
        for row in range(row0 // 384 * 384, row0 + source["window_shape"][0], 384)
        for col in range(col0 // 384 * 384, col0 + source["window_shape"][1], 384)
    )
    if source["required_tile_keys"] != required or any(
        tuple(key) not in store.keys for key in required
    ):
        raise ValueError("Source H3 tile lookup differs from complete window support")
    data = store.window(grid, tuple(source["window_shape"]))
    prefetch_seconds = getattr(store, "last_window_prefetch_seconds", 0.0)
    mask = required_support_mask
    if getattr(config, "calculation_mode", "reference") in {"exact_fast", "block_jump"}:
        identity = (id(reporting_water), config.max_distance_m)
        if getattr(store, "_support_identity", None) != identity:
            store._support_cache = SupportMaskCache(
                reporting_water, store.grid, config.max_distance_m
            )
            store._support_identity = identity
        mask = store._support_cache.mask
    support = mask(
        grid,
        tuple(source["window_shape"]),
        shape(source["source_land"]),
        reporting_water,
        config.max_distance_m,
    )
    validate_terrain_support(data["ground"], support)
    observers, outdoor = observer_design(source, data, samples=config.source_samples)
    targets, rr, cc, inside, cells, codes = provider.select(
        observers,
        original_grid=original,
        grid_shape=tuple(source["original_shape"]),
        max_distance_m=config.max_distance_m,
    )
    if not inside.all():
        raise ValueError("Incomplete target coverage")
    info: dict[str, Any] = {
        "source_h3": source["source_h3"],
        "raster_batch_assembly_seconds": prefetch_seconds,
        "source_outdoor_land": outdoor,
        "observers": observers.tolist(),
        "target_water_points": len(targets),
        "target_cells": len(cells),
        "source_state": "complete",
    }
    if not len(targets):
        elapsed = time.perf_counter() - started + prefetch_seconds
        info.update(
            preparation_seconds=elapsed,
            total_seconds=elapsed,
            source_state="no_target_population",
            sampled_result_used=None,
            sampling_error=None,
            census_checked=False,
        )
        return pl.DataFrame(schema=TRANSMISSION_PAIR_SCHEMA), info
    selected, weights = target_design(
        rr,
        cc,
        codes,
        config.target_stride,
        census_threshold=config.census_threshold,
    )
    data["ray_reference_transform"] = original
    prepared = prepare_calculation(
        data,
        data["classes"],
        water_class_policy=water_class_policy,
        terrain_support_mask=support,
        canopy_gap_policy=canopy_gap_policy,
        canopy_gap_qualified=canopy_gap_qualified,
    )
    info["canopy_imputation"] = prepared.canopy_imputation
    del data, support
    distance = config.distance_config()
    info["preparation_seconds"] = time.perf_counter() - started + prefetch_seconds
    shared = census and getattr(config, "calculation_mode", "reference") in {
        "exact_fast",
        "block_jump",
    }
    result, seconds = calculate_prepared(
        prepared,
        observers,
        targets if shared else targets[selected],
        codes if shared else codes[selected],
        np.ones(len(targets)) if shared else weights,
        cells,
        config.max_distance_m,
        distance_config=distance,
        observer_workers=config.observer_workers,
        calculation_mode=getattr(config, "calculation_mode", "reference"),
        target_chunks=getattr(config, "target_chunks", 4),
        pool=getattr(store, "scoring_pool", None),
        **({"sampled_subset": (selected, weights)} if shared else {}),
    )
    if shared:
        reference = result
        result = reference.pop("sampled_result")
        sample_seconds = reference.pop("sampled_reduction_seconds")
        info["shared_census_traversal"] = True
        info["reference_seconds"] = seconds - sample_seconds
        seconds = sample_seconds
    info.update(
        scoring_seconds=seconds,
        sampled_target_points=len(selected),
        sampled_result_used=True,
        sampling_error=None,
        census_checked=census,
    )
    if census:
        if not shared:
            reference, seconds = calculate_prepared(
                prepared,
                observers,
                targets,
                codes,
                np.ones(len(targets)),
                cells,
                config.max_distance_m,
                distance_config=distance,
                observer_workers=config.observer_workers,
                calculation_mode=getattr(config, "calculation_mode", "reference"),
                target_chunks=getattr(config, "target_chunks", 4),
                pool=getattr(store, "scoring_pool", None),
            )
            info["reference_seconds"] = seconds
        error = sampling_difference(result, reference)
        info["sampling_error"] = error
        if (
            error["maximum"] > config.maximum_sampling_error
            or error["p95"] > config.p95_sampling_error
        ):
            raise ValueError("Weighted sampling census gate rejected the approximation")
    info["pair_support_interpretation"] = {
        str(cell): (
            "unknown"
            if result["unknown"][i] > 0
            else (
                "known_with_canopy_gap_zero_assumption_active"
                if canopy_gap_policy == "assume_zero_acquired"
                else "known_without_gap_imputation"
            )
        )
        for i, cell in enumerate(cells)
    }
    counts = np.bincount(codes, minlength=len(cells))
    sampled_counts = np.bincount(codes[selected], minlength=len(cells))
    rows = [
        dict(
            **result["diagnostics"][i],
            source_type="land",
            source_h3=source["source_h3"],
            target_h3=str(cell),
            method=config.method,
            target_support="partial_h3_in_observer_reach",
            support_status="unknown" if result["unknown"][i] > 0 else "known",
            lower=float(result["lower"][i]),
            upper=float(result["upper"][i]),
            average_viewability=result["score"][i],
            input_unknown_fraction=float(result["unknown"][i]),
            out_of_range_fraction=float(result["out_of_range"][i]),
            target_water_pixel_count=int(counts[i]),
            sampled_target_points=int(sampled_counts[i]),
            observer_count=len(observers),
            max_distance_m=config.max_distance_m,
            sampling=f"stride{config.target_stride}",
            census_checked=census,
        )
        for i, cell in enumerate(cells)
    ]
    frame = pl.DataFrame(
        rows,
        schema_overrides={
            k: pl.Float64
            for k in (
                "average_viewability",
                "above_ground_lower",
                "above_ground_upper",
                "weight_above_ground",
            )
        },
    )
    slat, slon = h3.cell_to_latlng(source["source_h3"])
    centres = [h3.cell_to_latlng(str(cell)) for cell in cells]
    distances = pl.DataFrame(
        dict(
            source_lat=[slat] * len(cells),
            source_lon=[slon] * len(cells),
            target_lat=[p[0] for p in centres],
            target_lon=[p[1] for p in centres],
        )
    ).select(
        _haversine_expr("source_lat", "source_lon", "target_lat", "target_lon").alias("distance_km")
    )
    frame = frame.hstack(distances).with_columns(
        distance_weight_expr(distance, distance.hard_cutoff_km)
    )
    frame = frame.select(list(TRANSMISSION_PAIR_SCHEMA)).cast(pl.Schema(TRANSMISSION_PAIR_SCHEMA))
    info["total_seconds"] = time.perf_counter() - started + prefetch_seconds
    return frame, info
