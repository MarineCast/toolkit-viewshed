"""Compose, materialize, and validate durable viewshed artifacts.

Shared schemas, paths, Parquet persistence, and cleanup policy live in
``pipeline.contracts`` and ``_internal.data``. This module owns the scientific
composition and metadata validation performed during finalization.

Working Parquet products
------------------------
Lookup, shared by land and water source workflows under ``paths.output_dir``:
- lookup/SOURCE_TARGET_LOOKUP_H3R{res}.parquet
    source_h3, target_h3, distance_km, source_type
- lookup/TARGET_WATER_AREA_H3R{res}.parquet
    target_h3, total_water_area_m2, equivalent_water_pixel_count
    Immutable terrain denominator calculated from complete H3-cell intersections
    with the canonical water polygon in an equal-area CRS. The equivalent pixel
    count is the area divided by the configured nominal DEM pixel area; it is
    not a count sampled from a particular raster grid.

Land-source factors under ``paths.output_dir``:
- land/weights_files/DISTANCE_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, distance_km, weight_distance
- land/weights_files/TERRAIN_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, weight_terrain
- land/weights_files/VEGETATION_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, source_type, weight_vegetation, vegetation_status

Water-source factors under ``paths.output_dir``:
- ocean/weights_files/DISTANCE_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, distance_km, weight_distance
- ocean/weights_files/TERRAIN_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, weight_terrain
- ocean/weights_files/VEGETATION_WEIGHTS_H3R{res}.parquet
    source_h3, target_h3, source_type, weight_vegetation, vegetation_status

The durable ``paths.final_output_dir`` contract contains only
``LAND_STATIC_WEIGHTS_R{res}.parquet`` and
``WATER_STATIC_WEIGHTS_R{res}.parquet``. Working tables and diagnostics are
removed after a successful complete build.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl
import pyarrow.parquet as pq

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.data.parquet import (
    atomic_sink_parquet,
    scan_required,
    validate_parquet_schema,
)

from ..config.paths import (
    get_dem_settings,
    load_metadata_sidecar,
    stable_config_hash,
    write_metadata_sidecar,
)
from ..config.schema import load_yaml
from ..contracts.artifacts import (
    FINAL_SCHEMAS,
    OBSERVATION_GEOMETRY_SCHEMA_VERSION,
    STATIC_ARTIFACT_SCHEMA_VERSION,
    VEGETATION_STATUS_COMPUTED,
    VEGETATION_STATUS_NOT_APPLICABLE,
    FinalArtifactPaths,
    final_artifact_paths,
    final_artifact_paths_from_raw,
    normalize_source_type,
)
from ..contracts.cleanup import (
    cleanup_output_tree,
    metadata_sidecars_for,
)
from ..contracts.pairs import SOURCE_TYPES


def materialize_distance_weights(
    input_paths: Sequence[Path],
    config_path: str | Path,
    *,
    overwrite: bool,
    source_type: str = "land",
) -> tuple[Path, int]:
    """Materialize the minimal distance-factor artifact for a source type."""

    source_type = normalize_source_type(source_type)
    paths = final_artifact_paths(config_path)
    output_path = paths.weights_path("distance_weights", source_type=source_type)

    lf = (
        scan_required(input_paths, FINAL_SCHEMAS["distance_weights"])
        .with_columns(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
            pl.col("distance_km").cast(pl.Float32, strict=False),
            pl.col("weight_distance").cast(pl.Float32, strict=False).fill_null(0.0).clip(0.0, 1.0),
        )
        .select(list(FINAL_SCHEMAS["distance_weights"]))
    )

    rows = atomic_sink_parquet(lf, output_path, overwrite=overwrite)
    validate_parquet_schema(output_path, FINAL_SCHEMAS["distance_weights"])
    return output_path, rows


def _validate_unit_interval_columns(
    lf: pl.LazyFrame,
    columns: Sequence[str],
    *,
    label: str,
) -> pl.LazyFrame:
    """Cast unit-interval factors and reject null, malformed, or non-finite values."""

    casted = lf.with_columns(
        [pl.col(column).cast(pl.Float32, strict=False).alias(column) for column in columns]
    )
    invalid_expr = pl.any_horizontal(
        [
            pl.col(column).is_null()
            | pl.col(column).is_nan()
            | pl.col(column).is_infinite()
            | (pl.col(column) < 0.0)
            | (pl.col(column) > 1.0)
            for column in columns
        ]
    )
    invalid = casted.filter(invalid_expr)
    invalid_count = invalid.select(pl.len()).collect().item()
    if invalid_count:
        sample_columns = [
            column
            for column in ("source_h3", "target_h3", *columns)
            if column in casted.collect_schema().names()
        ]
        sample = invalid.select(sample_columns).limit(10).collect().to_dicts()
        raise ValueError(
            f"{label} contains {invalid_count} null, malformed, non-finite, or "
            f"out-of-range value(s): {sample}"
        )
    return casted


def _validate_vegetation_frame(
    lf: pl.LazyFrame,
    *,
    expected_source_type: str,
) -> pl.LazyFrame:
    """Enforce the semantic vegetation contract for one source type."""

    expected_source_type = normalize_source_type(expected_source_type)
    expected_status = (
        VEGETATION_STATUS_COMPUTED
        if expected_source_type == "land"
        else VEGETATION_STATUS_NOT_APPLICABLE
    )
    normalized = lf.with_columns(
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
        pl.col("source_type").cast(pl.Utf8).str.to_lowercase(),
        pl.col("vegetation_status").cast(pl.Utf8).str.to_lowercase(),
    )
    bad_contract = normalized.filter(
        (pl.col("source_type") != expected_source_type)
        | pl.col("source_type").is_null()
        | (pl.col("vegetation_status") != expected_status)
        | pl.col("vegetation_status").is_null()
    )
    bad_count = bad_contract.select(pl.len()).collect().item()
    if bad_count:
        sample = (
            bad_contract.select("source_h3", "target_h3", "source_type", "vegetation_status")
            .limit(10)
            .collect()
            .to_dicts()
        )
        raise ValueError(
            f"{expected_source_type} vegetation requires source_type="
            f"{expected_source_type!r} and vegetation_status={expected_status!r}; "
            f"found {bad_count} invalid row(s): {sample}"
        )
    normalized = _validate_unit_interval_columns(
        normalized,
        ["weight_vegetation"],
        label=f"{expected_source_type} vegetation",
    )
    if expected_source_type == "water":
        non_neutral = normalized.filter(pl.col("weight_vegetation") != 1.0)
        non_neutral_count = non_neutral.select(pl.len()).collect().item()
        if non_neutral_count:
            sample = (
                non_neutral.select("source_h3", "target_h3", "weight_vegetation")
                .limit(10)
                .collect()
                .to_dicts()
            )
            raise ValueError(
                "Water vegetation marked not_applicable must have neutral "
                f"weight_vegetation=1.0; found {non_neutral_count} invalid row(s): {sample}"
            )
    return normalized.select(list(FINAL_SCHEMAS["vegetation_weights"]))


def _vegetation_weight_frame(input_paths: Sequence[Path], *, source_type: str) -> pl.LazyFrame:
    """Return a canonical vegetation-weight lazy frame.

    The new compact contract is weight_vegetation. To keep this materializer
    useful while the vegetation stage catches up, it also accepts legacy
    weight_landcover + weight_chm inputs and composes them into
    weight_vegetation. The output schema is always compact.
    """

    paths_in = [Path(p) for p in input_paths]
    if not paths_in:
        raise FileNotFoundError("No vegetation input paths were supplied.")
    missing = [str(p) for p in paths_in if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing vegetation inputs:\n" + "\n".join(missing[:20]))

    lf = pl.scan_parquet([str(p) for p in paths_in])
    schema_names = set(lf.collect_schema().names())
    required_keys = {"source_h3", "target_h3"}
    missing_keys = sorted(required_keys - schema_names)
    if missing_keys:
        raise ValueError(f"Missing required columns {missing_keys} in {paths_in[0]}")

    source_type = normalize_source_type(source_type)
    if "weight_vegetation" in schema_names:
        selected = ["source_h3", "target_h3", "weight_vegetation"]
        expressions: list[pl.Expr] = []
        if "source_type" in schema_names:
            selected.append("source_type")
        else:
            expressions.append(pl.lit(source_type).alias("source_type"))
        if "vegetation_status" in schema_names:
            selected.append("vegetation_status")
        else:
            status = (
                VEGETATION_STATUS_COMPUTED
                if source_type == "land"
                else VEGETATION_STATUS_NOT_APPLICABLE
            )
            expressions.append(pl.lit(status).alias("vegetation_status"))
        direct = lf.select(selected)
        if expressions:
            direct = direct.with_columns(expressions)
        return _validate_vegetation_frame(direct, expected_source_type=source_type)

    legacy = {"weight_landcover", "weight_chm"}
    missing_legacy = sorted(legacy - schema_names)
    if missing_legacy:
        raise ValueError(
            "Vegetation inputs must contain either weight_vegetation or both "
            f"weight_landcover and weight_chm. Missing {missing_legacy} in {paths_in[0]}"
        )

    if source_type != "land":
        raise ValueError(
            "Legacy weight_landcover/weight_chm inputs are valid only for land "
            "vegetation; water vegetation must be explicitly not_applicable."
        )

    legacy_frame = lf.select(
        ["source_h3", "target_h3", "weight_landcover", "weight_chm"]
    ).with_columns(
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
    )
    legacy_frame = _validate_unit_interval_columns(
        legacy_frame,
        ["weight_landcover", "weight_chm"],
        label="land vegetation components",
    )
    return _validate_vegetation_frame(
        legacy_frame.select(
            [
                "source_h3",
                "target_h3",
                pl.lit("land").alias("source_type"),
                (pl.col("weight_landcover") * pl.col("weight_chm"))
                .cast(pl.Float32)
                .alias("weight_vegetation"),
                pl.lit(VEGETATION_STATUS_COMPUTED).alias("vegetation_status"),
            ]
        ),
        expected_source_type="land",
    )


def materialize_vegetation_weights_from_mapping(
    input_paths: Sequence[Path],
    raw_config: Mapping[str, object],
    *,
    config_dir: str | Path,
    overwrite: bool,
    source_type: str = "land",
) -> tuple[Path, int]:
    """Materialize vegetation weights using an in-memory viewshed config."""

    source_type = normalize_source_type(source_type)
    paths = final_artifact_paths_from_raw(
        raw_config,
        Path(config_dir).expanduser().resolve(),
    )
    output_path = paths.weights_path("vegetation_weights", source_type=source_type)
    lf = _vegetation_weight_frame(input_paths, source_type=source_type)
    rows = atomic_sink_parquet(lf, output_path, overwrite=overwrite)
    validate_parquet_schema(output_path, FINAL_SCHEMAS["vegetation_weights"])
    return output_path, rows


def _require_parquet_inputs(paths: Sequence[Path], *, label: str) -> None:
    missing = [str(Path(path)) for path in paths if not Path(path).exists()]
    if missing:
        raise FileNotFoundError(f"Missing {label} input(s):\n" + "\n".join(missing[:20]))


def _static_weight_expr(
    terrain_col: str = "weight_terrain",
    distance_col: str = "weight_distance",
    vegetation_col: str | None = None,
) -> pl.Expr:
    """Compose a distance-integrated terrain kernel with optional canopy loss.

    ``distance_col`` remains in the signature for compatibility with existing
    artifact callers, but it is a centroid diagnostic and is intentionally not
    multiplied into the physical kernel.
    """
    del distance_col
    expr = pl.col(terrain_col)
    if vegetation_col is not None:
        expr = expr * pl.col(vegetation_col)
    return expr.clip(0.0, 1.0).cast(pl.Float32).alias("weight_static_viewability")


def _scan_factor(path: Path, columns: Sequence[str]) -> pl.LazyFrame:
    lf = scan_required(path, columns)
    casts = [
        pl.col("source_h3").cast(pl.Utf8),
        pl.col("target_h3").cast(pl.Utf8),
    ]
    for column in columns:
        if column not in {"source_h3", "target_h3"}:
            casts.append(pl.col(column).cast(pl.Float32, strict=False))
    normalized = lf.with_columns(casts).select(list(columns))
    factor_columns = [c for c in columns if c not in {"source_h3", "target_h3"}]
    return _validate_unit_interval_columns(
        normalized, factor_columns, label=f"factor artifact {path}"
    )


def _scan_vegetation_factor(path: Path, *, source_type: str) -> pl.LazyFrame:
    lf = scan_required(path, FINAL_SCHEMAS["vegetation_weights"])
    return _validate_vegetation_frame(lf, expected_source_type=source_type)


def _pair_key_stats(frame: pl.LazyFrame) -> dict[str, int]:
    keys = ["source_h3", "target_h3"]
    return {
        key: int(value)
        for key, value in frame.select(
            pl.len().alias("rows"),
            pl.struct(keys).n_unique().alias("unique_pairs"),
        )
        .collect()
        .row(0, named=True)
        .items()
    }


def _pair_sample(frame: pl.LazyFrame, limit: int = 10) -> list[dict[str, str]]:
    return frame.select("source_h3", "target_h3").limit(limit).collect().to_dicts()


def _validate_pair_factor_coverage(
    universe: pl.LazyFrame,
    factor: pl.LazyFrame,
    *,
    label: str,
    require_complete: bool,
) -> dict[str, int | list[dict[str, str]]]:
    """Validate uniqueness and coverage against the authoritative lookup."""

    keys = ["source_h3", "target_h3"]
    universe_stats = _pair_key_stats(universe)
    factor_stats = _pair_key_stats(factor)
    if universe_stats["rows"] != universe_stats["unique_pairs"]:
        raise ValueError(
            "Canonical source-target lookup contains duplicate keys: " f"{universe_stats}"
        )
    if factor_stats["rows"] != factor_stats["unique_pairs"]:
        raise ValueError(f"{label} contains duplicate source-target keys: {factor_stats}")

    universe_keys = universe.select(keys)
    factor_keys = factor.select(keys)
    missing = universe_keys.join(factor_keys, on=keys, how="anti")
    extra = factor_keys.join(universe_keys, on=keys, how="anti")
    missing_count = _pair_key_stats(missing)["rows"]
    extra_count = _pair_key_stats(extra)["rows"]
    report: dict[str, int | list[dict[str, str]]] = {
        "universe_rows": universe_stats["rows"],
        "factor_rows": factor_stats["rows"],
        "missing_pair_count": missing_count,
        "extra_pair_count": extra_count,
        "missing_pair_sample": _pair_sample(missing) if missing_count else [],
        "extra_pair_sample": _pair_sample(extra) if extra_count else [],
    }
    if extra_count or (require_complete and missing_count):
        policy = "exact coverage" if require_complete else "no extra keys"
        raise ValueError(
            f"{label} violates the canonical pair-universe policy ({policy}): "
            f"{json.dumps(report, sort_keys=True)}"
        )
    return report


def build_static_viewability_lazy(
    config_path: str | Path,
    *,
    source_type: str,
) -> tuple[pl.LazyFrame, dict[str, object]]:
    """Compose one source type on the canonical lookup with explicit missingness.

    All three factor artifacts must cover the lookup exactly. The terrain
    combine stage is responsible for converting its validated sparse
    partitions into a dense factor only after every lookup source has a
    matching partition. Finalization therefore treats a missing terrain key as
    an incomplete build, never as physical zero.
    """

    source_type = normalize_source_type(source_type)
    paths = final_artifact_paths(config_path)
    keys = ["source_h3", "target_h3"]
    lookup = (
        scan_required(paths.source_target_lookup, FINAL_SCHEMAS["source_target_lookup"])
        .filter(pl.col("source_type") == source_type)
        .select(
            pl.col("source_h3").cast(pl.Utf8),
            pl.col("target_h3").cast(pl.Utf8),
        )
    )
    if source_type == "land":
        terrain_path = paths.terrain_weights
        distance_path = paths.distance_weights
        vegetation_path = paths.vegetation_weights
    else:
        terrain_path = paths.ocean_terrain_weights
        distance_path = paths.ocean_distance_weights
        vegetation_path = paths.ocean_vegetation_weights
    _require_parquet_inputs(
        [terrain_path, distance_path, vegetation_path],
        label=f"{source_type} static viewability",
    )
    terrain = _scan_factor(terrain_path, [*keys, "weight_terrain"])
    distance = _scan_factor(distance_path, [*keys, "weight_distance"])
    vegetation = _scan_vegetation_factor(vegetation_path, source_type=source_type).select(
        *keys, "weight_vegetation"
    )
    coverage = {
        "source_type": source_type,
        "missingness_policy": {
            "terrain": "exact_dense_coverage_required",
            "distance": "exact_coverage_required",
            "vegetation": "exact_coverage_required",
        },
        "terrain": _validate_pair_factor_coverage(
            lookup, terrain, label=f"{source_type} terrain", require_complete=True
        ),
        "distance": _validate_pair_factor_coverage(
            lookup, distance, label=f"{source_type} distance", require_complete=True
        ),
        "vegetation": _validate_pair_factor_coverage(
            lookup,
            vegetation,
            label=f"{source_type} vegetation",
            require_complete=True,
        ),
    }
    composed = (
        lookup.join(terrain, on=keys, how="left")
        .join(distance, on=keys, how="left")
        .join(vegetation, on=keys, how="left")
        .with_columns(
            pl.col("weight_terrain").cast(pl.Float32),
            pl.col("weight_distance").cast(pl.Float32),
            pl.col("weight_vegetation").cast(pl.Float32),
        )
        .with_columns(_static_weight_expr(vegetation_col="weight_vegetation"))
        .select(
            "source_h3",
            "target_h3",
            "weight_terrain",
            "weight_distance",
            "weight_vegetation",
            "weight_static_viewability",
        )
    )
    return composed, coverage


def _controlled_weight_state(column: str, *, available: pl.Expr | bool = True) -> pl.Expr:
    available_expr = available if isinstance(available, pl.Expr) else pl.lit(bool(available))
    return (
        pl.when(~available_expr | pl.col(column).is_null())
        .then(pl.lit("source_unavailable"))
        .when(pl.col(column) > 0.0)
        .then(pl.lit("positive"))
        .otherwise(pl.lit("derived_zero"))
    )


def build_observation_geometry_lazy(
    config_path: str | Path,
    *,
    source_type: str,
    lineage: Mapping[str, object],
    component_provenance_json: str,
) -> tuple[pl.LazyFrame, dict[str, object]]:
    """Build the forward-only component-rich static geometry contract.

    Older compact terrain partitions may lack independently observed unweighted
    LOS. Those rows retain the distance-integrated value and publish null pure
    LOS/viewability with ``source_unavailable`` rather than fabricating it.
    """

    source_type = normalize_source_type(source_type)
    paths = final_artifact_paths(config_path)
    keys = ["source_h3", "target_h3"]
    lookup = (
        scan_required(paths.source_target_lookup, FINAL_SCHEMAS["source_target_lookup"])
        .filter(pl.col("source_type") == source_type)
        .select(*keys, pl.col("distance_km").cast(pl.Float32), "source_type")
    )
    clear_sky_path = (
        paths.source_target_clear_sky
        if source_type == "land"
        else paths.ocean_source_target_clear_sky
    )
    clear_sky = scan_required(
        clear_sky_path,
        [
            *keys,
            "unweighted_los_observed",
            "joint_los_fraction",
            "distance_weighted_los_fraction",
        ],
    )
    distance = scan_required(
        paths.weights_path("distance_weights", source_type=source_type),
        [*keys, "weight_distance"],
    )
    vegetation = scan_required(
        paths.weights_path("vegetation_weights", source_type=source_type),
        FINAL_SCHEMAS["vegetation_weights"],
    ).select(*keys, "weight_vegetation", "vegetation_status")
    coverage = {
        "source_type": source_type,
        "clear_sky": _validate_pair_factor_coverage(
            lookup.select(keys),
            clear_sky.select(keys),
            label=f"{source_type} clear sky",
            require_complete=True,
        ),
        "distance": _validate_pair_factor_coverage(
            lookup.select(keys),
            distance.select(keys),
            label=f"{source_type} distance",
            require_complete=True,
        ),
        "vegetation": _validate_pair_factor_coverage(
            lookup.select(keys),
            vegetation.select(keys),
            label=f"{source_type} vegetation",
            require_complete=True,
        ),
    }
    unweighted_available = pl.col("unweighted_los_observed")
    vegetation_applicable = pl.col("vegetation_status") == VEGETATION_STATUS_COMPUTED
    vegetation_factor = (
        pl.col("weight_vegetation") if source_type == "land" else pl.lit(1.0, dtype=pl.Float32)
    )
    composed = (
        lookup.join(clear_sky, on=keys, how="left")
        .join(distance, on=keys, how="left")
        .join(vegetation, on=keys, how="left")
        .with_columns(
            pl.when(unweighted_available)
            .then(pl.col("joint_los_fraction"))
            .otherwise(pl.lit(None, dtype=pl.Float32))
            .cast(pl.Float32)
            .alias("line_of_sight_support"),
            pl.col("weight_distance").cast(pl.Float32).alias("distance_detection_weight"),
            pl.col("distance_weighted_los_fraction")
            .cast(pl.Float32)
            .alias("distance_weighted_los_support"),
            pl.when(vegetation_applicable)
            .then(pl.col("weight_vegetation"))
            .otherwise(pl.lit(None, dtype=pl.Float32))
            .cast(pl.Float32)
            .alias("vegetation_attenuation"),
            pl.when(unweighted_available)
            .then(pl.col("joint_los_fraction") * vegetation_factor)
            .otherwise(pl.lit(None, dtype=pl.Float32))
            .cast(pl.Float32)
            .alias("physical_viewability"),
            (pl.col("distance_weighted_los_fraction") * vegetation_factor)
            .cast(pl.Float32)
            .alias("distance_adjusted_viewability"),
        )
        .with_columns(
            _controlled_weight_state("line_of_sight_support", available=unweighted_available).alias(
                "line_of_sight_state"
            ),
            _controlled_weight_state("distance_detection_weight").alias("distance_detection_state"),
            _controlled_weight_state("distance_weighted_los_support").alias(
                "distance_weighted_los_state"
            ),
        )
    )
    composed = composed.with_columns(
        (
            _controlled_weight_state("vegetation_attenuation", available=vegetation_applicable)
            if source_type == "land"
            else pl.lit("not_applicable")
        ).alias("vegetation_state"),
        _controlled_weight_state("physical_viewability", available=unweighted_available).alias(
            "physical_viewability_state"
        ),
        _controlled_weight_state("distance_adjusted_viewability").alias(
            "distance_adjusted_viewability_state"
        ),
        (~pl.col("unweighted_los_observed")).alias("legacy_static_schema"),
        pl.lit(component_provenance_json).alias("COMPONENT_PROVENANCE_JSON"),
        pl.when(pl.col("unweighted_los_observed"))
        .then(pl.lit("complete"))
        .otherwise(pl.lit("partial"))
        .alias("DATA_COVERAGE_STATE"),
        pl.when(pl.col("unweighted_los_observed"))
        .then(pl.lit("complete"))
        .otherwise(pl.lit("partial"))
        .alias("SOURCE_COVERAGE_STATE"),
        *(pl.lit(value).alias(name) for name, value in lineage.items()),
    ).select(FINAL_SCHEMAS["observation_geometry"])
    return composed, coverage


def materialize_observation_geometry_output(
    config_path: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
) -> Path:
    """Publish one schema-v3 geometry artifact from retained full diagnostics."""

    source_type = normalize_source_type(source_type)
    raw, _config_dir = load_yaml(config_path)
    paths = final_artifact_paths(config_path)
    output_path = (
        paths.land_observation_geometry
        if source_type == "land"
        else paths.water_observation_geometry
    )
    input_paths = {
        **_static_input_paths(paths, source_type),
        "source_target_clear_sky": (
            paths.source_target_clear_sky
            if source_type == "land"
            else paths.ocean_source_target_clear_sky
        ),
    }
    input_checksums = {name: checksum_path(path) for name, path in input_paths.items()}
    config_hash = static_scientific_config_hash(raw)
    generation_id = f"{source_type}_{stable_config_hash({'config': config_hash, 'inputs': input_checksums}, length=20)}"
    knowledge_time = datetime.now(UTC).replace(microsecond=0).isoformat()
    lineage = {
        "GENERATION_ID": generation_id,
        "CONFIG_HASH": config_hash,
        "SOURCE_HASHES_JSON": json.dumps(
            dict(sorted(input_checksums.items())), sort_keys=True, separators=(",", ":")
        ),
        "KNOWLEDGE_TIME_UTC": knowledge_time,
        "SOURCE_VINTAGES_JSON": json.dumps(
            {
                "canopy_product_year": (raw.get("vegetation_data", {}) or {})
                .get("canopy", {})
                .get("product_year", 2020),
                "viewshed_run_version": (raw.get("run", {}) or {}).get("version"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "HISTORICAL_RECONSTRUCTION": False,
    }
    component_provenance = json.dumps(
        {
            "line_of_sight": {
                "source_vintage": (raw.get("run", {}) or {}).get("version"),
                "knowledge_time_utc": knowledge_time,
                "historical_reconstruction": False,
            },
            "vegetation": {
                "source_vintage": (raw.get("vegetation_data", {}) or {})
                .get("canopy", {})
                .get("product_year", 2020),
                "knowledge_time_utc": knowledge_time,
                "historical_reconstruction": False,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    geometry, _coverage = build_observation_geometry_lazy(
        config_path,
        source_type=source_type,
        lineage=lineage,
        component_provenance_json=component_provenance,
    )
    atomic_sink_parquet(
        geometry,
        output_path,
        overwrite=overwrite,
        metadata={
            "orcacast.artifact_kind": "human.viewshed.observation_geometry",
            "orcacast.schema_version": OBSERVATION_GEOMETRY_SCHEMA_VERSION,
            "orcacast.source_type": source_type,
            "orcacast.generation_id": generation_id,
            "orcacast.config_hash": config_hash,
            "orcacast.input_checksums": lineage["SOURCE_HASHES_JSON"],
        },
    )
    validate_parquet_schema(output_path, FINAL_SCHEMAS["observation_geometry"])
    return output_path


def static_scientific_config_hash(raw: Mapping[str, object]) -> str:
    """Hash only configuration that can change the static physical kernel."""

    payload = {
        key: raw.get(key)
        for key in (
            "area",
            "region",
            "viewshed",
            "h3",
            "distance_weight",
            "source_target_lookup",
            "water_viewing",
            "canopy_visibility",
            "vegetation_weights",
        )
    }
    return stable_config_hash(payload, length=16)


def _static_input_paths(paths: FinalArtifactPaths, source_type: str) -> dict[str, Path]:
    source_type = normalize_source_type(source_type)
    return {
        "source_target_lookup": paths.source_target_lookup,
        "terrain_weights": paths.weights_path("terrain_weights", source_type=source_type),
        "distance_weights": paths.weights_path("distance_weights", source_type=source_type),
        "vegetation_weights": paths.weights_path("vegetation_weights", source_type=source_type),
    }


def _static_parquet_metadata(
    raw: Mapping[str, object],
    *,
    source_type: str,
    input_checksums: Mapping[str, str],
) -> dict[str, str]:
    assumptions = _effective_physical_assumptions(raw, source_type=source_type)
    return {
        "orcacast.artifact_kind": "human.viewshed.static_pair_kernel",
        "orcacast.schema_version": STATIC_ARTIFACT_SCHEMA_VERSION,
        "orcacast.source_type": normalize_source_type(source_type),
        "orcacast.scientific_config_hash": static_scientific_config_hash(raw),
        "orcacast.input_checksums": json.dumps(
            dict(sorted(input_checksums.items())), separators=(",", ":")
        ),
        "orcacast.grain": "source_h3_x_target_h3",
        "orcacast.formula": "weight_terrain*weight_vegetation",
        "orcacast.physical_assumptions": json.dumps(
            assumptions, sort_keys=True, separators=(",", ":"), default=str
        ),
    }


def _effective_physical_assumptions(
    raw: Mapping[str, object], *, source_type: str
) -> dict[str, object]:
    """Return the assumptions actually applied for one source domain."""

    source_type = normalize_source_type(source_type)
    dem = get_dem_settings(dict(raw))
    h3_settings = dict(raw.get("h3", {}) or {})
    assumptions: dict[str, object] = {
        "source_type": source_type,
        "distance_weight": dict(raw.get("distance_weight", {}) or {}),
        "maximum_candidate_distance_km": (raw.get("source_target_lookup", {}) or {}).get(
            f"max_distance_km_{source_type}"
        ),
    }
    if source_type == "water":
        water = dict(raw.get("water_viewing", {}) or {})
        height_classes = dict(water.get("observer_height_classes", {}) or {})
        height_class = str(water.get("default_observer_height_class", ""))
        selected = dict(height_classes.get(height_class, {}) or {})
        assumptions.update(
            {
                "observer_height_class": height_class,
                "observer_height_m": selected.get("eye_height_m"),
                "target_height_m": water.get("target_visibility_height_m"),
                "maximum_viewshed_distance_m": dem.get("max_distance_m"),
                "curvature_coefficient": dem.get("curvature_coefficient"),
                "earth_radius_m": dem.get("earth_radius_m"),
                "source_samples_per_cell": water.get("source_samples_per_cell"),
                "target_samples_per_cell": (water.get("land_mask", {}) or {}).get(
                    "target_samples_per_cell"
                ),
                "land_mask": dict(water.get("land_mask", {}) or {}),
                "surface_model": "opaque_land_mask_open_water",
                "dem_applicable": False,
                "canopy_applicable": False,
                "h3": {
                    "source_resolution": h3_settings.get("source_resolution"),
                    "target_resolution": h3_settings.get("target_resolution"),
                    "source_sampling_mode": "deterministic_open_water_samples",
                    "source_samples_per_cell": water.get("source_samples_per_cell"),
                    "target_samples_per_cell": (water.get("land_mask", {}) or {}).get(
                        "target_samples_per_cell"
                    ),
                },
            }
        )
    else:
        assumptions.update(dem)
        assumptions["h3"] = h3_settings
        assumptions["dem_applicable"] = True
        assumptions["canopy_visibility"] = dict(raw.get("canopy_visibility", {}) or {})
        assumptions["canopy_applicable"] = True
        assumptions["canopy_observer_algorithm_version"] = "observer_isolated_canopy_surface_v2"
    return assumptions


def _write_static_artifact_metadata(
    output_path: Path,
    *,
    raw: dict[str, object],
    source_type: str,
    input_paths: Mapping[str, Path],
    input_checksums: Mapping[str, str],
    coverage: Mapping[str, object],
    row_count: int,
) -> Path:
    pair_stats = _pair_key_stats(pl.scan_parquet(str(output_path)))
    if pair_stats["rows"] != pair_stats["unique_pairs"]:
        raise ValueError(f"Final static artifact has duplicate pair keys: {pair_stats}")
    assumptions = _effective_physical_assumptions(raw, source_type=source_type)
    return write_metadata_sidecar(
        output_path,
        raw,
        {
            "dem": assumptions,
            "h3": assumptions["h3"],
            "physical_assumptions": assumptions,
            "artifact_kind": "human.viewshed.static_pair_kernel",
            "schema_version": STATIC_ARTIFACT_SCHEMA_VERSION,
            "status": "complete",
            "source_type": normalize_source_type(source_type),
            "scientific_config_hash": static_scientific_config_hash(raw),
            "grain": "source_h3_x_target_h3",
            "formula": "weight_static_viewability = weight_terrain * weight_vegetation",
            "distance_semantics": "distance_integrated_in_weight_terrain; weight_distance_is_diagnostic",
            "input_paths": {key: str(value) for key, value in input_paths.items()},
            "input_checksums": dict(input_checksums),
            "input_retention_policy": "ephemeral_inputs_may_be_pruned_after_final_validation",
            "artifact_checksum": checksum_path(output_path),
            "coverage": dict(coverage),
            "row_count": int(row_count),
            "pair_stats": pair_stats,
            "schema": list(FINAL_SCHEMAS["static_weights"]),
        },
    )


def refresh_static_artifact_metadata(
    config_path: str | Path,
    *,
    source_type: str,
) -> Path:
    """Refresh only a compact artifact sidecar without rebuilding its data."""

    source_type = normalize_source_type(source_type)
    raw, _config_dir = load_yaml(config_path)
    paths = final_artifact_paths(config_path)
    output_path = paths.land_static_weights if source_type == "land" else paths.water_static_weights
    validate_parquet_schema(output_path, FINAL_SCHEMAS["static_weights"])
    previous = load_metadata_sidecar(output_path)
    if previous is None:
        raise ValueError(f"Static viewshed artifact has no metadata sidecar: {output_path}")
    input_paths = previous.get("input_paths")
    input_checksums = previous.get("input_checksums")
    if not isinstance(input_paths, Mapping) or not isinstance(input_checksums, Mapping):
        raise ValueError(f"Static viewshed sidecar lacks input lineage: {output_path}")
    if previous.get("artifact_checksum") != checksum_path(output_path):
        raise ValueError(f"Static viewshed artifact checksum changed: {output_path}")
    return _write_static_artifact_metadata(
        output_path,
        raw=raw,
        source_type=source_type,
        input_paths={name: Path(str(path)) for name, path in input_paths.items()},
        input_checksums={name: str(value) for name, value in input_checksums.items()},
        coverage=dict(previous.get("coverage", {})),
        row_count=int(previous.get("row_count", pq.ParquetFile(output_path).metadata.num_rows)),
    )


def validate_static_artifact_metadata(
    output_path: Path,
    *,
    raw: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    validate_parquet_schema(output_path, FINAL_SCHEMAS["static_weights"])
    sidecar = load_metadata_sidecar(output_path)
    if sidecar is None:
        raise ValueError(f"Static viewshed artifact has no metadata sidecar: {output_path}")
    expected = {
        "artifact_kind": "human.viewshed.static_pair_kernel",
        "schema_version": STATIC_ARTIFACT_SCHEMA_VERSION,
        "status": "complete",
        "source_type": normalize_source_type(source_type),
        "scientific_config_hash": static_scientific_config_hash(raw),
    }
    mismatches = {
        key: {"expected": value, "actual": sidecar.get(key)}
        for key, value in expected.items()
        if sidecar.get(key) != value
    }
    expected_assumptions = _effective_physical_assumptions(raw, source_type=source_type)
    if sidecar.get("dem") != expected_assumptions:
        mismatches["dem"] = {
            "expected": expected_assumptions,
            "actual": sidecar.get("dem"),
        }
    if sidecar.get("physical_assumptions") != expected_assumptions:
        mismatches["physical_assumptions"] = {
            "expected": expected_assumptions,
            "actual": sidecar.get("physical_assumptions"),
        }
    if sidecar.get("h3") != expected_assumptions["h3"]:
        mismatches["h3"] = {
            "expected": expected_assumptions["h3"],
            "actual": sidecar.get("h3"),
        }
    if mismatches:
        raise ValueError(
            f"Static viewshed metadata mismatch for {output_path}: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    expected_artifact_checksum = sidecar.get("artifact_checksum")
    actual_artifact_checksum = checksum_path(output_path)
    if expected_artifact_checksum != actual_artifact_checksum:
        raise ValueError(
            f"Static viewshed artifact checksum mismatch for {output_path}: "
            f"expected={expected_artifact_checksum} actual={actual_artifact_checksum}"
        )
    input_paths = sidecar.get("input_paths")
    input_checksums = sidecar.get("input_checksums")
    if not isinstance(input_paths, Mapping) or not isinstance(input_checksums, Mapping):
        raise ValueError(f"Static viewshed metadata is missing input lineage for {output_path}")
    if set(input_paths) != set(input_checksums):
        raise ValueError(f"Static viewshed input path/checksum keys disagree for {output_path}")
    checksum_mismatches: dict[str, dict[str, object]] = {}
    missing_inputs: list[str] = []
    for name, value in input_paths.items():
        input_path = Path(str(value))
        expected_checksum = str(input_checksums[name])
        if not input_path.exists():
            missing_inputs.append(str(name))
            continue
        actual_checksum = checksum_path(input_path)
        if actual_checksum != expected_checksum:
            checksum_mismatches[str(name)] = {
                "path": str(input_path),
                "expected": expected_checksum,
                "actual": actual_checksum,
            }
    if checksum_mismatches:
        raise ValueError(
            f"Static viewshed inputs changed after {output_path} was built: "
            f"{json.dumps(checksum_mismatches, sort_keys=True)}"
        )
    if missing_inputs and sidecar.get("input_retention_policy") != (
        "ephemeral_inputs_may_be_pruned_after_final_validation"
    ):
        raise ValueError(
            f"Static viewshed inputs are missing without a declared retention policy: "
            f"{sorted(missing_inputs)}"
        )
    return sidecar


def materialize_static_viewability_outputs(
    config_path: str | Path, *, overwrite: bool = False
) -> dict[str, Path]:
    """Create the two notebook-style static viewshed products.

    Both products start from the source-type-specific canonical lookup. Terrain,
    distance, and vegetation all require exact key coverage. Physical zeroes
    must already be explicit in the dense terrain factor.
    """

    raw, _config_dir = load_yaml(config_path)
    paths = final_artifact_paths(config_path)
    outputs = {
        "land": paths.land_static_weights,
        "water": paths.water_static_weights,
    }
    existing = {source_type: path.exists() for source_type, path in outputs.items()}
    if not overwrite and any(existing.values()):
        if not all(existing.values()):
            raise FileExistsError(
                "Static viewshed output set is incomplete and cannot be reused: "
                f"{existing}. Rebuild with overwrite=True."
            )
        for source_type, output_path in outputs.items():
            validate_static_artifact_metadata(
                output_path,
                raw=raw,
                source_type=source_type,
            )
        return {
            "land_static_weights": outputs["land"],
            "water_static_weights": outputs["water"],
        }

    staged: dict[str, Path] = {}
    prepared: dict[str, dict[str, object]] = {}
    try:
        for source_type, output_path in outputs.items():
            composed, coverage = build_static_viewability_lazy(
                config_path,
                source_type=source_type,
            )
            input_paths = _static_input_paths(paths, source_type)
            input_checksums = {name: checksum_path(path) for name, path in input_paths.items()}
            parquet_metadata = _static_parquet_metadata(
                raw,
                source_type=source_type,
                input_checksums=input_checksums,
            )
            stage_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.staged")
            staged[source_type] = stage_path
            row_count = atomic_sink_parquet(
                composed,
                stage_path,
                overwrite=True,
                metadata=parquet_metadata,
            )
            validate_parquet_schema(stage_path, FINAL_SCHEMAS["static_weights"])
            prepared[source_type] = {
                "coverage": coverage,
                "input_paths": input_paths,
                "input_checksums": input_checksums,
                "row_count": row_count,
            }

        backups: dict[str, Path] = {}
        sidecar_backups: dict[Path, Path] = {}
        promoted: set[str] = set()
        try:
            for source_type, output_path in outputs.items():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    backup = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.backup")
                    output_path.replace(backup)
                    backups[source_type] = backup
                for sidecar in metadata_sidecars_for(output_path):
                    if sidecar.exists():
                        sidecar_backup = sidecar.with_name(
                            f".{sidecar.name}.{uuid.uuid4().hex}.backup"
                        )
                        sidecar.replace(sidecar_backup)
                        sidecar_backups[sidecar] = sidecar_backup
            for source_type, output_path in outputs.items():
                staged[source_type].replace(output_path)
                promoted.add(source_type)
            for source_type, output_path in outputs.items():
                info = prepared[source_type]
                _write_static_artifact_metadata(
                    output_path,
                    raw=raw,
                    source_type=source_type,
                    input_paths=info["input_paths"],
                    input_checksums=info["input_checksums"],
                    coverage=info["coverage"],
                    row_count=int(info["row_count"]),
                )
                validate_static_artifact_metadata(
                    output_path,
                    raw=raw,
                    source_type=source_type,
                )
        except Exception:
            for source_type, output_path in outputs.items():
                if source_type in promoted:
                    output_path.unlink(missing_ok=True)
                for sidecar in metadata_sidecars_for(output_path):
                    if source_type in promoted or sidecar in sidecar_backups:
                        sidecar.unlink(missing_ok=True)
                backup = backups.get(source_type)
                if backup is not None and backup.exists():
                    backup.replace(output_path)
            for sidecar, backup in sidecar_backups.items():
                if backup.exists():
                    backup.replace(sidecar)
            raise
        finally:
            for backup in backups.values():
                backup.unlink(missing_ok=True)
            for backup in sidecar_backups.values():
                backup.unlink(missing_ok=True)
    finally:
        for stage_path in staged.values():
            stage_path.unlink(missing_ok=True)

    geometry_outputs = {
        source_type: materialize_observation_geometry_output(
            config_path, source_type=source_type, overwrite=True
        )
        for source_type in ("land", "water")
    }
    return {
        "land_static_weights": outputs["land"],
        "water_static_weights": outputs["water"],
        "land_observation_geometry": geometry_outputs["land"],
        "water_observation_geometry": geometry_outputs["water"],
    }


def materialize_static_viewability_output(
    config_path: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
) -> Path:
    """Materialize one source domain without requiring the other domain."""

    if source_type not in SOURCE_TYPES:
        raise ValueError("source_type must be 'land' or 'water'.")
    paths = final_artifact_paths(config_path)
    output_path = paths.land_static_weights if source_type == "land" else paths.water_static_weights
    raw, _config_dir = load_yaml(config_path)
    composed, coverage = build_static_viewability_lazy(
        config_path,
        source_type=source_type,
    )
    input_paths = _static_input_paths(paths, source_type)
    input_checksums = {name: checksum_path(path) for name, path in input_paths.items()}
    parquet_metadata = _static_parquet_metadata(
        raw,
        source_type=source_type,
        input_checksums=input_checksums,
    )
    row_count = atomic_sink_parquet(
        composed,
        output_path,
        overwrite=overwrite,
        metadata=parquet_metadata,
    )
    validate_parquet_schema(output_path, FINAL_SCHEMAS["static_weights"])
    _write_static_artifact_metadata(
        output_path,
        raw=raw,
        source_type=source_type,
        input_paths=input_paths,
        input_checksums=input_checksums,
        coverage=coverage,
        row_count=row_count,
    )
    materialize_observation_geometry_output(
        config_path, source_type=source_type, overwrite=overwrite
    )
    return output_path


def cleanup_viewshed_dir_to_static_outputs(config_path: str | Path) -> list[Path]:
    """Delete rebuildable stage data after both durable tables exist."""

    paths = final_artifact_paths(config_path)
    final_outputs = [paths.land_static_weights, paths.water_static_weights]
    missing = [path for path in final_outputs if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Refusing to clean viewshed outputs because static outputs are missing:\n"
            + "\n".join(str(path) for path in missing)
        )
    raw, _config_dir = load_yaml(config_path)
    validate_static_artifact_metadata(
        paths.land_static_weights,
        raw=raw,
        source_type="land",
    )
    validate_static_artifact_metadata(
        paths.water_static_weights,
        raw=raw,
        source_type="water",
    )

    root = paths.output_dir.resolve()
    preserved: set[Path] = set()
    for path in [
        *final_outputs,
        paths.land_observation_geometry,
        paths.water_observation_geometry,
    ]:
        if not path.exists():
            continue
        if path.resolve().is_relative_to(root):
            preserved.add(path.resolve())
            preserved.update(sidecar.resolve() for sidecar in metadata_sidecars_for(path))

    return cleanup_output_tree(
        root,
        preserve_paths=preserved,
        remove_root_if_empty=paths.final_output_dir.resolve() != root,
    )
