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
from .pipeline.config import AppConfig, load_app_config
from .pipeline.contracts import WorkflowIdentity
from .pipeline.prepare.elevation.obstruction import compose_above_ground_surface

from .pipeline.visualization.comparison_maps import export_comparison_maps

__version__ = "0.1.0"

__all__ = [
    "compose_above_ground_surface",
    "DEFAULT_STAGES",
    "STAGES",
    "AppConfig",
    "DistanceProfile",
    "StageInvocation",
    "StageSpec",
    "ViewshedRequest",
    "ViewshedRunResult",
    "WorkflowIdentity",
    "build_distance_profile",
    "build_pair_distances",
    "export_comparison_maps",
    "load_app_config",
    "process",
    "run_component_stage",
    "run_components",
    "run_stage",
    "run_viewshed",
    "validate",
    "validate_distance_product",
]
