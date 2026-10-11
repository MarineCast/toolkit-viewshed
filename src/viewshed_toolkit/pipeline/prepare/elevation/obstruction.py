"""Opt-in canopy/building lower surfaces with conservative joint support."""

from dataclasses import dataclass

import numpy as np

OBSTRUCTION_CONTRACT = "above_ground_max_interval_v1"


@dataclass(frozen=True)
class ObstructionSurface:
    lower_height: np.ndarray
    unknown: np.ndarray
    qualified_water: np.ndarray
    water_conflicts: np.ndarray


def compose_above_ground_surface(
    canopy_lower: np.ndarray,
    canopy_unknown: np.ndarray,
    building_lower: np.ndarray,
    building_unknown: np.ndarray,
    *,
    qualified_water: np.ndarray,
    building_present: np.ndarray,
) -> ObstructionSurface:
    """Combine compatible AGL heights by max, retaining unknown upper support.

    Lower arrays contain finite nonnegative supported component lower bounds.
    Unknown components have unbounded upper height, even if the other is known.
    No footprint must stay building-unknown unless separately qualified absence.
    A building footprint contradicting qualified water keeps the cell uncertain;
    the conflicting building prediction is not treated as a proven lower height.
    Input arrays must already share one projected grid and are never mutated.
    """
    arrays = (canopy_unknown, building_lower, building_unknown, qualified_water, building_present)
    if (
        canopy_lower.ndim != 2
        or not canopy_lower.size
        or any(a.shape != canopy_lower.shape for a in arrays)
    ):
        raise ValueError("Nonempty aligned 2D arrays required")
    if any(
        a.dtype != np.bool_
        for a in (canopy_unknown, building_unknown, qualified_water, building_present)
    ):
        raise ValueError("Boolean support masks required")
    if any(not np.isfinite(a).all() or (a < 0).any() for a in (canopy_lower, building_lower)):
        raise ValueError("Finite nonnegative component lower heights required")
    conflicts = qualified_water & building_present
    water = qualified_water & ~conflicts
    lower = np.maximum(canopy_lower, building_lower).astype(np.float32)
    unknown = canopy_unknown | building_unknown
    lower[water] = 0
    unknown[water] = False
    lower[conflicts] = 0
    unknown[conflicts] = True
    return ObstructionSurface(lower, unknown, water, conflicts)
