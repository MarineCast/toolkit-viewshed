"""YAML loading and schema normalization for viewshed configuration.

Scientific and operational role
-------------------------------
This module owns the conversion from the compact human-layer YAML shape to the
nested viewshed runtime schema consumed by the pipeline. It intentionally does
not contain terrain, distance, or vegetation modeling logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from viewshed_toolkit._internal.config.common_areas import bbox_from_config as common_bbox_from_config
from viewshed_toolkit._internal.config.data import load_data_config
from viewshed_toolkit._internal.config.paths import resolve_config_path


def load_yaml_config(config_path: str | Path) -> dict[str, Any]:
    config_path = resolve_config_path(config_path)
    raw = load_data_config(config_path, domains="HUMAN_LAYER")
    is_normalized_runtime = any(
        key in raw
        for key in (
            "region",
            "h3",
            "batch",
            "vegetation_weights",
            "vegetation_path_weights",
        )
    )
    if "viewshed" in raw and not is_normalized_runtime:
        viewshed_raw = raw["viewshed"]
        if not isinstance(viewshed_raw, dict):
            raise ValueError("Config section 'viewshed' must be a mapping.")
        raw = viewshed_raw
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must parse to a dictionary: {config_path}")
    return normalize_viewshed_config(raw)


def normalize_viewshed_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Expand the compact user-facing viewshed config into internal sections.

    The active YAML intentionally exposes only the run-level knobs people should
    tune often. Existing implementation modules still consume nested sections
    such as `region`, `viewshed`, `h3`, `batch`, `canopy`, and
    `vegetation_weights`, so this function provides the compatibility layer.
    """
    cfg = dict(raw or {})
    has_nested = any(
        key in cfg
        for key in (
            "region",
            "viewshed",
            "h3",
            "batch",
            "canopy",
            "vegetation_weights",
            "run",
            "paths",
        )
    )

    if has_nested:
        out = {
            "run": {
                "name": "puget_sound_land_h3_regional_dem_v1",
                "version": "gdal_3dep_puget_sound_r7_30km_v1",
                "skip_existing_partitions": True,
                "combine_final_parquet": False,
                "write_pair_geometry": False,
                **dict(cfg.get("run", {}) or {}),
            },
            **cfg,
        }
        resolution_m = out.get("resolution_m")
        if resolution_m is not None:
            out.setdefault("viewshed", {}).setdefault("dem_resolution_m", resolution_m)
            out.setdefault("vegetation_weights", {}).setdefault("resolution_m", resolution_m)
        if "bbox_wgs84" in out:
            out.setdefault("region", {}).setdefault("bbox_wgs84", out["bbox_wgs84"])
        elif "area" in out:
            out.setdefault("region", {}).setdefault(
                "bbox_wgs84",
                common_bbox_from_config(out),
            )
        for key in (
            "observer_eye_height_m",
            "target_height_m",
            "max_distance_m",
            "sea_level_m",
        ):
            if key in out:
                out.setdefault("viewshed", {}).setdefault(key, out[key])
        h3_resolution = out.get("h3_resolution")
        if h3_resolution is not None:
            out.setdefault("h3", {}).setdefault("source_resolution", h3_resolution)
            out.setdefault("h3", {}).setdefault("target_resolution", h3_resolution)
        for key in (
            "source_sampling_mode",
            "sample_points_per_source_cell",
            "min_sample_points_per_source_cell",
            "aggregation_mode",
            "pixel_stride",
        ):
            if key in out:
                out.setdefault("h3", {}).setdefault(key, out[key])
        if "max_workers" in out:
            out.setdefault("batch", {}).setdefault("max_workers", out["max_workers"])
        chm_resolution = out.get("chm_resolution_m")
        if chm_resolution is not None:
            out.setdefault("canopy", {}).setdefault("resolution_m", chm_resolution)
        vegetation_data = out.get("vegetation_data", {})
        if isinstance(vegetation_data, dict):
            canopy_cfg = vegetation_data.get("canopy")
            if isinstance(canopy_cfg, dict):
                out.setdefault("canopy", {}).update(
                    {key: value for key, value in canopy_cfg.items() if key not in out["canopy"]}
                )
            landcover_cfg = vegetation_data.get("landcover")
            if isinstance(landcover_cfg, dict):
                vegetation_data["landcover"] = landcover_cfg
        return out

    resolution_m = int(cfg.get("resolution_m", cfg.get("dem_resolution_m", 30)))
    chm_resolution_m = int(cfg.get("chm_resolution_m", resolution_m))
    h3_resolution = int(cfg.get("h3_resolution", 7))
    bbox = common_bbox_from_config(cfg)

    return {
        "run": {
            "name": "puget_sound_land_h3_regional_dem_v1",
            "version": (
                f"gdal_3dep_puget_sound_r{h3_resolution}_"
                f"{int(float(cfg.get('max_distance_m', 30000)) / 1000)}km_v1"
            ),
            "skip_existing_partitions": True,
            "combine_final_parquet": False,
            "write_pair_geometry": False,
        },
        "region": {
            "bbox_wgs84": bbox,
            "coastal_buffer_m": 6000,
            "min_source_cell_land_fraction": 0.01,
            "max_source_cell_water_fraction": 0.95,
            "cleanup_natural_earth": False,
        },
        "viewshed": {
            "dem_resolution_m": resolution_m,
            "backend": "gdal",
            "surface_model": "bare_earth",
            "canopy_resampling": "max",
            "canopy_nodata_policy": "error",
            "minimum_canopy_height_m": 0.0,
            "observer_canopy_clearance_radius_m": 0.0,
            "observer_eye_height_m": float(cfg.get("observer_eye_height_m", 1.7)),
            "target_height_m": float(cfg.get("target_height_m", 1.0)),
            "max_distance_m": float(cfg.get("max_distance_m", 30000)),
            "aoi_margin_m": 1000,
            "curvature_coefficient": 0.85714,
            "earth_radius_m": 6378137.0,
            "crs_projected": "EPSG:32610",
            "sea_level_m": float(cfg.get("sea_level_m", 0.0)),
            "dem_nodata_policy": str(cfg.get("dem_nodata_policy", "error")),
            "dem_nodata_barrier_height_m": float(cfg.get("dem_nodata_barrier_height_m", 100_000.0)),
        },
        "h3": {
            "source_resolution": h3_resolution,
            "target_resolution": h3_resolution,
            "source_sampling_mode": str(cfg.get("source_sampling_mode", "fixed")),
            "sample_points_per_source_cell": int(cfg.get("sample_points_per_source_cell", 5)),
            "min_sample_points_per_source_cell": int(
                cfg.get("min_sample_points_per_source_cell", 1)
            ),
            "aggregation_mode": str(cfg.get("aggregation_mode", "full")),
            "pixel_stride": int(cfg.get("pixel_stride", 1)),
            "min_visible_sampled_pixel_count": 1,
            "min_visible_area_km2": 0.0,
            "include_centroid": True,
        },
        "batch": {
            "max_workers": int(cfg.get("max_workers", 8)),
            "batch_size_cells": 7,
            "strategy": "h3_parent",
            "max_batch_aoi_pixels": 20000000,
            "max_estimated_batch_memory_mb": 768,
        },
        "distance_weight": {
            "version": "distance_v001",
            "selected_model": "logistic",
            "logistic_d50_km": 9.0,
            "logistic_slope_km": 2.7,
            "hard_cutoff_km": float(cfg.get("max_distance_m", 30000)) / 1000.0,
        },
        "canopy": {
            "resolution_m": chm_resolution_m,
        },
        "vegetation_weights": {
            "resolution_m": resolution_m,
        },
    }


def load_yaml(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = resolve_config_path(path)
    raw = load_yaml_config(config_path)
    return raw, config_path.parent


def resolve_distance_weight_section(
    config: dict[str, Any], *, error_on_deprecated: bool = True
) -> dict[str, Any]:
    canonical = config.get("distance_weight")
    deprecated = config.get("distance_weights")

    if deprecated is not None:
        if canonical is not None:
            raise ValueError(
                "Config contains both `distance_weight` (canonical) and deprecated `distance_weights`. "
                "Remove `distance_weights` to avoid ambiguity."
            )
        if error_on_deprecated:
            raise ValueError(
                "Config key `distance_weights` is deprecated. Rename it to `distance_weight`."
            )
        import warnings

        warnings.warn(
            "Config key `distance_weights` is deprecated; use `distance_weight`.",
            DeprecationWarning,
            stacklevel=2,
        )
        canonical = deprecated

    if canonical is None:
        return {}
    if not isinstance(canonical, dict):
        raise ValueError("Config field `distance_weight` must be a dictionary.")
    return canonical
