"""Canonical source-target and factor-table contracts."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

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


def validate_pair_kernel(frame: "pl.LazyFrame", *, weight: str | None = None) -> None:
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


LOS_NUMERICAL_TOLERANCE = 1e-6


def validate_los_diagnostics(frame: "pl.LazyFrame") -> None:
    """Validate observed LOS before coercion; optional absent legacy fields stay absent."""
    import polars as pl

    columns = set(frame.collect_schema().names())
    bounded = {
        "joint_los_fraction",
        "distance_weighted_los_fraction",
        "any_observer_support_fraction",
        "union_visible_target_fraction",
        "observer_sample_fraction",
        "visible_area_fraction",
    }
    counts = {
        "sample_points_requested",
        "sample_points_actual",
        "n_observers",
        "target_water_pixel_count",
        "target_water_sample_count",
        "pixel_stride",
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
    }
    nonnegative = counts | {"los_distance_weight_sum", "visible_area_km2", "target_water_area_km2"}
    invalid = []
    for name in sorted(columns & (bounded | nonnegative)):
        value = pl.col(name).cast(pl.Float64, strict=False)
        bad = value.is_null() | ~value.is_finite() | (value < 0)
        if name in bounded:
            bad = bad | (value > 1)
        if name in counts:
            bad = bad | (value != value.floor())
        if name in {"sample_points_requested", "sample_points_actual"}:
            # Optional design metadata can be absent; numerical LOS evidence cannot.
            bad = bad & pl.col(name).is_not_null()
        if name == "joint_los_fraction" and "unweighted_los_observed" in columns:
            bad = bad & pl.col("unweighted_los_observed")
        invalid.append(bad)
    if invalid and frame.filter(pl.any_horizontal(invalid)).limit(1).collect().height:
        raise ValueError("Invalid observed LOS diagnostic: finite values and valid bounds required")
    if {"joint_los_fraction", "distance_weighted_los_fraction"} <= columns:
        bad = (
            pl.col("distance_weighted_los_fraction").cast(pl.Float64, strict=False)
            > pl.col("joint_los_fraction").cast(pl.Float64, strict=False) + LOS_NUMERICAL_TOLERANCE
        )
        if frame.filter(bad).limit(1).collect().height:
            raise ValueError("LOS diagnostic integrated support exceeds unweighted support")
