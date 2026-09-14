"""Shared distance runtime and decay configuration for viewshed stages.

This module is intentionally narrow and lookup-driven.

Canonical input
---------------
The source-target pair universe is created upstream by ``prepare_area.py`` and
must be stored as the new-standard lookup schema::

    source_h3: str
    target_h3: str
    distance_km: float
    source_type: str  # one of {"land", "water"}

This module does not classify land/water cells, generate H3 neighborhoods, or
recompute centroid distances. If the lookup does not contain ``distance_km`` or
``source_type``, the lookup is invalid and must be rebuilt.

Production output
-----------------
The compact distance-weight artifact contains exactly::

    source_h3: str
    target_h3: str
    distance_km: float32
    weight_distance: float32

The selected source type is encoded by the output path, not repeated in every
row of the final distance table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.config.paths import resolve_config_path

from ..contracts.artifacts import tmp_dir_for_stage
from ..contracts.pairs import SOURCE_TYPES
from .paths import (
    get_h3_settings,
    get_run_version,
    output_dir_from_config,
    stable_config_hash,
)
from .schema import load_yaml_config, resolve_distance_weight_section

VALID_SOURCE_TYPES = SOURCE_TYPES


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    *,
    log_file: str | Path | None = None,
    quiet: bool = False,
) -> None:
    """Configure root logging without dropping an explicitly requested log file."""

    level_name = "ERROR" if quiet else str(log_level).upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)
            handler.setFormatter(formatter)

        if log_file is not None:
            log_path = Path(log_file).expanduser().resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            has_file = any(
                isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename).resolve() == log_path
                for handler in root.handlers
            )
            if not has_file:
                file_handler = logging.FileHandler(log_path)
                file_handler.setLevel(level)
                file_handler.setFormatter(formatter)
                root.addHandler(file_handler)
        return

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_path = Path(log_file).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(level=level, handlers=handlers)
    for handler in root.handlers:
        handler.setFormatter(formatter)


# -----------------------------------------------------------------------------
# Config / runtime
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DistanceWeightConfig:
    selected_model: str = "logistic"

    # Logistic curve parameters.
    logistic_d50_km: float = 9.0
    logistic_slope_km: float = 2.7

    # Optional anchoring of the selected curve at exactly 1.0 for d=0.
    # Disabled by default so existing production configurations remain
    # reproducible; experiments can opt in explicitly.
    normalize_at_zero: bool = False

    # Exponential curve parameter.
    exponential_lambda_km: float = 8.0

    # Piecewise curve parameters. ``piecewise_near_km`` and
    # ``piecewise_far_km`` are accepted as aliases for the full/zero distances.
    piecewise_full_weight_km: float = 3.0
    piecewise_zero_weight_km: float = 30.0
    piecewise_near_km: float | None = None
    piecewise_far_km: float | None = None

    # Distance cutoff. Defaults to viewshed.max_distance_m / 1000 when omitted.
    hard_cutoff_km: float | None = None

    # Partitioning by source H3 cell.
    source_chunk_size: int = 256

    # Output behavior.
    overwrite: bool = False
    version: str = "distance_v001_slim_source_type_lookup"

    # Optional summaries. Exact p90 can be expensive; keep off by default.
    compute_exact_p90: bool = False
    p90_method: str = "none"


@dataclass(frozen=True)
class DistanceRuntime:
    config_path: Path
    config_dir: Path
    raw_config: dict[str, Any]
    data_dir: Path
    output_dir: Path
    source_resolution: int
    target_resolution: int
    run_version: str
    config_hash: str
    lookup_path: Path
    viewshed_max_distance_km: float
    bbox_wgs84: tuple[float, float, float, float]
    projected_crs: str


@dataclass(frozen=True)
class DistanceWeightPaths:
    base_dir: Path
    partitioned_dir: Path
    final_weights_path: Path
    target_aggregation_path: Path
    source_aggregation_path: Path
    manifest_path: Path


_ALLOWED_DISTANCE_FIELDS = set(DistanceWeightConfig.__dataclass_fields__.keys())


def _validate_source_type(source_type: str) -> str:
    out = str(source_type).strip().lower()
    if out not in VALID_SOURCE_TYPES:
        raise ValueError("source_type must be one of: land, water")
    return out


def load_distance_weight_config(raw_config: dict[str, Any]) -> DistanceWeightConfig:
    """Load and validate the distance_weight config section.

    This clean-standard version intentionally rejects old/deprecated fields such
    as ``distance_column`` or land/water classification settings.
    """

    section = dict(resolve_distance_weight_section(raw_config, error_on_deprecated=True))

    unknown = sorted(set(section) - _ALLOWED_DISTANCE_FIELDS)
    if unknown:
        raise ValueError(
            "Unknown distance_weight config field(s): "
            + ", ".join(unknown)
            + "\nAllowed fields: "
            + ", ".join(sorted(_ALLOWED_DISTANCE_FIELDS))
        )

    if section.get("piecewise_near_km") is not None:
        section["piecewise_full_weight_km"] = section["piecewise_near_km"]
    if section.get("piecewise_far_km") is not None:
        section["piecewise_zero_weight_km"] = section["piecewise_far_km"]

    cfg = DistanceWeightConfig(**section)
    selected = cfg.selected_model.lower().strip()

    if selected not in {"logistic", "exponential", "piecewise"}:
        raise ValueError(
            "distance_weight.selected_model must be one of: " "logistic, exponential, piecewise"
        )
    if cfg.logistic_slope_km <= 0:
        raise ValueError("distance_weight.logistic_slope_km must be > 0")
    if not isinstance(cfg.normalize_at_zero, bool):
        raise ValueError("distance_weight.normalize_at_zero must be a boolean")
    if cfg.exponential_lambda_km <= 0:
        raise ValueError("distance_weight.exponential_lambda_km must be > 0")
    if cfg.piecewise_zero_weight_km <= cfg.piecewise_full_weight_km:
        raise ValueError(
            "distance_weight.piecewise_zero_weight_km must be greater than "
            "piecewise_full_weight_km"
        )
    if cfg.hard_cutoff_km is not None and cfg.hard_cutoff_km <= 0:
        raise ValueError("distance_weight.hard_cutoff_km must be > 0 when provided")
    if cfg.source_chunk_size <= 0:
        raise ValueError("distance_weight.source_chunk_size must be > 0")
    if cfg.p90_method not in {"none", "exact"}:
        raise ValueError("distance_weight.p90_method must be one of: none, exact")

    if cfg.compute_exact_p90 and cfg.p90_method == "none":
        cfg = DistanceWeightConfig(**{**cfg.__dict__, "p90_method": "exact"})

    return cfg


def _parse_bbox_wgs84(value: Any) -> tuple[float, float, float, float]:
    """Parse bbox_wgs84 from either mapping or sequence config styles.

    Supported forms:
      bbox_wgs84:
        min_lon: -124
        min_lat: 47
        max_lon: -122
        max_lat: 49

      bbox_wgs84: [-124, 47, -122, 49]
    """
    if isinstance(value, dict):
        aliases = {
            "min_lon": ("min_lon", "xmin", "west", "left"),
            "min_lat": ("min_lat", "ymin", "south", "bottom"),
            "max_lon": ("max_lon", "xmax", "east", "right"),
            "max_lat": ("max_lat", "ymax", "north", "top"),
        }
        parsed: list[float] = []
        for canonical, keys in aliases.items():
            found = None
            for key in keys:
                if key in value:
                    found = value[key]
                    break
            if found is None:
                raise ValueError(
                    "bbox_wgs84 mapping must include min_lon, min_lat, max_lon, max_lat. "
                    f"Missing {canonical}; found keys={sorted(value)}"
                )
            parsed.append(float(found))
        return tuple(parsed)  # type: ignore[return-value]

    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(float(v) for v in value)  # type: ignore[return-value]

    raise ValueError(
        "bbox_wgs84 must be either a mapping with min_lon/min_lat/max_lon/max_lat "
        "or a 4-item sequence [min_lon, min_lat, max_lon, max_lat]."
    )


def load_distance_runtime(config_path: str | Path) -> DistanceRuntime:
    config_path = resolve_config_path(config_path)
    raw = load_yaml_config(config_path)

    config_dir = config_path.parent

    output_dir = output_dir_from_config(raw, config_dir).resolve()
    data_dir = output_dir.parent.resolve()

    h3_settings = get_h3_settings(raw)
    source_resolution = int(h3_settings.get("source_resolution", raw.get("h3_resolution", 6)))
    target_resolution = int(h3_settings.get("target_resolution", source_resolution))

    run_version = get_run_version(raw)
    config_hash = stable_config_hash(raw)

    lookup_path = raw.get("source_target_lookup_path")
    if lookup_path is None:
        lookup_path = output_dir / "lookup" / f"SOURCE_TARGET_LOOKUP_H3R{target_resolution}.parquet"
    else:
        lookup_path = Path(lookup_path).expanduser()
        if not lookup_path.is_absolute():
            lookup_path = (config_dir / lookup_path).resolve()
        else:
            lookup_path = lookup_path.resolve()

    viewshed_cfg = raw.get("viewshed", {}) or {}
    viewshed_max_distance_m = float(viewshed_cfg.get("max_distance_m", 30000.0))
    viewshed_max_distance_km = viewshed_max_distance_m / 1000.0
    region_cfg = raw.get("region", {}) or {}
    projected_crs = (
        viewshed_cfg.get("crs_projected")
        or region_cfg.get("crs_projected")
        or raw.get("projected_crs")
        or "EPSG:5070"
    )

    return DistanceRuntime(
        config_path=config_path,
        config_dir=config_dir,
        raw_config=raw,
        data_dir=data_dir,
        output_dir=output_dir,
        source_resolution=source_resolution,
        target_resolution=target_resolution,
        run_version=run_version,
        config_hash=config_hash,
        lookup_path=lookup_path,
        viewshed_max_distance_km=viewshed_max_distance_km,
        bbox_wgs84=_parse_bbox_wgs84(region_cfg.get("bbox_wgs84", raw.get("bbox_wgs84"))),
        projected_crs=str(projected_crs),
    )


def resolved_max_distance_km(runtime: DistanceRuntime, cfg: DistanceWeightConfig) -> float:
    if cfg.hard_cutoff_km is not None:
        return float(cfg.hard_cutoff_km)
    return float(runtime.viewshed_max_distance_km)


def distance_weight_paths(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    source_type: str = "land",
) -> DistanceWeightPaths:
    source_type = _validate_source_type(source_type)
    run_name = f"SRC_R{runtime.source_resolution}_TGT_R{runtime.target_resolution}"

    # Source-type-specific scratch dir. Final production artifact is materialized
    # by utils.final_artifacts.materialize_distance_weights.
    base_dir = tmp_dir_for_stage(runtime.config_path, "distance") / source_type
    partitioned_dir = base_dir / "partitions"

    return DistanceWeightPaths(
        base_dir=base_dir,
        partitioned_dir=partitioned_dir,
        final_weights_path=base_dir / f"viewshed_distance_weights_{run_name}.parquet",
        target_aggregation_path=base_dir
        / f"viewshed_target_distance_weighted_summary_{run_name}.parquet",
        source_aggregation_path=base_dir
        / f"viewshed_source_distance_weighted_summary_{run_name}.parquet",
        manifest_path=base_dir / f"distance_weight_manifest_{run_name}.csv",
    )
