"""Vegetation obstruction and attenuation weights for viewshed modeling.

Scientific role
---------------
This module represents vegetation effects in the viewshed model. It
separates two related but distinct concepts:

1. Source-cell vegetation context: how open or obstructed the land observer
   source cell is, based on canopy height and landcover.
2. Path vegetation attenuation: how vegetation between a source H3 cell and a
   target H3 cell reduces the line of sight after terrain has already declared
   the pair a candidate.

The persisted path-factor output used by finalization is intentionally compact:

    source_h3
        Source H3 cell.

    target_h3
        Water/target H3 cell.

    weight_vegetation
        Vegetation attenuation factor in [0, 1]. Land-source rows derive this
        from landcover and CHM path factors; water-source rows are neutral.

Scientific inputs
-----------------
- ESA WorldCover classes provide categorical vegetation/access assumptions.
- ETH Global Canopy Height provides continuous canopy-height structure.
- DEM-aligned CHM and landcover rasters are required before this module runs.
- The canonical area lookup provides the source-target pair universe. Terrain
  can still be used as an independent factor in final composition, but
  vegetation ray sampling should not generate pairs outside the lookup.
- Source and target H3 geometries define sampling domains; target sampling is
  water-aware when water geometry is available.

Method summary
--------------
The source-weight stage derives obstruction and transmission rasters from CHM
and landcover, then aggregates those rasters to land/source H3 cells using
rasterized H3 labels in the raster CRS. The path-weight stage samples rays
between source and target geometries, extracts landcover and CHM obstruction
along each ray, aggregates ray scores, and writes compact path factors.

Core assumptions
----------------
- Vegetation path weights are independent factor layers; they do not consume or
  multiply distance weights as part of their persisted output.
- Water pixels are not vegetation obstruction.
- CHM and landcover rasters must be aligned to the DEM grid.
- Categorical landcover values are interpreted through the configured class
  weights, not as measured canopy height.
- Persisted `weight_landcover` and `weight_chm` are clipped to [0, 1].

Downstream contract
-------------------
Final physical view score is composed outside this module:

    physical_view_score =
        weight_terrain * weight_vegetation

This makes vegetation effects inspectable separately from clear-sky terrain
visibility and distance decay.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# =============================================================================
# Shared vegetation defaults
# =============================================================================


DEFAULT_VEGETATION_BBOX_BUFFER_DEG = 0.02

ESA_WORLDCOVER_CLASS_WEIGHTS: dict[int, dict[str, Any]] = {
    10: {
        "class_name": "Tree cover",
        "group": "forest",
        "obstruction_multiplier": 1.00,
        "obstruction_floor": 0.10,
        "source_access_weight": 0.25,
        "include_as_source": True,
    },
    20: {
        "class_name": "Shrubland",
        "group": "shrub",
        "obstruction_multiplier": 0.65,
        "obstruction_floor": 0.15,
        "source_access_weight": 0.35,
        "include_as_source": True,
    },
    30: {
        "class_name": "Grassland",
        "group": "open_vegetation",
        "obstruction_multiplier": 0.15,
        "obstruction_floor": 0.03,
        "source_access_weight": 0.65,
        "include_as_source": True,
    },
    40: {
        "class_name": "Cropland",
        "group": "cropland",
        "obstruction_multiplier": 0.25,
        "obstruction_floor": 0.05,
        "source_access_weight": 0.35,
        "include_as_source": True,
    },
    50: {
        "class_name": "Built-up",
        "group": "built",
        "obstruction_multiplier": 0.40,
        "obstruction_floor": 0.20,
        "source_access_weight": 0.85,
        "include_as_source": True,
    },
    60: {
        "class_name": "Bare / sparse vegetation",
        "group": "bare",
        "obstruction_multiplier": 0.05,
        "obstruction_floor": 0.00,
        "source_access_weight": 0.75,
        "include_as_source": True,
    },
    70: {
        "class_name": "Snow and ice",
        "group": "snow_ice",
        "obstruction_multiplier": 0.05,
        "obstruction_floor": 0.00,
        "source_access_weight": 0.05,
        "include_as_source": True,
    },
    80: {
        "class_name": "Permanent water bodies",
        "group": "water",
        "obstruction_multiplier": 0.00,
        "obstruction_floor": 0.00,
        "source_access_weight": 0.00,
        "include_as_source": False,
    },
    90: {
        "class_name": "Herbaceous wetland",
        "group": "wetland",
        "obstruction_multiplier": 0.45,
        "obstruction_floor": 0.10,
        "source_access_weight": 0.20,
        "include_as_source": True,
    },
    95: {
        "class_name": "Mangroves",
        "group": "mangrove",
        "obstruction_multiplier": 0.90,
        "obstruction_floor": 0.20,
        "source_access_weight": 0.10,
        "include_as_source": True,
    },
    100: {
        "class_name": "Moss and lichen",
        "group": "moss_lichen",
        "obstruction_multiplier": 0.10,
        "obstruction_floor": 0.03,
        "source_access_weight": 0.15,
        "include_as_source": True,
    },
}


DEFAULT_VEGETATION_WEIGHT_SETTINGS: dict[str, Any] = {
    "nodata": {
        "chm": 255,
        "chm_obstruction": -9999,
        "landcover": 0,
        "output_float": -9999,
    },
    "chm_obstruction": {
        "source": "derive_if_missing",
        "method": "threshold_piecewise",
        "thresholds_m": {
            "no_obstruction_max": 2,
            "low_obstruction_max": 5,
            "medium_obstruction_max": 15,
        },
        "values": {
            "no_obstruction": 0.0,
            "low_obstruction": 0.25,
            "medium_obstruction": 0.65,
            "high_obstruction": 1.0,
        },
    },
    "surface_obstruction": {
        "method": "max_chm_times_multiplier_floor",
        "chm_nodata_policy": "treat_as_zero",
        "clamp_min": 0.0,
        "clamp_max": 1.0,
    },
    "h3_aggregation": {
        "id_column": "h3_cell",
        "geometry_column": "geometry",
        "drop_geometry": False,
        "metrics": {
            "surface_obstruction": {"stats": ["mean", "median", "max", "std"]},
            "surface_transmission": {"stats": ["mean", "median", "min", "std"]},
            "source_access_weight": {"stats": ["mean", "median", "max"]},
            "chm_obstruction": {"stats": ["mean", "median", "max"]},
        },
        "dominant_landcover": True,
        "landcover_proportions": True,
        "final_weight": {
            "name": "land_source_vegetation_weight",
            "method": "source_access_mean_times_transmission_mean",
            "clamp_min": 0.0,
            "clamp_max": 1.0,
            "invalid_policy": "neutral",
            "invalid_value": 1.0,
        },
    },
}


DEFAULT_VEGETATION_PATH_SETTINGS: dict[str, Any] = {
    "columns": {
        "source_id": "source_h3",
        "target_id": "target_h3",
        "target_id_output": "target_h3",
        "clear_sky_weight": "weight_terrain",
        "distance_km": "distance_km",
        "source_weight_column": "land_source_vegetation_weight",
    },
    "nodata": {
        "surface_transmission": -9999,
        "surface_obstruction": -9999,
        "landcover": 0,
    },
    "landcover": {
        "water_codes": [80],
        "ignore_codes_for_vegetation_path": [80],
        "unknown_policy": "ignore",
    },
    "geometry": {"missing_source_policy": "derive_h3_polygon"},
    "sampling": {
        "n_points_per_pair": 15,
        "pair_mode": "paired",
        "source_point_strategy": "deterministic_random",
        "target_point_strategy": "deterministic_random",
        "include_centroid": True,
        "random_seed": 42,
        "enforce_within_polygon": True,
        "max_sampling_attempts_per_polygon": 1000,
    },
    "ray_tracing": {
        "method": "raster_bresenham",
        "include_endpoint_pixels": True,
        "source_near_field_policy": "skip_first_distance_m",
        "skip_first_distance_m": 60,
        "include_intervening_islands": True,
        "no_land_pixels_policy": "transmission_1",
    },
    "path_transmission": {
        "method": "mean_transmission_over_land_pixels",
        "selected_path_weight": "mean",
        "clamp_min": 0.0,
        "clamp_max": 1.0,
        "beta_per_meter": 0.002,
    },
    "aggregation": {
        "ray_stats": ["mean", "median", "min", "max", "std", "p10", "p25", "p75"],
        "pair_weight_stat_for_final": "mean",
    },
    "combine": {
        "include_source_cell_weight": True,
        "source_weight_missing_policy": "use_1",
    },
    "filtering": {
        "max_distance_km": None,
        "min_terrain_visibility_support": None,
        "min_clear_sky_weight": 0.0,
        "only_visible_pairs": True,
    },
    "performance": {
        "chunk_size_pairs": 5000,
        "n_workers": 1,
        "overwrite": False,
        "count_candidates_before_run": False,
    },
    "raster_access_mode": "windowed",
    "debug": {
        "max_pairs": 100,
        "include_geometry": True,
    },
}


def _deep_merge_dicts(
    base: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge vegetation config overrides into default settings."""
    out = deepcopy(base)

    for key, value in dict(overrides or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge_dicts(out[key], value)
        else:
            out[key] = deepcopy(value)

    return out


def vegetation_weight_settings(
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return vegetation source-cell weight settings with defaults applied."""
    settings = deepcopy(DEFAULT_VEGETATION_WEIGHT_SETTINGS)
    settings["landcover_classes"] = deepcopy(ESA_WORLDCOVER_CLASS_WEIGHTS)

    return _deep_merge_dicts(settings, overrides or {})


def vegetation_path_settings(
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return vegetation path weight settings with defaults applied."""
    settings = deepcopy(DEFAULT_VEGETATION_PATH_SETTINGS)

    return _deep_merge_dicts(settings, overrides or {})
