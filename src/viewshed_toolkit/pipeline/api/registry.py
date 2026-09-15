"""Declarative registry for the canonical workflow stages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageInvocation:
    """One typed pipeline-stage request."""

    stage: str
    source_type: str | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class StageSpec:
    """Execution policy for one canonical pipeline stage."""

    name: str
    source_types: tuple[str | None, ...] = (None,)
    refresh_derived_output: bool = False

    def invocations(self) -> tuple[StageInvocation, ...]:
        return tuple(
            StageInvocation(
                self.name,
                source_type=source_type,
                overwrite=self.refresh_derived_output,
            )
            for source_type in self.source_types
        )


STAGE_SPECS: tuple[StageSpec, ...] = (
    StageSpec("download-data"),
    StageSpec("build-land-cells"),
    StageSpec("prepare-source-target-lookup"),
    StageSpec("build-distance-weights", source_types=("land", "water")),
    StageSpec("build-dual-surface-canopy-weights"),
    StageSpec("terrain-weight", source_types=("water",)),
    StageSpec("build-vegetation-path-weights", source_types=("water",)),
    StageSpec("finalize-viewshed-lookups", refresh_derived_output=True),
    StageSpec("export-static-maps", refresh_derived_output=True),
)
STAGES: tuple[str, ...] = tuple(spec.name for spec in STAGE_SPECS)
_STAGE_BY_NAME = {spec.name: spec for spec in STAGE_SPECS}

# Supplemental explicit workflow. The legacy complete-run contract remains
# available until regional equivalence and consumer migration are established.
COMPONENT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "resolve-area": (),
    "download-dem": ("resolve-area",),
    "download-chm": ("resolve-area",),
    "prepare-dem": ("download-dem",),
    "prepare-chm": ("download-chm",),
    "build-source-cells": ("resolve-area",),
    "build-target-cells": ("resolve-area",),
    "build-source-target-lookup": ("build-source-cells", "build-target-cells"),
    "build-dem-weights": ("prepare-dem", "build-source-target-lookup"),
    "build-chm-weights": ("prepare-chm", "build-dem-weights"),
    "build-distance-weights": ("build-source-target-lookup",),
    "compose-static-weights": ("build-dem-weights", "build-chm-weights", "build-distance-weights"),
    "finalize": ("compose-static-weights",),
    "validate": ("finalize",),
    "export-maps": ("validate",),
}
COMPONENT_STAGES = tuple(COMPONENT_DEPENDENCIES)


def component_plan(targets: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve only the requested dependency closure in deterministic order."""
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(stage: str) -> None:
        if stage not in COMPONENT_DEPENDENCIES:
            raise ValueError(f"Unknown component stage: {stage}")
        if stage in visiting:
            raise ValueError(f"Cyclic component dependency: {stage}")
        if stage in ordered:
            return
        visiting.add(stage)
        for dependency in COMPONENT_DEPENDENCIES[stage]:
            visit(dependency)
        visiting.remove(stage)
        ordered.append(stage)

    for target in targets:
        visit(target)
    return tuple(ordered)


def invocations_for_stage(stage: str) -> tuple[StageInvocation, ...]:
    """Return the registered invocations for one stage."""

    try:
        return _STAGE_BY_NAME[stage].invocations()
    except KeyError as exc:
        raise ValueError(f"Unknown viewshed stage: {stage}") from exc


__all__ = ["STAGES", "STAGE_SPECS", "StageInvocation", "StageSpec", "invocations_for_stage"]
