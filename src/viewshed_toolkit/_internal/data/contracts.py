from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..artifacts import ArtifactRef, RunManifest


@dataclass(frozen=True)
class ValidationReport:
    """Validation outcome for one produced dataset."""

    valid: bool
    dataset_id: str
    schema_valid: bool = True
    key_unique: bool = True
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def require_valid(self) -> None:
        if not self.valid:
            raise ValueError("; ".join(self.errors) or f"Invalid dataset: {self.dataset_id}")


@dataclass(frozen=True)
class StageResult:
    """Outputs and validation records returned by a pipeline run."""

    outputs: tuple[ArtifactRef, ...]
    validations: tuple[ValidationReport, ...]
    manifest: RunManifest | None = None
    skipped: bool = False

