"""Local dependency inventory for partial-run manifest validation."""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from .artifacts import final_artifact_paths_from_raw


def partial_run_inputs(app: AppConfig, stages: tuple[str, ...]) -> tuple[Path, ...]:
    """Inventory canonical stage inputs; existence is recorded, never zero-filled."""
    paths = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    geometry = (app.paths.land_polygon_path, app.paths.water_polygon_path)
    sources = (app.paths.land_h3_path, app.paths.source_cells_path)
    lookup = (paths.source_target_lookup,)
    factors = (
        paths.terrain_weights,
        paths.ocean_terrain_weights,
        paths.distance_weights,
        paths.ocean_distance_weights,
        paths.vegetation_weights,
        paths.ocean_vegetation_weights,
        paths.dual_surface_factors,
        paths.source_target_clear_sky,
        paths.ocean_source_target_clear_sky,
    )
    final = (
        paths.land_static_weights,
        paths.water_static_weights,
        paths.land_observation_geometry,
        paths.water_observation_geometry,
    )
    by_stage = {
        "download-data": (app.paths.raw_dem_dir,),
        "build-land-cells": geometry,
        "prepare-source-target-lookup": (*geometry, *sources),
        "build-distance-weights": lookup,
        "build-dual-surface-canopy-weights": (
            *geometry,
            *sources,
            *lookup,
            app.paths.regional_dem_path,
            app.paths.canopy_height_path,
        ),
        "terrain-weight": (*geometry, *lookup),
        "build-vegetation-path-weights": lookup,
        "finalize-viewshed-lookups": (
            *geometry,
            *lookup,
            *factors,
            app.paths.regional_dem_path,
            app.paths.canopy_height_path,
        ),
        "export-static-maps": (
            *final,
            paths.land_static_weights.parent / "viewshed-generation.json",
        ),
    }
    return tuple(sorted({path.resolve() for stage in stages for path in by_stage[stage]}))
