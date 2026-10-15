"""Joint direct-ray transmission arithmetic and conditional distance diagnostics."""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import rasterio

from ..prepare.elevation.obstruction import compose_above_ground_surface
from .distance.compute import distance_weight_values
from .rays import direct_ray_visibility
from .transmission_reduce import aggregate_support, reduce_weights
from .transmission_stationary import stationary_compact
from .vegetation.classified import classified_ray_support, prepare_classified_canopy


@dataclass(frozen=True)
class PreparedCalculation:
    """Owned immutable fields shared by observer threads and census reductions."""

    ground: Any
    canopy: Any
    building: Any
    building_unknown: Any
    transform: Any
    terrain_support: Any
    ray_reference: Any
    profile: Any
    canopy_imputation: dict[str, Any]


def _immutable(array: Any) -> Any:
    owned = np.array(array, copy=True)
    owned.flags.writeable = False
    return owned


def prepare_calculation(
    data: Any,
    classes: Any,
    *,
    water_class_policy: Any = "zero_if_missing",
    terrain_support_mask: Any = None,
    canopy_gap_policy: str = "retain_unknown",
    canopy_gap_qualified: bool = False,
) -> PreparedCalculation:
    """Qualify and own stationary inputs once, without filling unsupported terrain."""
    if canopy_gap_policy not in {"retain_unknown", "assume_zero_acquired"}:
        raise ValueError("Unknown canopy gap policy")
    if canopy_gap_policy == "assume_zero_acquired" and canopy_gap_qualified is not True:
        raise ValueError("Acquired canopy coverage must be qualified before gap imputation")
    started = time.perf_counter()
    ground = _immutable(data["ground"])
    stationary = stationary_compact(data, classes, water_class_policy)
    if stationary is None:
        buildings = compose_above_ground_surface(
            np.zeros_like(ground),
            np.zeros(ground.shape, bool),
            data["building"],
            data["building_unknown"],
            qualified_water=data["qualified_water"],
            building_present=data["building_present"],
        )
        canopy = np.where(buildings.qualified_water, 0, data["canopy"])
        unknown = data["canopy_unknown"] & ~buildings.qualified_water
        prepared_canopy = prepare_classified_canopy(canopy, unknown, classes, water_class_policy)
        building = _immutable(buildings.lower_height)
        building_unknown = _immutable(buildings.unknown)
    else:
        building, building_unknown, prepared_canopy = stationary
    if terrain_support_mask is not None:
        if terrain_support_mask.dtype != np.bool_ or terrain_support_mask.shape != ground.shape:
            raise ValueError("Aligned boolean terrain support required")
        if not np.isfinite(ground[terrain_support_mask]).all():
            raise ValueError("Required terrain support must be finite")
        support = _immutable(terrain_support_mask)
    else:
        if not np.isfinite(ground).all():
            raise ValueError("Complete terrain required unless explicit support is supplied")
        support = None
    imputed = np.zeros(ground.shape, bool)
    if canopy_gap_policy == "assume_zero_acquired":
        imputed = prepared_canopy.missing_height.copy()
        if (prepared_canopy.canopy[imputed] != 0).any():
            raise ValueError("Only missing zero-floor canopy gaps may be imputed")
        prepared_canopy = replace(
            prepared_canopy,
            input_unknown=_immutable(prepared_canopy.input_unknown & ~imputed),
            missing_height=_immutable(prepared_canopy.missing_height & ~imputed),
        )
    imputation = dict(
        policy=canopy_gap_policy,
        assumption_active=canopy_gap_policy == "assume_zero_acquired",
        mask_scope="source_window_before_observer_clearance",
        original_unknown_mask_sha256=hashlib.sha256(data["canopy_unknown"].tobytes()).hexdigest(),
        imputed_mask_sha256=hashlib.sha256(imputed.tobytes()).hexdigest(),
        imputed_pixels=int(imputed.sum()),
        window_pixels=int(imputed.size),
        imputed_window_fraction=float(imputed.mean()),
        required_support_imputed_pixels=(
            int(imputed[terrain_support_mask].sum())
            if terrain_support_mask is not None
            else int(imputed.sum())
        ),
        interpretation="Modelled zero canopy for acquired gaps; not measured tree absence",
        original_mask_retained_in_prepared_store=True,
        caveat="May overestimate physical visibility where trees were not observed",
    )
    return PreparedCalculation(
        ground,
        prepared_canopy,
        building,
        building_unknown,
        data["transform"],
        support,
        data.get("ray_reference_transform", data["transform"]),
        dict(
            class_preparation_seconds=prepared_canopy.class_preparation_seconds,
            stationary_preparation_seconds=time.perf_counter() - started,
        ),
        imputation,
    )


def calculate(
    data: Any,
    classes: Any,
    observers: Any,
    targets: Any,
    codes: Any,
    weights: Any,
    cells: Any,
    radius: Any,
    *,
    distance_config: Any = None,
    water_class_policy: Any = "zero_if_missing",
    observer_workers: int = 1,
    terrain_support_mask: Any = None,
    profile: Any = None,
) -> Any:
    """Backward-compatible calculation with stationary preparation once per call."""
    started = time.perf_counter()
    prepared = prepare_calculation(
        data,
        classes,
        water_class_policy=water_class_policy,
        terrain_support_mask=terrain_support_mask,
    )
    result, _ = calculate_prepared(
        prepared,
        observers,
        targets,
        codes,
        weights,
        cells,
        radius,
        distance_config=distance_config,
        observer_workers=observer_workers,
        profile=profile,
    )
    return result, time.perf_counter() - started


