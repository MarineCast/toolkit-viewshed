"""Explicit research LOS on a joint canopy/building above-ground surface."""

import numpy as np
from numpy.typing import NDArray

from ..prepare.elevation.buildings import BuildingSurface
from ..prepare.elevation.obstruction import compose_above_ground_surface


def above_ground_visibility(
    ground: NDArray[np.generic],
    canopy_lower: NDArray[np.generic],
    canopy_unknown: NDArray[np.bool_],
    buildings: BuildingSurface,
    *,
    qualified_water: NDArray[np.bool_],
    observer_row: int,
    observer_col: int,
    pixel_size_m: float,
    canopy_clearance_mask: NDArray[np.bool_] | None = None,
    grounded_observer: bool = False,
    observer_height_m: float = 1.7,
    target_height_m: float = 1.0,
    curvature_coefficient: float = 0.85714,
    earth_radius_m: float = 6378137.0,
    max_distance_m: float = 30000.0,
    observer_ground_elevation_m: float | None = None,
) -> NDArray[np.generic]:
    """Return -1 unknown, 0 blocked/out of range, 1 visible at ground targets.

    Apply private clearance only to canopy before max-height composition.
    Neighboring buildings are never cleared. `grounded_observer=True` explicitly
    anchors the central source raster cell to terrain + observer height; this is
    a source model assumption, not inventory evidence of open ground. Without it,
    unknown source support propagates and nonzero source height needs qualification.
    This helper does not sample sources/targets, attenuate distance or acquire data.
    """
    from .vegetation.partial import partial_canopy_visibility

    if (
        ground.ndim != 2
        or not ground.size
        or canopy_lower.shape != ground.shape
        or canopy_unknown.shape != ground.shape
        or canopy_unknown.dtype != np.bool_
    ):
        raise ValueError("Nonempty aligned ground/canopy arrays and boolean unknown required")
    canopy = canopy_lower.copy()
    unknown = canopy_unknown.copy()
    if canopy_clearance_mask is not None:
        if (
            canopy_clearance_mask.shape != canopy.shape
            or canopy_clearance_mask.dtype != np.bool_
            or not (0 <= observer_row < canopy.shape[0] and 0 <= observer_col < canopy.shape[1])
            or not canopy_clearance_mask[observer_row, observer_col]
        ):
            raise ValueError("Aligned boolean canopy clearance must include observer")
        canopy[canopy_clearance_mask] = 0
        unknown[canopy_clearance_mask] = False
    surface = compose_above_ground_surface(
        canopy,
        unknown,
        buildings.lower_height,
        buildings.unknown,
        qualified_water=qualified_water,
        building_present=buildings.present,
    )
    central = None
    if grounded_observer:
        if not (0 <= observer_row < canopy.shape[0] and 0 <= observer_col < canopy.shape[1]):
            raise ValueError("Observer outside grid")
        central = np.zeros(canopy.shape, dtype=np.bool_)
        central[observer_row, observer_col] = True
    return partial_canopy_visibility(
        ground,
        surface.lower_height,
        surface.unknown,
        observer_row=observer_row,
        observer_col=observer_col,
        pixel_size_m=pixel_size_m,
        clearance_mask=central,
        unknown_height_lower=surface.lower_height,
        observer_height_m=observer_height_m,
        target_height_m=target_height_m,
        curvature_coefficient=curvature_coefficient,
        earth_radius_m=earth_radius_m,
        max_distance_m=max_distance_m,
        observer_ground_elevation_m=observer_ground_elevation_m,
    )
