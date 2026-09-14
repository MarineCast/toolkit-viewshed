"""Build the canonical source-target H3 lookup universe.

This module owns the canonical pair universe for the viewshed weighting stages.
The production lookup is deliberately narrow and strict:

    source_h3, target_h3, distance_km, source_type

``source_type`` is the modeling role of the observer source, either ``land`` or
``water``. Physical land/water composition is used internally to construct the
source and target universes, but it is not persisted in the production lookup.

Design contract
---------------
- ``prepare_area.py`` decides which source-target pairs exist.
- ``distance.py`` transforms ``distance_km`` into ``weight_distance``.
- ``terrain.py`` transforms the same pair universe into ``weight_terrain``.
- Production artifacts stay compact; QA/intermediate details stay out of the
  canonical lookup.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ...config import load_metadata_sidecar, resolve_existing_or_relative_path
from ...config.distance import (
    DistanceRuntime,
    DistanceWeightConfig,
    resolved_max_distance_km,
)
from ...contracts.pairs import (
    LOOKUP_ALGORITHM_VERSION,
    LOOKUP_METADATA_STEP,
    SOURCE_TARGET_LOOKUP_SCHEMA,
)
from ...contracts.artifacts import tmp_dir_for_stage

LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class SourceTargetLookupConfig:
    """Configuration for the canonical source-target lookup builder.

    This config intentionally describes only how to build the source/target
    universe. It does not define distance-decay behavior; that belongs to
    ``distance_weight``.
    """

    h3_resolution: int | None = None
    max_distance_km_land: float | None = None
    max_distance_km_water: float | None = None

    # Source role controls.
    include_land_sources: bool = True
    include_water_sources: bool = True
    include_mixed_as_land_sources: bool = True
    include_mixed_as_water_sources: bool = True

    # Target controls. Targets are whale water-domain cells; pure land targets
    # are intentionally unsupported.
    include_water_targets: bool = True
    include_mixed_as_water_targets: bool = True

    allow_self_pairs: bool = False
    # Same-H3 pairs remain valid when the source and target are distinct active
    # physical roles (for example, land observer area to water target area in a
    # mixed coastal cell). This is intentionally separate from unrestricted
    # geometric self-pairs.
    allow_active_role_self_pairs: bool = True
    bbox_buffer_rings: int = 1
    strict_bbox_intersection: bool = True
    parallel_workers: int | None = None
    max_grid_disk_k: int | None = None
    max_candidate_pairs_per_chunk: int | None = None

    # Physical cell classification thresholds used internally only.
    min_water_fraction_for_target: float = 0.001
    min_land_fraction_for_source: float = 0.01
    projected_crs: str | None = None


@dataclass(frozen=True)
class AreaLookupResult:
    lookup_path: Path
    partition_dir: Path
    n_pairs: int
    n_source_cells: int
    n_target_cells: int
    h3_resolution: int


@dataclass(frozen=True)
class SourceUniverse:
    land_source_cells: list[str]
    water_source_cells: list[str]
    target_cells: set[str]
    cell_attributes: pd.DataFrame


# -----------------------------------------------------------------------------
# Config and metadata helpers
# -----------------------------------------------------------------------------


def _coerce_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"source_target_lookup.{field} must be a boolean; got {value!r}")


def _lookup_config(
    raw: Mapping[str, Any],
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
) -> SourceTargetLookupConfig:
    """Parse ``source_target_lookup`` using the new slim lookup contract."""

    section = dict(raw.get("source_target_lookup", {}) or {})

    # Deprecated aliases from the earlier compact prototype. These are tolerated
    # only to make config migration less annoying; they are immediately mapped to
    # the explicit new fields.
    legacy_mixed_sources = section.pop("include_mixed_water_sources", None)
    if legacy_mixed_sources is not None:
        if (
            "include_mixed_as_land_sources" in section
            or "include_mixed_as_water_sources" in section
        ):
            raise ValueError(
                "Config mixes deprecated source_target_lookup.include_mixed_water_sources "
                "with explicit include_mixed_as_land_sources/include_mixed_as_water_sources. "
                "Use only the explicit fields."
            )
        legacy_value = _coerce_bool(legacy_mixed_sources, field="include_mixed_water_sources")
        section["include_mixed_as_land_sources"] = legacy_value
        section["include_mixed_as_water_sources"] = legacy_value
        LOGGER.warning(
            "source_target_lookup.include_mixed_water_sources is deprecated; use "
            "include_mixed_as_land_sources and include_mixed_as_water_sources."
        )

    legacy_mixed_targets = section.pop("include_mixed_water_targets", None)
    if legacy_mixed_targets is not None:
        if "include_mixed_as_water_targets" in section:
            raise ValueError(
                "Config mixes deprecated source_target_lookup.include_mixed_water_targets "
                "with include_mixed_as_water_targets. Use only the explicit field."
            )
        section["include_mixed_as_water_targets"] = _coerce_bool(
            legacy_mixed_targets, field="include_mixed_water_targets"
        )
        LOGGER.warning(
            "source_target_lookup.include_mixed_water_targets is deprecated; use "
            "include_mixed_as_water_targets."
        )

    if "mixed_cell_policy" in section:
        # This was role-defining in older code, but the slim lookup now uses
        # include_mixed_as_* controls. Keeping it would be misleading.
        raise ValueError(
            "source_target_lookup.mixed_cell_policy is deprecated and ignored by the "
            "slim lookup builder. Remove it and use include_mixed_as_land_sources, "
            "include_mixed_as_water_sources, and include_mixed_as_water_targets."
        )

    allowed = set(SourceTargetLookupConfig.__dataclass_fields__)
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ValueError("Unknown source_target_lookup config field(s): " + ", ".join(unknown))

    out = SourceTargetLookupConfig(**section)
    h3_resolution = int(out.h3_resolution or runtime.source_resolution)
    if h3_resolution != runtime.source_resolution or h3_resolution != runtime.target_resolution:
        raise ValueError(
            "source_target_lookup.h3_resolution must match source and target H3 resolutions. "
            f"lookup={h3_resolution} source={runtime.source_resolution} "
            f"target={runtime.target_resolution}"
        )

    max_default = float(resolved_max_distance_km(runtime, cfg))
    max_land = float(
        out.max_distance_km_land if out.max_distance_km_land is not None else max_default
    )
    max_water = float(
        out.max_distance_km_water if out.max_distance_km_water is not None else max_default
    )
    if max_land <= 0 or max_water <= 0:
        raise ValueError(
            "source_target_lookup.max_distance_km_land and max_distance_km_water must be > 0."
        )
    if out.bbox_buffer_rings < 0:
        raise ValueError("source_target_lookup.bbox_buffer_rings must be >= 0")
    if out.parallel_workers is not None and out.parallel_workers <= 0:
        raise ValueError("source_target_lookup.parallel_workers must be > 0 when provided")
    if out.max_grid_disk_k is not None and out.max_grid_disk_k < 0:
        raise ValueError("source_target_lookup.max_grid_disk_k must be >= 0 when provided")
    if out.max_candidate_pairs_per_chunk is not None and out.max_candidate_pairs_per_chunk <= 0:
        raise ValueError(
            "source_target_lookup.max_candidate_pairs_per_chunk must be > 0 when provided"
        )
    if not (0.0 <= out.min_water_fraction_for_target <= 1.0):
        raise ValueError("source_target_lookup.min_water_fraction_for_target must be in [0, 1]")
    if not (0.0 <= out.min_land_fraction_for_source <= 1.0):
        raise ValueError("source_target_lookup.min_land_fraction_for_source must be in [0, 1]")

    return SourceTargetLookupConfig(
        **{
            **asdict(out),
            "h3_resolution": h3_resolution,
            "max_distance_km_land": max_land,
            "max_distance_km_water": max_water,
        }
    )


def _lookup_partition_dir(path: Path, config_path: str | Path | None = None) -> Path:
    if config_path is not None:
        return tmp_dir_for_stage(config_path, "source_target_lookup")
    return path.parent / "_tmp" / "source_target_lookup"


def _lookup_partition_path(partition_dir: Path, chunk_index: int) -> Path:
    return partition_dir / f"chunk_{int(chunk_index):06d}.parquet"


def _json_hash(payload: Mapping[str, Any], *, length: int = 16) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[: int(length)]


def _configured_path_content_sha256(
    runtime: DistanceRuntime,
    configured_value: Any,
) -> str | None:
    """Hash the actual configured vector input, including shapefile sidecars."""

    if configured_value in {None, ""}:
        return None
    path = resolve_existing_or_relative_path(configured_value, runtime.config_dir)
    if not path.exists():
        return "missing"
    if path.is_dir():
        files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    elif path.suffix.lower() == ".shp":
        files = sorted(
            candidate for candidate in path.parent.glob(f"{path.stem}.*") if candidate.is_file()
        )
    else:
        files = [path]

    digest = hashlib.sha256()
    for candidate in files:
        digest.update(candidate.relative_to(path.parent).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _lookup_fingerprint_payload(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
) -> dict[str, Any]:
    """Return the subset of run/config state that defines the lookup universe."""

    paths_cfg = runtime.raw_config.get("paths", {}) or {}
    return {
        "algorithm_version": LOOKUP_ALGORITHM_VERSION,
        "schema": list(SOURCE_TARGET_LOOKUP_SCHEMA),
        "h3_resolution": int(runtime.source_resolution),
        "source_resolution": int(runtime.source_resolution),
        "target_resolution": int(runtime.target_resolution),
        "bbox_wgs84": tuple(float(v) for v in runtime.bbox_wgs84),
        "bbox_buffer_rings": int(lookup_cfg.bbox_buffer_rings),
        "strict_bbox_intersection": bool(lookup_cfg.strict_bbox_intersection),
        "target_domain_buffer_m": float(
            (runtime.raw_config.get("viewshed", {}) or {}).get("max_distance_m", 30_000.0)
        )
        + float((runtime.raw_config.get("viewshed", {}) or {}).get("aoi_margin_m", 1_000.0)),
        "min_land_fraction_for_source": float(lookup_cfg.min_land_fraction_for_source),
        "min_water_fraction_for_target": float(lookup_cfg.min_water_fraction_for_target),
        "max_grid_disk_k": lookup_cfg.max_grid_disk_k,
        "max_candidate_pairs_per_chunk": lookup_cfg.max_candidate_pairs_per_chunk,
        "source_chunk_size": int(cfg.source_chunk_size),
        "max_distance_km_land": float(lookup_cfg.max_distance_km_land or 0.0),
        "max_distance_km_water": float(lookup_cfg.max_distance_km_water or 0.0),
        "include_land_sources": bool(lookup_cfg.include_land_sources),
        "include_water_sources": bool(lookup_cfg.include_water_sources),
        "include_mixed_as_land_sources": bool(lookup_cfg.include_mixed_as_land_sources),
        "include_mixed_as_water_sources": bool(lookup_cfg.include_mixed_as_water_sources),
        "include_water_targets": bool(lookup_cfg.include_water_targets),
        "include_mixed_as_water_targets": bool(lookup_cfg.include_mixed_as_water_targets),
        "allow_self_pairs": bool(lookup_cfg.allow_self_pairs),
        "allow_active_role_self_pairs": bool(lookup_cfg.allow_active_role_self_pairs),
        "projected_crs": str(lookup_cfg.projected_crs or runtime.projected_crs),
        "run_version": runtime.run_version,
        "land_polygon_path": str(paths_cfg.get("land_polygon_path")),
        "water_polygon_path": str(paths_cfg.get("water_polygon_path")),
        "land_polygon_sha256": _configured_path_content_sha256(
            runtime, paths_cfg.get("land_polygon_path")
        ),
        "water_polygon_sha256": _configured_path_content_sha256(
            runtime, paths_cfg.get("water_polygon_path")
        ),
    }


def _lookup_fingerprint(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
) -> str:
    return _json_hash(_lookup_fingerprint_payload(runtime, cfg, lookup_cfg))


def _lookup_metadata_extra(
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
    *,
    n_pairs: int,
    n_source_cells: int,
    n_land_source_cells: int,
    n_water_source_cells: int,
    n_target_cells: int,
    grid_disk_k: int,
) -> dict[str, Any]:
    return {
        "step": LOOKUP_METADATA_STEP,
        "algorithm_version": LOOKUP_ALGORITHM_VERSION,
        "lookup_fingerprint": _lookup_fingerprint(runtime, cfg, lookup_cfg),
        "lookup_fingerprint_payload": _lookup_fingerprint_payload(runtime, cfg, lookup_cfg),
        "schema": list(SOURCE_TARGET_LOOKUP_SCHEMA),
        "n_pairs": int(n_pairs),
        "n_source_cells": int(n_source_cells),
        "n_land_source_cells": int(n_land_source_cells),
        "n_water_source_cells": int(n_water_source_cells),
        "n_target_cells": int(n_target_cells),
        "h3_resolution": int(runtime.source_resolution),
        "source_resolution": int(runtime.source_resolution),
        "target_resolution": int(runtime.target_resolution),
        "grid_disk_k": int(grid_disk_k),
    }


def _metadata_values_match(metadata: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for key, value in expected.items():
        if metadata.get(key) != value:
            LOGGER.info(
                "Metadata mismatch key=%s expected=%r actual=%r",
                key,
                value,
                metadata.get(key),
            )
            return False
    return True


def _validate_existing_lookup_metadata(
    lookup_path: Path,
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
) -> None:
    metadata = load_metadata_sidecar(lookup_path)
    if not metadata:
        raise ValueError(
            f"Existing source-target lookup has no metadata sidecar: {lookup_path}. "
            "Run with overwrite=True/--overwrite to rebuild."
        )

    expected = {
        "step": LOOKUP_METADATA_STEP,
        "algorithm_version": LOOKUP_ALGORITHM_VERSION,
        "lookup_fingerprint": _lookup_fingerprint(runtime, cfg, lookup_cfg),
        "schema": list(SOURCE_TARGET_LOOKUP_SCHEMA),
    }
    if _metadata_values_match(metadata, expected):
        return

    # Lookup metadata written before terrain-specific config was removed from
    # this fingerprint can still be reused when its actual lookup-defining
    # payload is identical. This lets scientific terrain changes invalidate
    # terrain partitions without forcing a geometrically unchanged lookup to
    # be deleted or globally overwritten.
    stored_payload = metadata.get("lookup_fingerprint_payload")
    if isinstance(stored_payload, Mapping):
        normalized_stored_payload = dict(stored_payload)
        normalized_stored_payload.pop("config_hash", None)
        if _json_hash(normalized_stored_payload) == _lookup_fingerprint(runtime, cfg, lookup_cfg):
            LOGGER.info(
                "Reusing legacy lookup metadata after removing unrelated "
                "full-config hash from the lookup fingerprint: %s",
                lookup_path,
            )
            return

    raise ValueError(
        f"Existing source-target lookup metadata does not match current config: {lookup_path}. "
        "Run with overwrite=True/--overwrite to rebuild."
    )


# -----------------------------------------------------------------------------
# Internal geometry / source universe helpers
# -----------------------------------------------------------------------------
