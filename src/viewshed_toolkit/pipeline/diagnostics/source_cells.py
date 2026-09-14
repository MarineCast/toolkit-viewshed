"""Trace source H3 cells through viewshed inputs and finalized artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from ..config import land_h3_path_from_config, load_yaml
from ..contracts.artifacts import final_artifact_paths


@dataclass(frozen=True)
class AuditArtifact:
    name: str
    path: Path
    source_col: str
    target_col: str | None = None
    extra_cols: tuple[str, ...] = ()


def _artifact_paths(config_path: str | Path, *, source_type: str) -> list[AuditArtifact]:
    raw, config_dir = load_yaml(config_path)
    finals = final_artifact_paths(config_path)
    source_key = str(source_type).strip().lower()
    if source_key not in {"land", "water"}:
        raise ValueError(f"source_type must be 'land' or 'water'; got {source_type!r}")

    static_weights = (
        finals.land_static_weights if source_key == "land" else finals.water_static_weights
    )
    return [
        AuditArtifact(
            name="source_universe",
            path=land_h3_path_from_config(raw, config_dir),
            source_col="h3_cell",
            extra_cols=("land_fraction", "water_fraction", "distance_to_water_m"),
        ),
        AuditArtifact(
            name="source_target_lookup",
            path=finals.source_target_lookup,
            source_col="source_h3",
            target_col="target_h3",
            extra_cols=("distance_km", "source_type"),
        ),
        AuditArtifact(
            name="distance_weights",
            path=finals.weights_path("distance_weights", source_type=source_key),
            source_col="source_h3",
            target_col="target_h3",
            extra_cols=("distance_km", "weight_distance"),
        ),
        AuditArtifact(
            name="terrain_weights",
            path=finals.weights_path("terrain_weights", source_type=source_key),
            source_col="source_h3",
            target_col="target_h3",
            extra_cols=("weight_terrain",),
        ),
        AuditArtifact(
            name="vegetation_weights",
            path=finals.weights_path("vegetation_weights", source_type=source_key),
            source_col="source_h3",
            target_col="target_h3",
            extra_cols=("weight_vegetation",),
        ),
        AuditArtifact(
            name="static_weights",
            path=static_weights,
            source_col="source_h3",
            target_col="target_h3",
            extra_cols=(
                "weight_distance",
                "weight_terrain",
                "weight_vegetation",
                "weight_static_viewability",
            ),
        ),
    ]


def _collect_source_rows(
    artifact: AuditArtifact,
    source_h3: str,
    *,
    source_type: str | None = None,
) -> pl.DataFrame:
    lf = pl.scan_parquet(str(artifact.path))
    schema = set(lf.collect_schema().names())
    if artifact.source_col not in schema:
        raise ValueError(
            f"{artifact.name} missing source column {artifact.source_col!r}: {artifact.path}"
        )
    if (
        source_type is not None
        and artifact.name == "source_target_lookup"
        and "source_type" in schema
    ):
        lf = lf.filter(pl.col("source_type").cast(pl.Utf8) == str(source_type))
    keep = [artifact.source_col]
    if artifact.target_col and artifact.target_col in schema:
        keep.append(artifact.target_col)
    keep.extend(col for col in artifact.extra_cols if col in schema)
    return (
        lf.filter(pl.col(artifact.source_col).cast(pl.Utf8) == str(source_h3))
        .select(keep)
        .collect()
    )


def _numeric_bounds(df: pl.DataFrame, column: str) -> dict[str, float | None]:
    if column not in df.columns or df.is_empty():
        return {"min": None, "max": None}
    series = df[column].cast(pl.Float64, strict=False)
    return {
        "min": series.min(),
        "max": series.max(),
    }


def _summarize_artifact(
    artifact: AuditArtifact,
    source_h3: str,
    *,
    source_type: str,
    lookup_targets: set[str] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": artifact.name,
        "path": str(artifact.path),
        "exists": artifact.path.exists(),
    }
    if not artifact.path.exists():
        return result

    rows = _collect_source_rows(artifact, source_h3, source_type=source_type)
    result["row_count"] = int(rows.height)
    result["present"] = bool(rows.height)

    if rows.is_empty():
        return result

    if artifact.target_col and artifact.target_col in rows.columns:
        targets = {
            str(value) for value in rows[artifact.target_col].cast(pl.Utf8).drop_nulls().to_list()
        }
        result["target_count"] = len(targets)
        if lookup_targets is not None and artifact.name != "source_target_lookup":
            missing_vs_lookup = sorted(lookup_targets - targets)
            extra_vs_lookup = sorted(targets - lookup_targets)
            result["missing_target_count_vs_lookup"] = len(missing_vs_lookup)
            result["extra_target_count_vs_lookup"] = len(extra_vs_lookup)
            if missing_vs_lookup:
                result["missing_targets_vs_lookup_sample"] = missing_vs_lookup[:10]
            if extra_vs_lookup:
                result["extra_targets_vs_lookup_sample"] = extra_vs_lookup[:10]
    else:
        first = rows.row(0, named=True)
        result["attributes"] = {
            key: first[key]
            for key in rows.columns
            if key != artifact.source_col and first.get(key) is not None
        }

    for column in artifact.extra_cols:
        if column in rows.columns and column not in {"source_type"}:
            bounds = _numeric_bounds(rows, column)
            if bounds["min"] is not None or bounds["max"] is not None:
                result[f"{column}_min"] = bounds["min"]
                result[f"{column}_max"] = bounds["max"]

    if "source_type" in rows.columns:
        result["source_types"] = sorted(
            {str(value) for value in rows["source_type"].cast(pl.Utf8).drop_nulls().to_list()}
        )

    return result


def audit_source_cells(
    config_path: str | Path,
    source_h3_values: list[str],
    *,
    source_type: str = "land",
) -> list[dict[str, Any]]:
    artifacts = _artifact_paths(config_path, source_type=source_type)
    lookup_artifact = next(a for a in artifacts if a.name == "source_target_lookup")

    reports: list[dict[str, Any]] = []
    for source_h3 in source_h3_values:
        report: dict[str, Any] = {
            "source_h3": str(source_h3),
            "source_type": str(source_type),
            "artifacts": [],
        }
        lookup_targets: set[str] | None = None
        if lookup_artifact.path.exists():
            lookup_rows = _collect_source_rows(
                lookup_artifact,
                source_h3,
                source_type=source_type,
            )
            if "target_h3" in lookup_rows.columns:
                lookup_targets = {
                    str(value)
                    for value in lookup_rows["target_h3"].cast(pl.Utf8).drop_nulls().to_list()
                }

        for artifact in artifacts:
            report["artifacts"].append(
                _summarize_artifact(
                    artifact,
                    source_h3,
                    source_type=source_type,
                    lookup_targets=lookup_targets,
                )
            )
        reports.append(report)
    return reports


def print_audit_report(reports: list[dict[str, Any]]) -> None:
    for report in reports:
        print(f"source_h3={report['source_h3']} source_type={report['source_type']}")
        for artifact in report["artifacts"]:
            exists = artifact.get("exists", False)
            if not exists:
                print(f"  - {artifact['name']}: missing file -> {artifact['path']}")
                continue
            if not artifact.get("present", False):
                print(f"  - {artifact['name']}: source absent")
                continue

            parts = [f"rows={artifact['row_count']}"]
            if "target_count" in artifact:
                parts.append(f"targets={artifact['target_count']}")
            if "missing_target_count_vs_lookup" in artifact:
                parts.append("missing_vs_lookup=" f"{artifact['missing_target_count_vs_lookup']}")
            if artifact.get("source_types"):
                parts.append(f"source_types={','.join(artifact['source_types'])}")
            print(f"  - {artifact['name']}: " + " ".join(parts))

            attrs = artifact.get("attributes")
            if attrs:
                print("    attributes=" + json.dumps(attrs, default=str, sort_keys=True))

            for key in sorted(artifact):
                if key.endswith("_min") or key.endswith("_max"):
                    print(f"    {key}={artifact[key]}")

            missing_sample = artifact.get("missing_targets_vs_lookup_sample")
            if missing_sample:
                print(
                    "    missing_targets_vs_lookup_sample="
                    + ",".join(str(value) for value in missing_sample)
                )
