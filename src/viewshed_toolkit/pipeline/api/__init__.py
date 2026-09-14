"""Callable Python API for the viewshed workflow."""

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
    "StageInvocation",
    "StageSpec",
    "ViewshedRequest",
    "ViewshedRunResult",
    "process",
    "run_stage",
    "run_viewshed",
    "validate",
]
