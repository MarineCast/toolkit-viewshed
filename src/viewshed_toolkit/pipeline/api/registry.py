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


def invocations_for_stage(stage: str) -> tuple[StageInvocation, ...]:
    """Return the registered invocations for one stage."""

    try:
        return _STAGE_BY_NAME[stage].invocations()
    except KeyError as exc:
        raise ValueError(f"Unknown viewshed stage: {stage}") from exc


__all__ = ["STAGES", "STAGE_SPECS", "StageInvocation", "StageSpec", "invocations_for_stage"]
