"""First-class H3 pair-distance and reusable attenuation-profile products."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import h3
import numpy as np
import polars as pl

from viewshed_toolkit._internal.artifacts import checksum_path

from ...config import AppConfig, load_app_config, load_metadata_sidecar
from ...config.distance import (
    DistanceWeightConfig,
    load_distance_runtime,
    load_distance_weight_config,
)
from ...contracts.artifacts import final_artifact_paths_from_raw
from ...contracts.components import PAIR_KEYS, fingerprint
from ...contracts.distance import (
    DISTANCE_METHOD,
    DISTANCE_PRODUCT_SCHEMA_VERSION,
    DISTANCE_PROFILE_ALGORITHM_VERSION,
    DISTANCE_PROFILE_SCHEMA,
    PAIR_DISTANCE_ALGORITHM_VERSION,
    PAIR_DISTANCE_SCHEMA,
    distance_product_cache_matches,
    distance_profile_path,
    normalize_profile_id,
    normalize_source_type,
    package_version,
    pair_distance_path,
    read_distance_product_record,
    write_distance_product,
)
from ...contracts.pairs import LOOKUP_ALGORITHM_VERSION, SOURCE_TARGET_LOOKUP_SCHEMA
from .compute import distance_weight_values

PAIR_PRODUCT_KEYS = ["source_type", *PAIR_KEYS]
PAIR_SCIENTIFIC_FIELDS = (
    "schema_version",
    "schema",
    "algorithm_version",
    "source_type",
    "source_h3_resolution",
    "target_h3_resolution",
    "distance_method",
    "units",
    "candidate_policy",
    "pair_content_identity",
)


def _target_resolution(app: AppConfig) -> int:
    return int(app.h3.target_resolution or app.h3.source_resolution)


def _pair_content_identity(frame: pl.DataFrame) -> str:
    """Hash canonical pair values without depending on input serialization order."""

    row_hashes = frame.hash_rows(seed=0, seed_1=1, seed_2=2, seed_3=3).to_numpy()
    digest = hashlib.sha256()
    digest.update(str(frame.height).encode("ascii"))
    digest.update(b"\0")
    digest.update(row_hashes.tobytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class DistanceProfile:
    """One independently reusable distance-attenuation profile."""

    profile_id: str
    selected_model: str = "logistic"
    logistic_d50_km: float = 9.0
    logistic_slope_km: float = 2.7
    normalize_at_zero: bool = False
    exponential_lambda_km: float = 8.0
    piecewise_full_weight_km: float = 3.0
    piecewise_zero_weight_km: float = 30.0
    hard_cutoff_km: float | None = None

    @classmethod
    def from_weight_config(
        cls,
        profile_id: str,
        config: DistanceWeightConfig,
        *,
        effective_cutoff_km: float | None,
    ) -> DistanceProfile:
        near = (
            config.piecewise_near_km
            if config.piecewise_near_km is not None
            else config.piecewise_full_weight_km
        )
        far = (
            config.piecewise_far_km
            if config.piecewise_far_km is not None
            else config.piecewise_zero_weight_km
        )
        return cls(
            profile_id=profile_id,
            selected_model=config.selected_model,
            logistic_d50_km=config.logistic_d50_km,
            logistic_slope_km=config.logistic_slope_km,
            normalize_at_zero=config.normalize_at_zero,
            exponential_lambda_km=config.exponential_lambda_km,
            piecewise_full_weight_km=float(near),
            piecewise_zero_weight_km=float(far),
            hard_cutoff_km=effective_cutoff_km,
        )


def _profile_config(profile: DistanceProfile) -> DistanceWeightConfig:
    values = asdict(profile)
    values.pop("profile_id")
    cfg = load_distance_weight_config({"distance_weight": values})
    selected = cfg.selected_model.lower().strip()
    finite_values = {
        "logistic_d50_km": cfg.logistic_d50_km,
        "logistic_slope_km": cfg.logistic_slope_km,
        "exponential_lambda_km": cfg.exponential_lambda_km,
        "piecewise_full_weight_km": cfg.piecewise_full_weight_km,
        "piecewise_zero_weight_km": cfg.piecewise_zero_weight_km,
    }
    for name, value in finite_values.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"Distance profile {name} must be finite")
    if cfg.hard_cutoff_km is not None and not math.isfinite(float(cfg.hard_cutoff_km)):
        raise ValueError("Distance profile hard_cutoff_km must be finite when provided")
    return replace(cfg, selected_model=selected)


def effective_distance_profile(profile: DistanceProfile) -> dict[str, Any]:
    """Return model-relevant normalized parameters used for scientific identity."""

    normalize_profile_id(profile.profile_id)
    cfg = _profile_config(profile)
    selected = cfg.selected_model
    if selected == "logistic":
        parameters: dict[str, float | bool] = {
            "d50_km": float(cfg.logistic_d50_km),
            "slope_km": float(cfg.logistic_slope_km),
            "normalize_at_zero": bool(cfg.normalize_at_zero),
        }
    elif selected == "exponential":
        parameters = {"lambda_km": float(cfg.exponential_lambda_km)}
    else:
        parameters = {
            "full_weight_km": float(cfg.piecewise_full_weight_km),
            "zero_weight_km": float(cfg.piecewise_zero_weight_km),
        }
    return {
        "selected_model": selected,
        "parameters": parameters,
        "distance_units": "km",
        "effective_cutoff_km": (None if cfg.hard_cutoff_km is None else float(cfg.hard_cutoff_km)),
        "cutoff_behavior": "preserve_pairs_and_assign_zero_beyond_cutoff",
    }


def _profile_from_effective(profile_id: str, effective: dict[str, Any]) -> DistanceProfile:
    selected = str(effective.get("selected_model", ""))
    parameters = effective.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("Distance profile metadata has invalid normalized parameters")
    cutoff = effective.get("effective_cutoff_km")
    if selected == "logistic":
        return DistanceProfile(
            profile_id,
            selected_model=selected,
            logistic_d50_km=float(parameters["d50_km"]),
            logistic_slope_km=float(parameters["slope_km"]),
            normalize_at_zero=bool(parameters["normalize_at_zero"]),
            hard_cutoff_km=None if cutoff is None else float(cutoff),
        )
    if selected == "exponential":
        return DistanceProfile(
            profile_id,
            selected_model=selected,
            exponential_lambda_km=float(parameters["lambda_km"]),
            hard_cutoff_km=None if cutoff is None else float(cutoff),
        )
    if selected == "piecewise":
        return DistanceProfile(
            profile_id,
            selected_model=selected,
            piecewise_full_weight_km=float(parameters["full_weight_km"]),
            piecewise_zero_weight_km=float(parameters["zero_weight_km"]),
            hard_cutoff_km=None if cutoff is None else float(cutoff),
        )
    raise ValueError(f"Unsupported distance profile model: {selected}")


def _validate_h3_column(frame: pl.DataFrame, column: str, resolution: int) -> None:
    for cell in frame[column].unique().to_list():
        if not isinstance(cell, str) or not h3.is_valid_cell(cell):
            raise ValueError(f"Invalid H3 identifier in {column}: {cell!r}")
        if h3.get_resolution(cell) != int(resolution):
            raise ValueError(
                f"H3 resolution mismatch in {column}: {cell} is R{h3.get_resolution(cell)}, "
                f"expected R{resolution}"
            )


def _validate_distance_frame(
    frame: pl.DataFrame,
    *,
    schema: tuple[str, ...],
    source_resolution: int,
    target_resolution: int,
    expected_source_type: str | None,
) -> None:
    if tuple(frame.columns) != schema:
        raise ValueError(f"Distance product schema must be exactly {list(schema)}")
    if frame.select(
        pl.any_horizontal(
            [pl.col(column).is_null() for column in ("source_h3", "target_h3", "source_type")]
        ).any()
    ).item():
        raise ValueError("Distance product keys must be non-null")
    source_types = set(frame["source_type"].unique().to_list())
    if not source_types.issubset({"land", "water"}):
        raise ValueError(f"Distance product has invalid source_type values: {source_types}")
    if expected_source_type is not None and source_types not in ({expected_source_type}, set()):
        raise ValueError(
            f"Distance product does not contain only source_type={expected_source_type}"
        )
    if frame.select(PAIR_PRODUCT_KEYS).is_duplicated().any():
        raise ValueError("Duplicate source-target pair within a source role")
    _validate_h3_column(frame, "source_h3", source_resolution)
    _validate_h3_column(frame, "target_h3", target_resolution)
    for column in ("distance_m", "distance_km"):
        values = frame[column]
        if values.null_count() or not values.is_finite().all() or (values < 0).any():
            raise ValueError(f"{column} must contain finite, nonnegative observed distances")
    if frame.height and not np.allclose(
        frame["distance_m"].to_numpy(),
        frame["distance_km"].to_numpy() * 1000.0,
        rtol=1e-12,
        atol=1e-6,
    ):
        raise ValueError("distance_m and distance_km are inconsistent")
    if not frame.equals(frame.sort(PAIR_PRODUCT_KEYS)):
        raise ValueError("Distance product rows are not in deterministic key order")


def _candidate_policy(metadata: dict[str, Any], source_type: str) -> dict[str, Any]:
    payload = metadata.get("lookup_fingerprint_payload")
    if not isinstance(payload, dict):
        raise ValueError("Source-target lookup metadata has no fingerprint payload")
    required = {
        "bbox_wgs84",
        "bbox_buffer_rings",
        "strict_bbox_intersection",
        "min_land_fraction_for_source",
        "min_water_fraction_for_target",
        "allow_self_pairs",
        "allow_active_role_self_pairs",
        "projected_crs",
        "land_polygon_sha256",
        "water_polygon_sha256",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"Source-target lookup metadata lacks candidate policy fields: {missing}")
    return {
        "lookup_algorithm_version": metadata.get("algorithm_version"),
        "enumeration": "h3_grid_disk_then_geometry_aware_polygon_distance_cutoff",
        "maximum_pair_generation_distance_km": float(payload[f"max_distance_km_{source_type}"]),
        "centroid_distance_may_exceed_generation_limit": True,
        "bbox_wgs84": payload["bbox_wgs84"],
        "bbox_buffer_rings": int(payload["bbox_buffer_rings"]),
        "strict_bbox_intersection": bool(payload["strict_bbox_intersection"]),
        "min_land_fraction_for_source": float(payload["min_land_fraction_for_source"]),
        "min_water_fraction_for_target": float(payload["min_water_fraction_for_target"]),
        "max_grid_disk_k": payload.get("max_grid_disk_k"),
        "allow_self_pairs": bool(payload["allow_self_pairs"]),
        "allow_active_role_self_pairs": bool(payload["allow_active_role_self_pairs"]),
        "projected_cutoff_crs": str(payload["projected_crs"]),
        "land_polygon_sha256": payload["land_polygon_sha256"],
        "water_polygon_sha256": payload["water_polygon_sha256"],
    }


def _pair_contract(
    app: AppConfig,
    source_type: str,
    lookup_path: Path,
    frame: pl.DataFrame,
) -> tuple[dict[str, Any], str]:
    metadata = load_metadata_sidecar(lookup_path)
    if not isinstance(metadata, dict):
        raise ValueError(f"Source-target lookup metadata is missing or invalid: {lookup_path}")
    expected_metadata = {
        "algorithm_version": LOOKUP_ALGORITHM_VERSION,
        "schema": list(SOURCE_TARGET_LOOKUP_SCHEMA),
        "source_resolution": app.h3.source_resolution,
        "target_resolution": _target_resolution(app),
    }
    mismatches = {
        key: {"expected": expected, "actual": metadata.get(key)}
        for key, expected in expected_metadata.items()
        if metadata.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"Source-target lookup metadata mismatch: {mismatches}")
    lookup_checksum = checksum_path(lookup_path)
    scientific_contract = {
        "schema_version": DISTANCE_PRODUCT_SCHEMA_VERSION,
        "schema": list(PAIR_DISTANCE_SCHEMA),
        "algorithm_version": PAIR_DISTANCE_ALGORITHM_VERSION,
        "source_type": source_type,
        "source_h3_resolution": int(app.h3.source_resolution),
        "target_h3_resolution": _target_resolution(app),
        "distance_method": DISTANCE_METHOD,
        "units": {"distance_m": "m", "distance_km": "km"},
        "candidate_policy": _candidate_policy(metadata, source_type),
        "pair_content_identity": _pair_content_identity(frame),
    }
    contract = {
        "product_type": "pair_distances",
        **scientific_contract,
        "pair_scientific_identity": fingerprint(scientific_contract),
        "software_version": package_version(),
        "input_lineage": {
            "lookup_checksum": lookup_checksum,
            "lookup_algorithm_version": metadata["algorithm_version"],
        },
        "output_root": str(
            final_artifact_paths_from_raw(
                app.raw_config, app.config_path.parent
            ).final_output_dir.resolve()
        ),
    }
    return contract, lookup_checksum


def build_pair_distances(
    config: str | Path | AppConfig,
    *,
    source_type: str = "land",
    overwrite: bool = False,
) -> Path:
    """Materialize raw centroid distances from the validated candidate lookup."""

    app = config if isinstance(config, AppConfig) else load_app_config(config)
    source_type = normalize_source_type(source_type)
    lookup_path = final_artifact_paths_from_raw(
        app.raw_config, app.config_path.parent
    ).source_target_lookup
    if not lookup_path.is_file():
        raise FileNotFoundError(
            f"Pair distances require the canonical source-target lookup: {lookup_path}"
        )
    # Exact pair-product execution still validates that the persisted lookup
    # belongs to the current geometry/coverage contract. This reads vector
    # fingerprints only; it never touches raster configuration or inputs.
    from ...prepare.area.config import (
        _lookup_config,
        _validate_existing_lookup_metadata,
    )

    runtime = load_distance_runtime(app.config_path)
    integrated_cfg = load_distance_weight_config(app.raw_config)
    lookup_cfg = _lookup_config(app.raw_config, runtime, integrated_cfg)
    _validate_existing_lookup_metadata(lookup_path, runtime, integrated_cfg, lookup_cfg)
    raw = pl.read_parquet(lookup_path)
    if tuple(raw.columns) != SOURCE_TARGET_LOOKUP_SCHEMA:
        raise ValueError(
            "Source-target lookup schema mismatch; rebuild it before pair distances. "
            f"Expected {list(SOURCE_TARGET_LOOKUP_SCHEMA)}, got {raw.columns}"
        )
    selected = raw.filter(pl.col("source_type") == source_type)
    if selected.select(PAIR_KEYS).is_duplicated().any():
        raise ValueError("Source-target lookup contains duplicate pairs within a source role")
    distances = selected["distance_km"]
    if distances.null_count() or not distances.is_finite().all() or (distances < 0).any():
        raise ValueError("Source-target lookup distances must be finite, nonnegative, and observed")
    frame = selected.select(
        pl.col("source_h3").cast(pl.String),
        pl.col("target_h3").cast(pl.String),
        pl.col("source_type").cast(pl.String),
        (pl.col("distance_km").cast(pl.Float64) * 1000.0).alias("distance_m"),
        pl.col("distance_km").cast(pl.Float64),
    ).sort(PAIR_PRODUCT_KEYS)
    contract, lookup_checksum = _pair_contract(app, source_type, lookup_path, frame)
    _validate_distance_frame(
        frame,
        schema=PAIR_DISTANCE_SCHEMA,
        source_resolution=app.h3.source_resolution,
        target_resolution=_target_resolution(app),
        expected_source_type=source_type,
    )
    output = pair_distance_path(app, source_type)
    if not overwrite and distance_product_cache_matches(output, contract):
        validate_distance_product(output)
        return output
    return write_distance_product(
        frame,
        output,
        contract,
        overwrite=overwrite,
        extra_metadata={
            "source_lookup_path": str(lookup_path.resolve()),
            "source_lookup_checksum": lookup_checksum,
            "pair_coverage": {
                "rows": frame.height,
                "minimum_distance_km": frame["distance_km"].min(),
                "maximum_distance_km": frame["distance_km"].max(),
            },
            "audit_config_hash": app.config_hash,
        },
    )


def _profile_scientific_identity(pair_scientific_identity: str, effective: dict[str, Any]) -> str:
    return fingerprint(
        {
            "schema_version": DISTANCE_PRODUCT_SCHEMA_VERSION,
            "schema": list(DISTANCE_PROFILE_SCHEMA),
            "algorithm_version": DISTANCE_PROFILE_ALGORITHM_VERSION,
            "pair_distance_scientific_identity": pair_scientific_identity,
            "units": {
                "distance_m": "m",
                "distance_km": "km",
                "weight_distance": "unitless_[0,1]",
            },
            "effective_profile": effective,
        }
    )


def distance_profile_output_path(
    pair_distances: str | Path,
    profile: DistanceProfile,
) -> Path:
    """Return the contained content-addressed path for a profile request."""

    pair_path = Path(pair_distances).expanduser().resolve()
    pair_record = validate_distance_product(pair_path)
    if pair_record["contract"]["product_type"] != "pair_distances":
        raise ValueError("Distance profiles require a pair-distance product")
    effective = effective_distance_profile(profile)
    scientific_identity = _profile_scientific_identity(
        str(pair_record["contract"]["pair_scientific_identity"]), effective
    )
    return distance_profile_path(
        pair_path,
        profile_id=profile.profile_id,
        scientific_identity=scientific_identity,
    )


def build_distance_profile(
    pair_distances: str | Path,
    profile: DistanceProfile,
    *,
    overwrite: bool = False,
) -> Path:
    """Apply one profile using only a validated persisted pair-distance product."""

    pair_path = Path(pair_distances).expanduser().resolve()
    pair_record = validate_distance_product(pair_path)
    pair_contract = pair_record["contract"]
    if pair_contract["product_type"] != "pair_distances":
        raise ValueError("Distance profiles require a pair-distance product")
    effective = effective_distance_profile(profile)
    cutoff = effective["effective_cutoff_km"]
    coverage_limit = float(pair_contract["candidate_policy"]["maximum_pair_generation_distance_km"])
    if cutoff is not None and float(cutoff) > coverage_limit + 1e-12:
        raise ValueError(
            f"Profile cutoff {cutoff} km exceeds pair-product coverage limit "
            f"{coverage_limit} km; generate a wider pair universe first"
        )
    scientific_identity = _profile_scientific_identity(
        str(pair_contract["pair_scientific_identity"]), effective
    )
    profile_id = normalize_profile_id(profile.profile_id)
    output = distance_profile_output_path(pair_path, profile)
    contract = {
        "product_type": "distance_profile",
        "schema_version": DISTANCE_PRODUCT_SCHEMA_VERSION,
        "schema": list(DISTANCE_PROFILE_SCHEMA),
        "algorithm_version": DISTANCE_PROFILE_ALGORITHM_VERSION,
        "software_version": package_version(),
        "source_type": pair_contract["source_type"],
        "source_h3_resolution": pair_contract["source_h3_resolution"],
        "target_h3_resolution": pair_contract["target_h3_resolution"],
        "distance_method": pair_contract["distance_method"],
        "units": {**pair_contract["units"], "weight_distance": "unitless_[0,1]"},
        "candidate_policy": pair_contract["candidate_policy"],
        "profile_id": profile_id,
        "profile_scientific_identity": scientific_identity,
        "effective_profile": effective,
        "source_pair_product": {
            "path": str(pair_path),
            "checksum": pair_record["output_checksum"],
            "scientific_identity": pair_contract["pair_scientific_identity"],
        },
        "output_root": pair_contract["output_root"],
    }
    if not overwrite and distance_product_cache_matches(output, contract):
        validate_distance_product(output)
        return output
    pair_frame = pl.read_parquet(pair_path)
    cfg = _profile_config(profile)
    values = distance_weight_values(
        pair_frame["distance_km"].to_numpy(),
        cfg,
        max_distance_km=cfg.hard_cutoff_km,
    )
    frame = pair_frame.with_columns(pl.Series("weight_distance", values)).sort(PAIR_PRODUCT_KEYS)
    _validate_distance_frame(
        frame,
        schema=DISTANCE_PROFILE_SCHEMA,
        source_resolution=int(pair_contract["source_h3_resolution"]),
        target_resolution=int(pair_contract["target_h3_resolution"]),
        expected_source_type=str(pair_contract["source_type"]),
    )
    if frame["weight_distance"].null_count() or not frame["weight_distance"].is_finite().all():
        raise ValueError("Distance profile produced null or nonfinite weights")
    if not frame["weight_distance"].is_between(0.0, 1.0).all():
        raise ValueError("Distance profile weights must remain in [0, 1]")
    return write_distance_product(
        frame,
        output,
        contract,
        overwrite=overwrite,
        extra_metadata={
            "pair_coverage": {
                "rows": frame.height,
                "zero_weight_rows": int((frame["weight_distance"] == 0.0).sum()),
            }
        },
    )


def validate_distance_product(path: str | Path) -> dict[str, Any]:
    """Validate one standalone distance product without static-workflow inputs."""

    product_path = Path(path).expanduser().resolve()
    record = read_distance_product_record(product_path)
    contract = record["contract"]
    product_type = contract.get("product_type")
    schema = (
        PAIR_DISTANCE_SCHEMA
        if product_type == "pair_distances"
        else DISTANCE_PROFILE_SCHEMA if product_type == "distance_profile" else None
    )
    if schema is None:
        raise ValueError(f"Unknown distance product type: {product_type!r}")
    if contract.get("schema_version") != DISTANCE_PRODUCT_SCHEMA_VERSION:
        raise ValueError("Distance product schema version mismatch")
    if contract.get("schema") != list(schema):
        raise ValueError("Distance product contract schema mismatch")
    frame = pl.read_parquet(product_path)
    _validate_distance_frame(
        frame,
        schema=schema,
        source_resolution=int(contract["source_h3_resolution"]),
        target_resolution=int(contract["target_h3_resolution"]),
        expected_source_type=str(contract["source_type"]),
    )
    if int(record.get("rows", -1)) != frame.height:
        raise ValueError("Distance product row-count metadata mismatch")
    if product_type == "pair_distances":
        if contract.get("algorithm_version") != PAIR_DISTANCE_ALGORITHM_VERSION:
            raise ValueError("Pair-distance algorithm version mismatch")
        if contract.get("distance_method") != DISTANCE_METHOD:
            raise ValueError("Pair-distance method metadata mismatch")
        if contract.get("pair_content_identity") != _pair_content_identity(frame):
            raise ValueError("Pair-distance content identity mismatch")
        scientific_contract = {name: contract.get(name) for name in PAIR_SCIENTIFIC_FIELDS}
        if contract.get("pair_scientific_identity") != fingerprint(scientific_contract):
            raise ValueError("Pair-distance scientific identity mismatch")
        return record

    if contract.get("algorithm_version") != DISTANCE_PROFILE_ALGORITHM_VERSION:
        raise ValueError("Distance-profile algorithm version mismatch")
    source = contract.get("source_pair_product")
    if not isinstance(source, dict):
        raise ValueError("Distance profile has no source pair-product lineage")
    source_path = Path(str(source.get("path", "")))
    if not source_path.is_file() or checksum_path(source_path) != source.get("checksum"):
        raise ValueError("Distance profile source pair product is missing or has changed")
    pair_record = validate_distance_product(source_path)
    if pair_record["contract"]["pair_scientific_identity"] != source.get("scientific_identity"):
        raise ValueError("Distance profile source pair-product scientific identity mismatch")
    effective = contract.get("effective_profile")
    if not isinstance(effective, dict):
        raise ValueError("Distance profile has no normalized effective parameters")
    expected_identity = _profile_scientific_identity(
        str(pair_record["contract"]["pair_scientific_identity"]), effective
    )
    if contract.get("profile_scientific_identity") != expected_identity:
        raise ValueError("Distance profile scientific identity mismatch")
    pair_frame = pl.read_parquet(source_path)
    if not frame.select(PAIR_DISTANCE_SCHEMA).equals(pair_frame.select(PAIR_DISTANCE_SCHEMA)):
        raise ValueError("Distance profile does not preserve the source pair universe exactly")
    cfg = _profile_config(
        _profile_from_effective(str(contract.get("profile_id", "profile")), effective)
    )
    expected_weights = distance_weight_values(
        frame["distance_km"].to_numpy(), cfg, max_distance_km=cfg.hard_cutoff_km
    )
    if not np.array_equal(frame["weight_distance"].to_numpy(), expected_weights):
        raise ValueError("Distance profile weights do not match its effective parameters")
    if frame["weight_distance"].null_count() or not frame["weight_distance"].is_finite().all():
        raise ValueError("Distance profile has null or nonfinite weights")
    if not frame["weight_distance"].is_between(0.0, 1.0).all():
        raise ValueError("Distance profile weights must remain in [0, 1]")
    return record


def default_distance_profile(app: AppConfig) -> DistanceProfile:
    """Resolve the integrated model's existing default diagnostic profile."""

    cfg = load_distance_weight_config(app.raw_config)
    effective_cutoff_km = (
        float(cfg.hard_cutoff_km)
        if cfg.hard_cutoff_km is not None
        else float(app.viewshed.max_distance_m) / 1000.0
    )
    return DistanceProfile.from_weight_config(
        "default",
        cfg,
        effective_cutoff_km=effective_cutoff_km,
    )


__all__ = [
    "DistanceProfile",
    "build_distance_profile",
    "build_pair_distances",
    "default_distance_profile",
    "distance_profile_output_path",
    "effective_distance_profile",
    "validate_distance_product",
]
