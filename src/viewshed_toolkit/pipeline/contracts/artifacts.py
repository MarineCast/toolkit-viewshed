"""Artifact schemas and path contracts shared by all pipeline phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..config.paths import resolve_path, viewshed_domain_relative
from ..config.schema import load_yaml
from .pairs import DISTANCE_OUTPUT_SCHEMA, SOURCE_TARGET_LOOKUP_SCHEMA, SOURCE_TYPES

VEGETATION_STATUS_COMPUTED = "computed"
VEGETATION_STATUS_NOT_APPLICABLE = "not_applicable"

FINAL_SCHEMAS: dict[str, tuple[str, ...]] = {
    "source_target_lookup": SOURCE_TARGET_LOOKUP_SCHEMA,
    "target_water_area": (
        "target_h3",
        "total_water_area_m2",
        "equivalent_water_pixel_count",
    ),
    "distance_weights": DISTANCE_OUTPUT_SCHEMA,
    "terrain_weights": ("source_h3", "target_h3", "weight_terrain"),
    "canopy_los_weights": ("source_h3", "target_h3", "weight_canopy_los"),
    "dual_surface_factors": (
        "source_h3",
        "target_h3",
        "weight_terrain",
        "weight_canopy_los_raw",
        "weight_canopy_los",
        "source_type",
        "weight_vegetation",
        "vegetation_status",
        "vegetation_provenance",
    ),
    "source_target_clear_sky": (
        "source_h3",
        "target_h3",
        "terrain_binary",
        "aggregation_method",
        "unweighted_los_observed",
        "any_observer_support_fraction",
        "union_visible_target_fraction",
        "joint_los_fraction",
        "distance_weighted_los_fraction",
        "los_distance_weight_sum",
        "sample_points_requested",
        "sample_points_actual",
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
        "n_observers",
        "target_water_pixel_count",
        "target_water_sample_count",
        "pixel_stride",
        "visible_area_km2",
        "target_water_area_km2",
        "weight_terrain",
    ),
    "vegetation_weights": (
        "source_h3",
        "target_h3",
        "source_type",
        "weight_vegetation",
        "vegetation_status",
    ),
    "static_weights": (
        "source_h3",
        "target_h3",
        "weight_terrain",
        "weight_distance",
        "weight_vegetation",
        "weight_static_viewability",
    ),
    "observation_geometry": (
        "source_h3",
        "target_h3",
        "source_type",
        "distance_km",
        "line_of_sight_support",
        "line_of_sight_state",
        "distance_detection_weight",
        "distance_detection_state",
        "distance_weighted_los_support",
        "distance_weighted_los_state",
        "vegetation_attenuation",
        "vegetation_state",
        "physical_viewability",
        "physical_viewability_state",
        "distance_adjusted_viewability",
        "distance_adjusted_viewability_state",
        "legacy_static_schema",
        "COMPONENT_PROVENANCE_JSON",
        "DATA_COVERAGE_STATE",
        "SOURCE_COVERAGE_STATE",
        "GENERATION_ID",
        "CONFIG_HASH",
        "SOURCE_HASHES_JSON",
        "KNOWLEDGE_TIME_UTC",
        "SOURCE_VINTAGES_JSON",
        "HISTORICAL_RECONSTRUCTION",
    ),
}

STATIC_ARTIFACT_SCHEMA_VERSION = "viewshed_static_pair_v2"
OBSERVATION_GEOMETRY_SCHEMA_VERSION = "3.0.0-research"


@dataclass(frozen=True)
class FinalArtifactPaths:
    """Concrete artifact paths for one H3 resolution."""

    output_dir: Path
    final_output_dir: Path
    h3_resolution: int
    h3_geometry: Path
    source_target_lookup: Path
    target_water_area: Path
    land_output_dir: Path
    land_weights_dir: Path
    distance_weights: Path
    terrain_weights: Path
    canopy_los_weights: Path
    dual_surface_factors: Path
    source_target_clear_sky: Path
    vegetation_weights: Path
    land_view_score: Path
    ocean_output_dir: Path
    ocean_weights_dir: Path
    ocean_distance_weights: Path
    ocean_terrain_weights: Path
    ocean_source_target_clear_sky: Path
    ocean_vegetation_weights: Path
    ocean_los_weights: Path
    ocean_physical_weights: Path
    ocean_view_score: Path
    land_static_weights: Path
    water_static_weights: Path
    land_observation_geometry: Path
    water_observation_geometry: Path

    def weights_path(self, artifact: str, *, source_type: str = "land") -> Path:
        source_type = normalize_source_type(source_type)
        mapping = {
            ("land", "distance_weights"): self.distance_weights,
            ("land", "terrain_weights"): self.terrain_weights,
            ("land", "vegetation_weights"): self.vegetation_weights,
            ("water", "distance_weights"): self.ocean_distance_weights,
            ("water", "terrain_weights"): self.ocean_terrain_weights,
            ("water", "vegetation_weights"): self.ocean_vegetation_weights,
        }
        try:
            return mapping[(source_type, artifact)]
        except KeyError as exc:
            raise KeyError(
                f"Unknown source/artifact combination: source_type={source_type!r}, "
                f"artifact={artifact!r}"
            ) from exc

    def as_dict(self, *, source_type: str = "land") -> dict[str, Path]:
        return {
            "h3_geometry": self.h3_geometry,
            "source_target_lookup": self.source_target_lookup,
            "target_water_area": self.target_water_area,
            "distance_weights": self.weights_path("distance_weights", source_type=source_type),
            "terrain_weights": self.weights_path("terrain_weights", source_type=source_type),
            "vegetation_weights": self.weights_path("vegetation_weights", source_type=source_type),
        }

    def all_final_paths(self) -> dict[str, Path]:
        return {
            "h3_geometry": self.h3_geometry,
            "source_target_lookup": self.source_target_lookup,
            "land_distance_weights": self.distance_weights,
            "land_terrain_weights": self.terrain_weights,
            "land_canopy_los_weights": self.canopy_los_weights,
            "land_dual_surface_factors": self.dual_surface_factors,
            "land_vegetation_weights": self.vegetation_weights,
            "land_source_target_clear_sky": self.source_target_clear_sky,
            "water_distance_weights": self.ocean_distance_weights,
            "water_terrain_weights": self.ocean_terrain_weights,
            "water_vegetation_weights": self.ocean_vegetation_weights,
            "water_source_target_clear_sky": self.ocean_source_target_clear_sky,
            "water_los_weights": self.ocean_los_weights,
            "water_physical_weights": self.ocean_physical_weights,
            "water_view_score": self.ocean_view_score,
            "land_view_score": self.land_view_score,
            "land_static_weights": self.land_static_weights,
            "water_static_weights": self.water_static_weights,
            "land_observation_geometry": self.land_observation_geometry,
            "water_observation_geometry": self.water_observation_geometry,
        }


def normalize_source_type(source_type: str) -> str:
    out = str(source_type).strip().lower()
    if out not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}; got {source_type!r}")
    return out


def h3_resolution_from_raw(raw: Mapping[str, object]) -> int:
    h3 = raw.get("h3", {}) or {}
    if isinstance(h3, Mapping):
        return int(h3.get("source_resolution", h3.get("resolution", raw.get("h3_resolution", 6))))
    return int(raw.get("h3_resolution", 6))


def output_dir_from_raw(raw: Mapping[str, object], config_dir: Path) -> Path:
    paths = raw.get("paths", {}) or {}
    if isinstance(paths, Mapping):
        return resolve_path(paths.get("output_dir", viewshed_domain_relative()), config_dir)
    return resolve_path(viewshed_domain_relative(), config_dir)


def final_output_dir_from_raw(raw: Mapping[str, object], config_dir: Path) -> Path:
    paths = raw.get("paths", {}) or {}
    if isinstance(paths, Mapping) and paths.get("final_output_dir") is not None:
        return resolve_path(paths["final_output_dir"], config_dir)
    return output_dir_from_raw(raw, config_dir)


def final_artifact_paths(config_path: str | Path) -> FinalArtifactPaths:
    raw, config_dir = load_yaml(config_path)
    return final_artifact_paths_from_raw(raw, config_dir)


def final_artifact_paths_from_raw(
    raw: Mapping[str, object], config_dir: Path
) -> FinalArtifactPaths:
    res = h3_resolution_from_raw(raw)
    output_dir = output_dir_from_raw(raw, config_dir)
    final_output_dir = final_output_dir_from_raw(raw, config_dir)
    land_output_dir = output_dir / "land"
    land_weights_dir = land_output_dir / "weights_files"
    ocean_output_dir = output_dir / "ocean"
    ocean_weights_dir = ocean_output_dir / "weights_files"
    return FinalArtifactPaths(
        output_dir=output_dir,
        final_output_dir=final_output_dir,
        h3_resolution=res,
        h3_geometry=output_dir / "lookup" / f"H3_GEOMETRY_H3R{res}.parquet",
        source_target_lookup=output_dir / "lookup" / f"SOURCE_TARGET_LOOKUP_H3R{res}.parquet",
        target_water_area=output_dir / "lookup" / f"TARGET_WATER_AREA_H3R{res}.parquet",
        land_output_dir=land_output_dir,
        land_weights_dir=land_weights_dir,
        distance_weights=land_weights_dir / f"DISTANCE_WEIGHTS_H3R{res}.parquet",
        terrain_weights=land_weights_dir / f"TERRAIN_WEIGHTS_H3R{res}.parquet",
        canopy_los_weights=land_weights_dir / f"CANOPY_LOS_WEIGHTS_H3R{res}.parquet",
        dual_surface_factors=land_weights_dir / f"DUAL_SURFACE_FACTORS_H3R{res}.parquet",
        source_target_clear_sky=land_weights_dir / f"SOURCE_TARGET_CLEAR_SKY_H3R{res}.parquet",
        vegetation_weights=land_weights_dir / f"VEGETATION_WEIGHTS_H3R{res}.parquet",
        land_view_score=land_weights_dir / f"LAND_PHYSICAL_VIEW_SCORE_H3R{res}.parquet",
        ocean_output_dir=ocean_output_dir,
        ocean_weights_dir=ocean_weights_dir,
        ocean_distance_weights=ocean_weights_dir / f"DISTANCE_WEIGHTS_H3R{res}.parquet",
        ocean_terrain_weights=ocean_weights_dir / f"TERRAIN_WEIGHTS_H3R{res}.parquet",
        ocean_source_target_clear_sky=(
            ocean_weights_dir / f"SOURCE_TARGET_CLEAR_SKY_H3R{res}.parquet"
        ),
        ocean_vegetation_weights=ocean_weights_dir / f"VEGETATION_WEIGHTS_H3R{res}.parquet",
        ocean_los_weights=ocean_weights_dir / f"WATER_LOS_WEIGHTS_BY_HEIGHT_H3R{res}.parquet",
        ocean_physical_weights=ocean_weights_dir / f"WATER_PHYSICAL_WEIGHTS_H3R{res}.parquet",
        ocean_view_score=ocean_weights_dir / f"WATER_PHYSICAL_VIEW_SCORE_H3R{res}.parquet",
        land_static_weights=final_output_dir / f"LAND_STATIC_WEIGHTS_R{res}.parquet",
        water_static_weights=final_output_dir / f"WATER_STATIC_WEIGHTS_R{res}.parquet",
        land_observation_geometry=final_output_dir / f"LAND_OBSERVATION_GEOMETRY_R{res}.parquet",
        water_observation_geometry=final_output_dir / f"WATER_OBSERVATION_GEOMETRY_R{res}.parquet",
    )


def tmp_dir_for_stage(config_path: str | Path, stage: str) -> Path:
    paths = final_artifact_paths(config_path)
    safe_stage = str(stage).strip().strip("/")
    if not safe_stage:
        raise ValueError("stage must be non-empty")
    return paths.output_dir / "_tmp" / safe_stage


__all__ = [
    "FINAL_SCHEMAS",
    "OBSERVATION_GEOMETRY_SCHEMA_VERSION",
    "STATIC_ARTIFACT_SCHEMA_VERSION",
    "VEGETATION_STATUS_COMPUTED",
    "VEGETATION_STATUS_NOT_APPLICABLE",
    "FinalArtifactPaths",
    "final_artifact_paths",
    "final_artifact_paths_from_raw",
    "final_output_dir_from_raw",
    "h3_resolution_from_raw",
    "normalize_source_type",
    "output_dir_from_raw",
    "tmp_dir_for_stage",
]