def calculate_prepared(
    prepared: PreparedCalculation,
    observers: Any,
    targets: Any,
    codes: Any,
    weights: Any,
    cells: Any,
    radius: Any,
    *,
    distance_config: Any = None,
    observer_workers: int = 1,
    calculation_mode: str = "reference",
    target_chunks: int = 4,
    pool: Any = None,
    profile: Any = None,
    sampled_subset: Any = None,
) -> Any:
    """Deterministic row order with immutable fields and bounded native observer threads."""
    if (
        isinstance(observer_workers, bool)
        or not isinstance(observer_workers, int)
        or not 1 <= observer_workers <= 32
    ):
        raise ValueError("Observer workers must be an integer from1to32")
    if calculation_mode not in {"reference", "exact_fast", "block_jump"}:
        raise ValueError("Unknown calculation mode")
    if type(target_chunks) is not int or not 1 <= target_chunks <= 4:
        raise ValueError("Target chunks must be an integer from 1 to 4")
    if calculation_mode in {"exact_fast", "block_jump"}:
        from .transmission_fast import calculate_chunked

        return calculate_chunked(
            prepared,
            observers,
            targets,
            codes,
            weights,
            cells,
            radius,
            distance_config=distance_config,
            observer_workers=observer_workers,
            chunks_per_observer=target_chunks,
            pool=pool,
            profile=profile,
            sampled_subset=sampled_subset,
            block_jumps=calculation_mode == "block_jump",
        )
    if sampled_subset is not None:
        raise ValueError("Sample reuse requires exact_fast or block_jump calculation mode")
    started = time.perf_counter()
    ground = prepared.ground
    pc = prepared.canopy
    values = np.empty((len(observers), len(targets)), dtype="float64")
    bare = np.empty(values.shape, dtype="int8") if distance_config is not None else None
    decay = np.empty(values.shape, dtype="float64") if distance_config is not None else None
    zero = np.zeros_like(ground) if distance_config is not None else None
    if zero is not None:
        zero.flags.writeable = False
    metrics: list[Any] = [None] * len(observers)

    def one(i: int) -> None:
        x, y, elevation = observers[i]
        clearance = np.zeros(ground.shape, bool)
        row, col = rasterio.transform.rowcol(prepared.transform, x, y)
        r0, r1, c0, c1 = (
            max(0, row - 4),
            min(ground.shape[0], row + 5),
            max(0, col - 4),
            min(ground.shape[1], col + 5),
        )
        rr, cc = np.mgrid[r0:r1, c0:c1]
        xx, yy = prepared.transform * (cc + 0.5, rr + 0.5)
        clearance[r0:r1, c0:c1] = np.hypot(xx - x, yy - y) <= 30
        clearance[row, col] = True
        item: dict[str, Any] = {}
        values[i] = classified_ray_support(
            ground,
            pc.canopy,
            pc.input_unknown,
            pc.landcover,
            prepared.building,
            prepared.building_unknown,
            transform=prepared.transform,
            observer_xy=(x, y),
            targets_xy=targets,
            observer_ground_elevation_m=float(elevation),
            water_class_policy=pc.water_class_policy,
            canopy_clearance_mask=clearance,
            max_distance_m=radius,
            terrain_support_mask=prepared.terrain_support,
            ray_reference_transform=prepared.ray_reference,
            prepared_canopy=pc,
            profile=item if profile is not None else None,
        )
        if distance_config is not None:
            assert bare is not None and decay is not None and zero is not None
            bare_started = time.perf_counter()
            bare[i] = direct_ray_visibility(
                ground,
                zero,
                pc.zero_clearance,
                transform=prepared.transform,
                observer_xy=(x, y),
                targets_xy=targets,
                observer_ground_elevation_m=float(elevation),
                max_distance_m=radius,
                terrain_support_mask=prepared.terrain_support,
                ray_reference_transform=prepared.ray_reference,
                clearance_mask=pc.zero_clearance,
            )
            item["bare_ray_seconds"] = time.perf_counter() - bare_started
            decay[i] = distance_weight_values(
                np.hypot(targets[:, 0] - x, targets[:, 1] - y) / 1000,
                distance_config,
                radius / 1000,
            )
        metrics[i] = item

    if observer_workers == 1:
        for i in range(len(observers)):
            one(i)
    else:
        with ThreadPoolExecutor(
            max_workers=min(observer_workers, len(observers)),
            thread_name_prefix="transmission-observer",
        ) as pool:
            list(pool.map(one, range(len(observers))))
    result = aggregate_support(values, codes, weights, len(cells))
    if distance_config is not None:
        result["diagnostics"] = reduce_weights(bare, values, decay, codes, weights, len(cells))
    elapsed = time.perf_counter() - started
    if profile is not None:
        profile.update(prepared.profile)
        profile.update(
            observer_workers=observer_workers,
            scoring_wall_seconds=elapsed,
            observer_phase_seconds=metrics,
        )
    return result, elapsed
