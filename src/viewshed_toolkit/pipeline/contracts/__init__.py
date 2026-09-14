"""Stable contracts shared across viewshed pipeline phases."""

from .pairs import (
    ALLOWED_CELL_TYPES,
    ALLOWED_TARGET_CELL_TYPES,
    DISTANCE_OUTPUT_SCHEMA,
    EARTH_RADIUS_KM,
    LOOKUP_ALGORITHM_VERSION,
    LOOKUP_METADATA_STEP,
    SOURCE_TARGET_LOOKUP_SCHEMA,
    SOURCE_TYPES,
)
from .artifacts import (
    FINAL_SCHEMAS,
    OBSERVATION_GEOMETRY_SCHEMA_VERSION,
    STATIC_ARTIFACT_SCHEMA_VERSION,
    VEGETATION_STATUS_COMPUTED,
    VEGETATION_STATUS_NOT_APPLICABLE,
    FinalArtifactPaths,
    final_artifact_paths,
    final_artifact_paths_from_raw,
    normalize_source_type,
    tmp_dir_for_stage,
)
from .provenance import DEFAULT_WORKFLOW_IDENTITY, WorkflowIdentity, workflow_identity_from_config

__all__ = [
    "ALLOWED_CELL_TYPES",
    "ALLOWED_TARGET_CELL_TYPES",
    "DEFAULT_WORKFLOW_IDENTITY",
    "DISTANCE_OUTPUT_SCHEMA",
    "EARTH_RADIUS_KM",
    "FINAL_SCHEMAS",
    "FinalArtifactPaths",
    "LOOKUP_ALGORITHM_VERSION",
    "LOOKUP_METADATA_STEP",
    "OBSERVATION_GEOMETRY_SCHEMA_VERSION",
    "SOURCE_TARGET_LOOKUP_SCHEMA",
    "SOURCE_TYPES",
    "STATIC_ARTIFACT_SCHEMA_VERSION",
    "VEGETATION_STATUS_COMPUTED",
    "VEGETATION_STATUS_NOT_APPLICABLE",
    "WorkflowIdentity",
    "workflow_identity_from_config",
    "final_artifact_paths",
    "final_artifact_paths_from_raw",
    "normalize_source_type",
    "tmp_dir_for_stage",
]
