"""Schema-v3 contracts for observation/reporting-opportunity research products.

The contract deliberately separates physical viewability, observer/platform
activity, viewing conditions, and reporting capture.  It does not define a
detection probability and it does not make any field model eligible.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa

SCHEMA_VERSION = "3.0.0-research"
LEGACY_STATIC_SCHEMA_VERSION = "viewshed_static_pair_v2"


class ComponentState(StrEnum):
    POSITIVE = "positive"
    OBSERVED_ZERO = "observed_zero"
    DERIVED_ZERO = "derived_zero"
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    UNMAPPED = "unmapped"
    SOURCE_UNAVAILABLE = "source_unavailable"
    OUTSIDE_JURISDICTION = "outside_jurisdiction"
    NOT_APPLICABLE = "not_applicable"
    PROCESSING_FAILURE = "processing_failure"


class CausalRole(StrEnum):
    EXOGENOUS = "exogenous"
    GENERALLY_EXOGENOUS = "generally_exogenous"
    MIXED = "mixed"
    POTENTIALLY_ENDOGENOUS = "potentially_endogenous"
    UNKNOWN = "unknown"


class ProductRole(StrEnum):
    CANONICAL_COMPONENT = "canonical_component"
    DIAGNOSTIC = "diagnostic"
    SENSITIVITY = "sensitivity"
    COMPATIBILITY = "compatibility"


CONTROLLED_STATES = tuple(state.value for state in ComponentState)
LINEAGE_COLUMNS = (
    "GENERATION_ID",
    "CONFIG_HASH",
    "SOURCE_HASHES_JSON",
    "KNOWLEDGE_TIME_UTC",
    "SOURCE_VINTAGES_JSON",
    "HISTORICAL_RECONSTRUCTION",
)

COMPONENT_PROVENANCE_COLUMNS = (
    "COMPONENT_PROVENANCE_JSON",
    "DATA_COVERAGE_STATE",
    "SOURCE_COVERAGE_STATE",
)


class JoinCoverageReason(StrEnum):
    MATCHED = "matched"
    OUTSIDE_TEMPORAL_COVERAGE = "outside_temporal_coverage"
    OUTSIDE_SPATIAL_SUPPORT = "outside_spatial_support"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    PROCESSING_FAILURE = "processing_failure"


CONTROLLED_JOIN_REASONS = tuple(reason.value for reason in JoinCoverageReason)


@dataclass(frozen=True)
class ProductContract:
    """Required envelope for one canonical observation-opportunity product."""

    keys: tuple[str, ...]
    required_columns: tuple[str, ...]
    component_state_pairs: tuple[tuple[str, str], ...] = ()
    require_lineage: bool = True
    h3_resolution: int | None = None


STATIC_GEOMETRY_COMPONENT_PAIRS = (
    ("line_of_sight_support", "line_of_sight_state"),
    ("distance_detection_weight", "distance_detection_state"),
    ("distance_weighted_los_support", "distance_weighted_los_state"),
    ("vegetation_attenuation", "vegetation_state"),
    ("physical_viewability", "physical_viewability_state"),
    ("distance_adjusted_viewability", "distance_adjusted_viewability_state"),
)

LAND_COMPONENT_PAIRS = (
    ("PHYSICAL_VIEWABILITY_RAW", "PHYSICAL_VIEWABILITY_STATE"),
    ("DISTANCE_DETECTION_WEIGHT", "DISTANCE_DETECTION_STATE"),
    ("ATMOSPHERIC_VISIBILITY_WEIGHT", "ATMOSPHERIC_VISIBILITY_STATE"),
    ("DAYLIGHT_WEIGHT", "DAYLIGHT_STATE"),
    ("WIND_WEIGHT", "WIND_STATE"),
    ("PRECIPITATION_WEIGHT", "PRECIPITATION_STATE"),
    ("CALENDAR_CONTEXT_WEIGHT", "CALENDAR_CONTEXT_STATE"),
    (
        "POPULATION_TRAVEL_OPPORTUNITY_RAW",
        "POPULATION_TRAVEL_OPPORTUNITY_STATE",
    ),
    ("ROAD_ACCESS_OPPORTUNITY_RAW", "ROAD_ACCESS_OPPORTUNITY_STATE"),
    ("TRANSPORT_ACCESS_OPPORTUNITY_RAW", "TRANSPORT_ACCESS_OPPORTUNITY_STATE"),
    ("LAND_REACHABILITY_OPPORTUNITY_RAW", "LAND_REACHABILITY_OPPORTUNITY_STATE"),
    (
        "PUBLIC_SHORE_ACCESS_MAPPED_STATIC_CONTEXT_FRACTION",
        "PUBLIC_SHORE_ACCESS_MAPPED_STATE",
    ),
    (
        "PUBLIC_SHORE_ACCESS_VERIFIED_STATIC_CONTEXT_FRACTION",
        "PUBLIC_SHORE_ACCESS_VERIFIED_STATE",
    ),
    ("LAND_OBSERVATION_OPPORTUNITY_RAW", "LAND_OBSERVATION_OPPORTUNITY_STATE"),
    ("REPORTING_CAPTURE_WEIGHT", "REPORTING_CAPTURE_STATE"),
)

LAND_COVERAGE_COLUMNS = (
    "PHYSICAL_VIEWABILITY_CORE_COVERAGE",
    "DYNAMIC_CONDITION_COVERAGE",
    "PHYSICAL_VIEWABILITY_DYNAMIC_CONTEXT_COVERAGE",
    "POPULATION_TRAVEL_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "ROAD_ACCESS_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "CITY_ACCESS_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "TRANSPORT_ACCESS_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "LAND_REACHABILITY_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "LAND_OBSERVATION_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
    "VERIFIED_LAND_OBSERVATION_OPPORTUNITY_DYNAMIC_CONTEXT_COVERAGE",
)

WATER_COMPONENT_PAIRS = (
    ("LINE_OF_SIGHT_SUPPORT", "LINE_OF_SIGHT_STATE"),
    ("PHYSICAL_VIEWABILITY_RAW", "PHYSICAL_VIEWABILITY_STATE"),
    ("DISTANCE_DETECTION_WEIGHT", "DISTANCE_DETECTION_STATE"),
    (
        "DISTANCE_ADJUSTED_VIEWABILITY_RAW",
        "DISTANCE_ADJUSTED_VIEWABILITY_STATE",
    ),
    ("ATMOSPHERIC_VISIBILITY_WEIGHT", "ATMOSPHERIC_VISIBILITY_STATE"),
    ("DAYLIGHT_WEIGHT", "DAYLIGHT_STATE"),
    ("WIND_WEIGHT", "WIND_STATE"),
    ("PRECIPITATION_WEIGHT", "PRECIPITATION_STATE"),
    ("SEA_STATE_WEIGHT", "SEA_STATE_STATE"),
    (
        "WATER_ALL_VESSEL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_ALL_VESSEL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_PASSENGER_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_PASSENGER_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_RECREATIONAL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_RECREATIONAL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_COMMERCIAL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_COMMERCIAL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FISHING_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FISHING_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FERRY_RIDER_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FERRY_RIDER_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FERRY_PLATFORM_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FERRY_PLATFORM_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_WHALE_WATCH_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_WHALE_WATCH_OBSERVATION_OPPORTUNITY_STATE",
    ),
    ("REPORTING_CAPTURE_WEIGHT", "REPORTING_CAPTURE_STATE"),
)

WATER_SOURCE_COMPONENT_PAIRS = (
    (
        "ALL_VESSEL_AIS_ACTIVITY_HOURS_PROXY",
        "ALL_VESSEL_AIS_ACTIVITY_HOURS_PROXY_STATE",
    ),
    (
        "PASSENGER_AIS_ACTIVITY_HOURS_PROXY",
        "PASSENGER_AIS_ACTIVITY_HOURS_PROXY_STATE",
    ),
    (
        "RECREATIONAL_AIS_ACTIVITY_HOURS_PROXY",
        "RECREATIONAL_AIS_ACTIVITY_HOURS_PROXY_STATE",
    ),
    (
        "COMMERCIAL_AIS_ACTIVITY_HOURS_PROXY",
        "COMMERCIAL_AIS_ACTIVITY_HOURS_PROXY_STATE",
    ),
    (
        "FISHING_AIS_ACTIVITY_HOURS_PROXY",
        "FISHING_AIS_ACTIVITY_HOURS_PROXY_STATE",
    ),
    ("FERRY_RIDER_HOURS", "FERRY_RIDER_HOURS_STATE"),
    ("FERRY_PLATFORM_HOURS", "FERRY_PLATFORM_HOURS_STATE"),
    ("VISIBILITY_KM", "VISIBILITY_STATE"),
    ("DAYLIGHT_WEIGHT", "DAYLIGHT_STATE"),
    ("WIND_WEIGHT", "WIND_STATE"),
    ("PRECIPITATION_WEIGHT", "PRECIPITATION_STATE"),
    ("SEA_STATE_WEIGHT", "SEA_STATE_STATE"),
    ("WHALE_WATCH_ACTIVITY_HOURS", "WHALE_WATCH_ACTIVITY_HOURS_STATE"),
    ("REPORTING_CAPTURE_WEIGHT", "REPORTING_CAPTURE_STATE"),
)

WATER_COVERAGE_COLUMNS = (
    "AIS_SOURCE_TEMPORAL_COVERAGE_FRACTION",
    "AIS_TEMPORAL_COVERAGE_COMPLETE",
    "AIS_SOURCE_COVERAGE_COMPLETE",
    "AIS_SOURCE_COVERAGE_STATE",
    "AIS_TEMPORAL_COVERAGE_SCOPE",
    "AIS_SPATIAL_COVERAGE_STATE",
    "AIS_RECEIVER_COVERAGE_METHOD",
    "AIS_ABSENT_POLICY",
)

WATER_ALL_COVERAGE_COLUMNS = (
    *WATER_COVERAGE_COLUMNS,
    "FERRY_SOURCE_COVERAGE_COMPLETE",
    "FERRY_SOURCE_COVERAGE_STATE",
    "DYNAMIC_CONDITION_COVERAGE",
)

SIGHTING_DIAGNOSTIC_COMPONENT_PAIRS = (
    ("WATER_LINE_OF_SIGHT_SUPPORT", "WATER_LINE_OF_SIGHT_SUPPORT_STATE"),
    ("WATER_PHYSICAL_VIEWABILITY_RAW", "WATER_PHYSICAL_VIEWABILITY_STATE"),
    ("WATER_DISTANCE_DETECTION_WEIGHT", "WATER_DISTANCE_DETECTION_STATE"),
    (
        "WATER_DISTANCE_ADJUSTED_VIEWABILITY_RAW",
        "WATER_DISTANCE_ADJUSTED_VIEWABILITY_STATE",
    ),
    (
        "WATER_ATMOSPHERIC_VISIBILITY_WEIGHT",
        "WATER_ATMOSPHERIC_VISIBILITY_STATE",
    ),
    ("WATER_DAYLIGHT_WEIGHT", "WATER_DAYLIGHT_STATE"),
    ("WATER_WIND_WEIGHT", "WATER_WIND_STATE"),
    ("WATER_PRECIPITATION_WEIGHT", "WATER_PRECIPITATION_STATE"),
    ("WATER_SEA_STATE_WEIGHT", "WATER_SEA_STATE_STATE"),
    (
        "WATER_ALL_VESSEL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_ALL_VESSEL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_PASSENGER_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_PASSENGER_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_RECREATIONAL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_RECREATIONAL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_COMMERCIAL_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_COMMERCIAL_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FISHING_AIS_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FISHING_AIS_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FERRY_RIDER_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FERRY_RIDER_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_FERRY_PLATFORM_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_FERRY_PLATFORM_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "WATER_WHALE_WATCH_OBSERVATION_OPPORTUNITY_RAW",
        "WATER_WHALE_WATCH_OBSERVATION_OPPORTUNITY_STATE",
    ),
    ("WATER_REPORTING_CAPTURE_WEIGHT", "WATER_REPORTING_CAPTURE_STATE"),
    ("LAND_PHYSICAL_VIEWABILITY_RAW", "LAND_PHYSICAL_VIEWABILITY_STATE"),
    ("LAND_DISTANCE_DETECTION_WEIGHT", "LAND_DISTANCE_DETECTION_STATE"),
    (
        "LAND_ATMOSPHERIC_VISIBILITY_WEIGHT",
        "LAND_ATMOSPHERIC_VISIBILITY_STATE",
    ),
    ("LAND_DAYLIGHT_WEIGHT", "LAND_DAYLIGHT_STATE"),
    ("LAND_WIND_WEIGHT", "LAND_WIND_STATE"),
    ("LAND_PRECIPITATION_WEIGHT", "LAND_PRECIPITATION_STATE"),
    ("LAND_CALENDAR_CONTEXT_WEIGHT", "LAND_CALENDAR_CONTEXT_STATE"),
    (
        "LAND_POPULATION_TRAVEL_OPPORTUNITY_RAW",
        "LAND_POPULATION_TRAVEL_OPPORTUNITY_STATE",
    ),
    ("LAND_ROAD_ACCESS_OPPORTUNITY_RAW", "LAND_ROAD_ACCESS_OPPORTUNITY_STATE"),
    (
        "LAND_TRANSPORT_ACCESS_OPPORTUNITY_RAW",
        "LAND_TRANSPORT_ACCESS_OPPORTUNITY_STATE",
    ),
    (
        "LAND_REACHABILITY_OPPORTUNITY_RAW",
        "LAND_REACHABILITY_OPPORTUNITY_STATE",
    ),
    ("LAND_PUBLIC_SHORE_ACCESS_MAPPED", "LAND_PUBLIC_SHORE_ACCESS_MAPPED_STATE"),
    (
        "LAND_PUBLIC_SHORE_ACCESS_VERIFIED",
        "LAND_PUBLIC_SHORE_ACCESS_VERIFIED_STATE",
    ),
    (
        "LAND_OBSERVATION_OPPORTUNITY_RAW",
        "LAND_OBSERVATION_OPPORTUNITY_STATE",
    ),
    (
        "VERIFIED_LAND_OBSERVATION_OPPORTUNITY_RAW",
        "VERIFIED_LAND_OBSERVATION_OPPORTUNITY_STATE",
    ),
)

SIGHTING_DIAGNOSTIC_AUDIT_COLUMNS = tuple(
    column
    for value, _state in SIGHTING_DIAGNOSTIC_COMPONENT_PAIRS
    for column in (
        f"{value.removesuffix('_RAW')}_SPATIAL_RANK",
        f"{value.removesuffix('_RAW')}_MISSING_FLAG",
        f"{value.removesuffix('_RAW')}_ZERO_FLAG",
        f"{value.removesuffix('_RAW')}_LOWEST_01PCT_FLAG",
        f"{value.removesuffix('_RAW')}_LOWEST_05PCT_FLAG",
        f"{value.removesuffix('_RAW')}_LOWEST_10PCT_FLAG",
    )
)

PRODUCT_CONTRACTS: dict[str, ProductContract] = {
    "static_geometry_r7": ProductContract(
        keys=("source_h3", "target_h3"),
        required_columns=(
            "source_type",
            "distance_km",
            *(column for pair in STATIC_GEOMETRY_COMPONENT_PAIRS for column in pair),
            "legacy_static_schema",
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=STATIC_GEOMETRY_COMPONENT_PAIRS,
        require_lineage=True,
    ),
    "land_daily_r6": ProductContract(
        keys=("DATE", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            *(column for pair in LAND_COMPONENT_PAIRS for column in pair),
            *LAND_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=LAND_COMPONENT_PAIRS,
        h3_resolution=6,
    ),
    "land_weekly_r6": ProductContract(
        keys=("WEEK_START", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            "PERIOD_DAY_COUNT",
            "INCOMPLETE_WEEK",
            *(column for pair in LAND_COMPONENT_PAIRS for column in pair),
            *LAND_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=LAND_COMPONENT_PAIRS,
        h3_resolution=6,
    ),
    "water_source_daily_r7": ProductContract(
        keys=("DATE", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            "AIS_PARENT_H3_R6",
            "AIS_SPATIAL_ALLOCATION_METHOD",
            *(column for pair in WATER_SOURCE_COMPONENT_PAIRS for column in pair),
            *WATER_ALL_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=WATER_SOURCE_COMPONENT_PAIRS,
        h3_resolution=7,
    ),
    "water_source_weekly_r7": ProductContract(
        keys=("WEEK_START", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            "AIS_PARENT_H3_R6",
            "AIS_SPATIAL_ALLOCATION_METHOD",
            "PERIOD_DAY_COUNT",
            "INCOMPLETE_WEEK",
            *(column for pair in WATER_SOURCE_COMPONENT_PAIRS for column in pair),
            *WATER_ALL_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=WATER_SOURCE_COMPONENT_PAIRS,
        h3_resolution=7,
    ),
    "water_target_daily_r6": ProductContract(
        keys=("DATE", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            *(column for pair in WATER_COMPONENT_PAIRS for column in pair),
            *WATER_ALL_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=WATER_COMPONENT_PAIRS,
        h3_resolution=6,
    ),
    "water_target_weekly_r6": ProductContract(
        keys=("WEEK_START", "H3_INDEX"),
        required_columns=(
            "H3_RESOLUTION",
            "PERIOD_DAY_COUNT",
            "INCOMPLETE_WEEK",
            *(column for pair in WATER_COMPONENT_PAIRS for column in pair),
            *WATER_ALL_COVERAGE_COLUMNS,
            *COMPONENT_PROVENANCE_COLUMNS,
        ),
        component_state_pairs=WATER_COMPONENT_PAIRS,
        h3_resolution=6,
    ),
    "sighting_diagnostics": ProductContract(
        keys=("OBSERVATION_ID",),
        required_columns=(
            "DATE",
            "SIGHTING_TIMESTAMP_UTC",
            "SOURCE_TIME_PRECISION",
            "CANONICAL_TIME_SYNTHETIC",
            "H3_INDEX",
            "H3_CLUSTER_R5",
            "PLATFORM",
            "SOURCE",
            "JURISDICTION",
            "LAND_PATHWAY_EVALUATED",
            "WATER_PATHWAY_EVALUATED",
            "LAND_JOIN_COVERAGE_REASON",
            "WATER_JOIN_COVERAGE_REASON",
            *(column for pair in SIGHTING_DIAGNOSTIC_COMPONENT_PAIRS for column in pair),
            *SIGHTING_DIAGNOSTIC_AUDIT_COLUMNS,
            *WATER_COVERAGE_COLUMNS,
            "FERRY_SOURCE_COVERAGE_COMPLETE",
            "FERRY_SOURCE_COVERAGE_STATE",
            "DYNAMIC_CONDITION_COVERAGE",
            "LAND_DYNAMIC_CONDITION_COVERAGE",
            "LAND_DATA_COVERAGE_STATE",
            "LAND_SOURCE_COVERAGE_STATE",
            "SIGHTINGS_RELEASE_ID",
            "SIGHTINGS_OBSERVATIONS_SHA256",
            "SIGHTINGS_SOURCE_RECORDS_SHA256",
        ),
        component_state_pairs=SIGHTING_DIAGNOSTIC_COMPONENT_PAIRS,
    ),
}

# The compact R7 pair artifacts remain physically unchanged. This compatibility
# mapping prevents distance attenuation from being multiplied a second time.
STATIC_PAIR_CANONICAL_MAPPING = {
    "source_h3": "SOURCE_H3",
    "target_h3": "TARGET_H3",
    "weight_terrain": "TERRAIN_LOS_DISTANCE_SUPPORT",
    "weight_distance": "CENTROID_DISTANCE_DETECTION_DIAGNOSTIC",
    "weight_vegetation": "VEGETATION_SUPPORT",
    "weight_static_viewability": "DISTANCE_ADJUSTED_VIEWABILITY",
}


def static_pair_arrow_schema() -> pa.Schema:
    """Return the compatibility schema for existing compact static pair files."""

    metadata = {
        b"schema_version": LEGACY_STATIC_SCHEMA_VERSION.encode(),
        b"observation_opportunity_contract": SCHEMA_VERSION.encode(),
        b"distance_semantics": (
            b"weight_terrain already includes the configured distance curve; "
            b"weight_distance is diagnostic and must not be multiplied again"
        ),
        b"canonical_mapping": json.dumps(
            STATIC_PAIR_CANONICAL_MAPPING, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    return pa.schema(
        [
            pa.field("source_h3", pa.string(), nullable=False),
            pa.field("target_h3", pa.string(), nullable=False),
            pa.field("weight_terrain", pa.float32(), nullable=False),
            pa.field("weight_distance", pa.float32(), nullable=False),
            pa.field("weight_vegetation", pa.float32(), nullable=False),
            pa.field("weight_static_viewability", pa.float32(), nullable=False),
        ],
        metadata=metadata,
    )


def static_geometry_arrow_schema() -> pa.Schema:
    """Return the forward-only component-rich static geometry schema."""

    metadata = {
        b"schema_version": SCHEMA_VERSION.encode(),
        b"grain": b"source_h3_x_target_h3",
        b"distance_semantics": (
            b"distance_weighted_los_support is integrated over sampled LOS distances; "
            b"distance_detection_weight is the centroid diagnostic and is not multiplied again"
        ),
        b"legacy_compatibility": (
            b"weight_terrain and weight_static_viewability remain in separate v2 compatibility artifacts"
        ),
    }
    return pa.schema(
        [
            pa.field("source_h3", pa.string(), nullable=False),
            pa.field("target_h3", pa.string(), nullable=False),
            pa.field("source_type", pa.string(), nullable=False),
            pa.field("distance_km", pa.float32(), nullable=False),
            pa.field("line_of_sight_support", pa.float32()),
            pa.field("line_of_sight_state", pa.string(), nullable=False),
            pa.field("distance_detection_weight", pa.float32(), nullable=False),
            pa.field("distance_detection_state", pa.string(), nullable=False),
            pa.field("distance_weighted_los_support", pa.float32(), nullable=False),
            pa.field("distance_weighted_los_state", pa.string(), nullable=False),
            pa.field("vegetation_attenuation", pa.float32()),
            pa.field("vegetation_state", pa.string(), nullable=False),
            pa.field("physical_viewability", pa.float32()),
            pa.field("physical_viewability_state", pa.string(), nullable=False),
            pa.field("distance_adjusted_viewability", pa.float32(), nullable=False),
            pa.field("distance_adjusted_viewability_state", pa.string(), nullable=False),
            pa.field("legacy_static_schema", pa.bool_(), nullable=False),
            pa.field("COMPONENT_PROVENANCE_JSON", pa.string(), nullable=False),
            pa.field("DATA_COVERAGE_STATE", pa.string(), nullable=False),
            pa.field("SOURCE_COVERAGE_STATE", pa.string(), nullable=False),
            pa.field("GENERATION_ID", pa.string(), nullable=False),
            pa.field("CONFIG_HASH", pa.string(), nullable=False),
            pa.field("SOURCE_HASHES_JSON", pa.string(), nullable=False),
            pa.field("KNOWLEDGE_TIME_UTC", pa.string(), nullable=False),
            pa.field("SOURCE_VINTAGES_JSON", pa.string(), nullable=False),
            pa.field("HISTORICAL_RECONSTRUCTION", pa.bool_(), nullable=False),
        ],
        metadata=metadata,
    )


def source_hashes_json(paths: Mapping[str, str]) -> str:
    """Serialize a deterministic name-to-checksum mapping for row lineage."""

    return json.dumps(dict(sorted(paths.items())), sort_keys=True, separators=(",", ":"))


def source_vintages_json(vintages: Mapping[str, Any]) -> str:
    """Serialize source vintage/coverage metadata without inventing dates."""

    return json.dumps(
        dict(sorted(vintages.items())), sort_keys=True, separators=(",", ":"), default=str
    )


def lineage_values(
    *,
    generation_id: str,
    config_hash: str,
    source_hashes: Mapping[str, str],
    source_vintages: Mapping[str, Any],
    knowledge_time_utc: str | None = None,
    historical_reconstruction: bool = True,
) -> dict[str, Any]:
    """Return the six required lineage values in their canonical representation."""

    return {
        "GENERATION_ID": str(generation_id),
        "CONFIG_HASH": str(config_hash),
        "SOURCE_HASHES_JSON": source_hashes_json(source_hashes),
        "KNOWLEDGE_TIME_UTC": knowledge_time_utc
        or datetime.now(UTC).replace(microsecond=0).isoformat(),
        "SOURCE_VINTAGES_JSON": source_vintages_json(source_vintages),
        "HISTORICAL_RECONSTRUCTION": bool(historical_reconstruction),
    }


def component_provenance_json(components: Mapping[str, Mapping[str, Any]]) -> str:
    """Serialize component-specific vintage and reconstruction statements."""

    normalized: dict[str, dict[str, Any]] = {}
    for name, payload in sorted(components.items()):
        values = dict(payload)
        required = {"source_vintage", "knowledge_time_utc", "historical_reconstruction"}
        missing = sorted(required.difference(values))
        if missing:
            raise ValueError(f"Component provenance for {name!r} is missing: {missing}")
        normalized[str(name)] = values
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)


def add_lineage_polars(frame: pl.DataFrame, values: Mapping[str, Any]) -> pl.DataFrame:
    missing = sorted(set(LINEAGE_COLUMNS).difference(values))
    if missing:
        raise ValueError(f"Missing lineage values: {missing}")
    return frame.with_columns(*(pl.lit(values[name]).alias(name) for name in LINEAGE_COLUMNS))


def add_lineage_pandas(frame: pd.DataFrame, values: Mapping[str, Any]) -> pd.DataFrame:
    missing = sorted(set(LINEAGE_COLUMNS).difference(values))
    if missing:
        raise ValueError(f"Missing lineage values: {missing}")
    result = frame.copy()
    for name in LINEAGE_COLUMNS:
        result[name] = values[name]
    return result


def component_state(
    value: float | int | None,
    *,
    coverage_complete: bool,
    derived: bool,
    available: bool = True,
    partial_positive: bool = True,
) -> str:
    """Classify one component value without treating unavailable data as zero."""

    if not available or value is None or pd.isna(value):
        return ComponentState.UNKNOWN.value
    numeric = float(value)
    if numeric < 0:
        return ComponentState.PROCESSING_FAILURE.value
    if not coverage_complete and (numeric == 0 or partial_positive):
        return ComponentState.PARTIAL.value
    if numeric > 0:
        return ComponentState.POSITIVE.value
    return ComponentState.DERIVED_ZERO.value if derived else ComponentState.OBSERVED_ZERO.value


def validate_component_table(
    frame: pl.DataFrame,
    *,
    keys: Sequence[str],
    component_state_columns: Sequence[str],
    required_lineage: bool = True,
) -> None:
    """Validate keys, controlled states, and the schema-v2 lineage envelope."""

    required = set(keys) | set(component_state_columns)
    if required_lineage:
        required.update(LINEAGE_COLUMNS)
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Component table is missing columns: {missing}")
    if frame.select(pl.struct(list(keys)).is_duplicated().sum()).item():
        raise ValueError(f"Component table keys are not unique: {list(keys)}")
    for column in component_state_columns:
        invalid = frame.filter(
            pl.col(column).is_null() | ~pl.col(column).is_in(CONTROLLED_STATES)
        ).height
        if invalid:
            raise ValueError(f"{column} contains {invalid} invalid component states.")
    if required_lineage:
        for column in LINEAGE_COLUMNS[:-1]:
            if frame.get_column(column).null_count():
                raise ValueError(f"{column} contains null lineage values.")


_NULL_REQUIRED_STATES = frozenset(
    {
        ComponentState.UNKNOWN.value,
        ComponentState.UNMAPPED.value,
        ComponentState.SOURCE_UNAVAILABLE.value,
        ComponentState.OUTSIDE_JURISDICTION.value,
        ComponentState.NOT_APPLICABLE.value,
        ComponentState.PROCESSING_FAILURE.value,
    }
)


def validate_component_state_values(
    frame: pl.DataFrame,
    *,
    pairs: Sequence[tuple[str, str]],
) -> None:
    """Reject value/state contradictions without collapsing partial evidence."""

    for value_column, state_column in pairs:
        missing = [name for name in (value_column, state_column) if name not in frame.columns]
        if missing:
            raise ValueError(f"Missing component value/state columns: {missing}")
        state = pl.col(state_column)
        value = pl.col(value_column)
        invalid = frame.filter(
            state.is_null()
            | ~state.is_in(CONTROLLED_STATES)
            | (state.is_in(_NULL_REQUIRED_STATES) & value.is_not_null())
            | (state.eq(ComponentState.POSITIVE.value) & (value.is_null() | (value <= 0.0)))
            | (
                state.is_in([ComponentState.OBSERVED_ZERO.value, ComponentState.DERIVED_ZERO.value])
                & (value.is_null() | (value != 0.0))
            )
        ).height
        if invalid:
            raise ValueError(
                f"{value_column}/{state_column} contains {invalid} value-state contradictions."
            )


def validate_product_contract(frame: pl.DataFrame, product: str) -> None:
    """Validate one named schema-v3 product contract."""

    try:
        contract = PRODUCT_CONTRACTS[product]
    except KeyError as exc:
        raise KeyError(f"Unknown observation-opportunity product contract: {product}") from exc
    required = set(contract.keys) | set(contract.required_columns)
    if contract.require_lineage:
        required.update(LINEAGE_COLUMNS)
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{product} is missing contract columns: {missing}")
    validate_component_table(
        frame,
        keys=contract.keys,
        component_state_columns=[state for _value, state in contract.component_state_pairs],
        required_lineage=contract.require_lineage,
    )
    validate_component_state_values(frame, pairs=contract.component_state_pairs)
    if contract.h3_resolution is not None:
        invalid_resolution = frame.filter(pl.col("H3_RESOLUTION") != contract.h3_resolution).height
        if invalid_resolution:
            raise ValueError(
                f"{product} contains {invalid_resolution} rows at the wrong H3 resolution."
            )
    if product == "sighting_diagnostics":
        for column in ("LAND_JOIN_COVERAGE_REASON", "WATER_JOIN_COVERAGE_REASON"):
            invalid = frame.filter(
                pl.col(column).is_null() | ~pl.col(column).is_in(CONTROLLED_JOIN_REASONS)
            ).height
            if invalid:
                raise ValueError(f"{column} contains {invalid} invalid join reasons.")
