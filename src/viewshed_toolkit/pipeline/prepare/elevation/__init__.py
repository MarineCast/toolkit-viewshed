"""DEM acquisition and terrain/canopy raster preparation."""

from .acquire import DemDownloadResult, download_dem_for_config
from .canopy import (
    CanopySurfaceResult,
    build_canonical_canopy_base_surface,
    build_canopy_obstacle_surface,
    build_observer_grounded_canopy_surface_from_base,
)

__all__ = [
    "CanopySurfaceResult",
    "DemDownloadResult",
    "build_canopy_obstacle_surface",
    "build_canonical_canopy_base_surface",
    "build_observer_grounded_canopy_surface_from_base",
    "download_dem_for_config",
]
