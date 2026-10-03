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


def validate_pair_kernel(frame, *, weight: str | None = None) -> None:
    """Reject invalid observed values/keys without materializing a regional table."""
    import polars as pl

    keys = ["source_h3", "target_h3"]
    invalid = [pl.col(key).is_null() | (pl.col(key) == "") for key in keys]
    if weight is not None:
        value = pl.col(weight).cast(pl.Float64, strict=False)
        invalid.append(value.is_null() | ~value.is_finite() | ~value.is_between(0, 1))
    if frame.filter(pl.any_horizontal(invalid)).limit(1).collect().height:
        raise ValueError("Invalid observed pair kernel: keys and finite weights in [0, 1] required")
    if frame.group_by(keys).len().filter(pl.col("len") > 1).limit(1).collect().height:
        raise ValueError("Duplicate source-target pairs in pair kernel")
