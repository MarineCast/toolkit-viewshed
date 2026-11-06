"""Reusable tools for geospatial viewshed and visibility modeling."""

from .pipeline.api import (
    DEFAULT_STAGES,
    STAGES,
    DistanceProfile,
    StageInvocation,
    StageSpec,
    ViewshedRequest,
    ViewshedRunResult,
    build_distance_profile,
    build_pair_distances,
    process,
    run_stage,
    run_viewshed,
    validate,
    validate_distance_product,
)
from .pipeline.api.components import run_component_stage, run_components
from .pipeline.api.transmission import run_transmission
from .pipeline.api.transmission_tile_workflow import run_transmission_tiles
from .pipeline.config import AppConfig, load_app_config
from .pipeline.config.transmission import TransmissionConfig, load_transmission_config
from .pipeline.config.transmission_tiles import (
    TransmissionTileConfig,
    load_transmission_tile_config,
)
from .pipeline.contracts import WorkflowIdentity
from .pipeline.finalize.transmission import validate_transmission_product
from .pipeline.prepare.elevation.buildings import rasterize_building_footprints
from .pipeline.prepare.elevation.obstruction import compose_above_ground_surface
from .pipeline.visualization.comparison_maps import export_comparison_maps, export_pilot_map
from .pipeline.visualization.raster_overlays import export_raster_overlay_asset
from .pipeline.weights.obstruction import above_ground_visibility
from .pipeline.weights.rays import direct_ray_envelope_visibility, direct_ray_visibility

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_STAGES",
    "STAGES",
    "AppConfig",
    "DistanceProfile",
    "StageInvocation",
    "StageSpec",
    "TransmissionConfig",
    "TransmissionTileConfig",
    "ViewshedRequest",
    "ViewshedRunResult",
    "WorkflowIdentity",
    "above_ground_visibility",
    "build_distance_profile",
    "build_pair_distances",
    "compose_above_ground_surface",
    "direct_ray_envelope_visibility",
    "direct_ray_visibility",
    "export_comparison_maps",
    "export_pilot_map",
    "export_raster_overlay_asset",
    "load_app_config",
    "load_transmission_config",
    "load_transmission_tile_config",
    "process",
    "rasterize_building_footprints",
    "run_component_stage",
    "run_components",
    "run_stage",
    "run_transmission",
    "run_transmission_tiles",
    "run_viewshed",
    "validate",
    "validate_distance_product",
    "validate_transmission_product",
]
