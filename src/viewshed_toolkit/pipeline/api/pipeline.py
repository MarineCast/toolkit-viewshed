"""High-level, argparse-free viewshed workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..config import AppConfig
from .registry import STAGES, invocations_for_stage
from .stages import run_stage

DEFAULT_STAGES = STAGES


@dataclass(frozen=True)
class ViewshedRunResult:
    """Results returned by a complete API-driven viewshed run."""

    config: AppConfig
    stage_results: Mapping[str, tuple[object, ...]]


def execute_stages(config: AppConfig, stages: Iterable[str]) -> dict[str, tuple[object, ...]]:
    """Execute registered stages through the single API orchestration path."""

    results: dict[str, tuple[object, ...]] = {}
    for stage in stages:
        results[stage] = tuple(
            run_stage(config, invocation) for invocation in invocations_for_stage(stage)
        )
    return results


def run_viewshed(config: AppConfig) -> ViewshedRunResult:
    """Run the canonical viewshed workflow using typed Python calls only."""

    results = execute_stages(config, DEFAULT_STAGES)
    from ..finalize.final_artifacts import cleanup_viewshed_dir_to_static_outputs

    cleanup_viewshed_dir_to_static_outputs(config.config_path)
    return ViewshedRunResult(config=config, stage_results=results)
