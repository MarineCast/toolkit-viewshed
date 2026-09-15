"""Callable Python API for the viewshed workflow."""

from ..weights.distance.products import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    validate_distance_product,
)
from .pipeline import (
    DEFAULT_STAGES,
    ViewshedRunResult,
    run_viewshed,
)
from .registry import StageSpec
from .service import STAGES, ViewshedRequest, process, validate
from .stages import StageInvocation, run_stage

__all__ = [
    "DEFAULT_STAGES",
    "STAGES",
    "DistanceProfile",
    "StageInvocation",
    "StageSpec",
    "ViewshedRequest",
    "ViewshedRunResult",
    "build_distance_profile",
    "build_pair_distances",
    "process",
    "run_stage",
    "run_viewshed",
    "validate",
    "validate_distance_product",
]
