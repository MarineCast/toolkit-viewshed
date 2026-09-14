"""Canonical source-target and factor-table contracts."""

SOURCE_TYPES: frozenset[str] = frozenset({"land", "water"})
ALLOWED_CELL_TYPES: frozenset[str] = frozenset({"land", "water", "mixed", "excluded"})
ALLOWED_TARGET_CELL_TYPES: frozenset[str] = frozenset({"water", "mixed"})

SOURCE_TARGET_LOOKUP_SCHEMA: tuple[str, ...] = (
    "source_h3",
    "target_h3",
    "distance_km",
    "source_type",
)
DISTANCE_OUTPUT_SCHEMA: tuple[str, ...] = (
    "source_h3",
    "target_h3",
    "distance_km",
    "weight_distance",
)

LOOKUP_ALGORITHM_VERSION = "source_target_lookup_buffered_targets_v4"
LOOKUP_METADATA_STEP = LOOKUP_ALGORITHM_VERSION
EARTH_RADIUS_KM = 6371.0088

__all__ = [
    "ALLOWED_CELL_TYPES",
    "ALLOWED_TARGET_CELL_TYPES",
    "DISTANCE_OUTPUT_SCHEMA",
    "EARTH_RADIUS_KM",
    "LOOKUP_ALGORITHM_VERSION",
    "LOOKUP_METADATA_STEP",
    "SOURCE_TARGET_LOOKUP_SCHEMA",
    "SOURCE_TYPES",
]
