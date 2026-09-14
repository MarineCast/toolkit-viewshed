"""Workflow identity used in artifact references and run manifests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WorkflowIdentity:
    """Names that identify a toolkit workflow and its durable dataset."""

    workflow: str
    dataset_id: str
    producer: str


DEFAULT_WORKFLOW_IDENTITY = WorkflowIdentity(
    workflow="viewshed",
    dataset_id="viewshed.static",
    producer="viewshed-toolkit",
)


def workflow_identity_from_config(raw: Mapping[str, object]) -> WorkflowIdentity:
    """Resolve an optional provenance section with reusable toolkit defaults."""

    configured = raw.get("provenance", {}) or {}
    if not isinstance(configured, Mapping):
        raise ValueError("Config section 'provenance' must be a mapping.")
    return WorkflowIdentity(
        workflow=str(configured.get("workflow", DEFAULT_WORKFLOW_IDENTITY.workflow)),
        dataset_id=str(configured.get("dataset_id", DEFAULT_WORKFLOW_IDENTITY.dataset_id)),
        producer=str(configured.get("producer", DEFAULT_WORKFLOW_IDENTITY.producer)),
    )


__all__ = [
    "DEFAULT_WORKFLOW_IDENTITY",
    "WorkflowIdentity",
    "workflow_identity_from_config",
]
