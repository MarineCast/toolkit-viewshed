"""Raster and observer context preparation for one viewshed batch."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import numpy as np
import rasterio

from viewshed_toolkit._internal.geo.geometry import safe_polygonal_union
from viewshed_toolkit._internal.geo.raster import validate_raster_grid_alignment

from ...config import (
    AppConfig,
    BatchContext,
    current_process_memory_mb,
    timer,
    viewshed_config_from_app_config,
)
from ..elevation.canopy import (
    build_observer_grounded_canopy_surface_from_base,
    normalize_surface_model,
)
from ..elevation.terrain import (
    apply_los_dem_nodata_policy,
    clip_raster_to_bounds,
    estimated_batch_memory_mb,
    repair_observer_endpoint_nodata,
    validate_batch_guardrails,
)
from .batching import make_batch_id
from .inputs import load_source_cells
from .observers import CRS_WGS84, build_observers_from_sample_points
from .raster_stack import ensure_canonical_raster_stack
from .sampling import prepare_source_samples, summarize_source_sampling_diagnostics

LOGGER = logging.getLogger(__name__)


def prepare_batch_context(
    app: AppConfig,
    source_cells: Sequence[str],
    batch_index: int,
    source_cells_gdf: gpd.GeoDataFrame | None = None,
    source_sample_points_gdf: gpd.GeoDataFrame | None = None,
) -> BatchContext:
    if not source_cells:
        raise ValueError("source_cells cannot be empty.")

    config = viewshed_config_from_app_config(app)
    batch_id = make_batch_id(app, batch_index, list(source_cells))
    batch_start = timer(f"prepare_batch_context:{batch_id}")
    batch_timer = batch_start.__enter__()
    source_metadata = (
        source_cells_gdf.copy() if source_cells_gdf is not None else load_source_cells(app)
    )
    source_metadata = source_metadata[
        source_metadata["h3_cell"].astype(str).isin({str(cell) for cell in source_cells})
    ].copy()
    if source_sample_points_gdf is None:
        all_sample_points, sampling_diagnostics = prepare_source_samples(
            app,
            source_metadata,
            source_cells,
        )
    else:
        all_sample_points = source_sample_points_gdf[
            source_sample_points_gdf["source_h3_cell"]
            .astype(str)
            .isin({str(cell) for cell in source_cells})
        ].copy()
        if all_sample_points.crs is None:
            all_sample_points = all_sample_points.set_crs(CRS_WGS84)
        elif str(all_sample_points.crs) != CRS_WGS84:
            all_sample_points = all_sample_points.to_crs(CRS_WGS84)
        diagnostic_columns = [
            "source_h3_cell",
            "source_type",
            "active_source_fraction",
            "source_sampling_mode",
            "source_sampling_projected_crs",
            "source_sampling_candidate_grid_side",
            "source_sampling_max_design_points",
            "source_sampling_component_count",
            "sample_points_max",
            "sample_points_min",
            "sample_points_requested",
            "sample_points_actual",
        ]
        sampling_diagnostics = (
            all_sample_points[diagnostic_columns]
            .drop_duplicates("source_h3_cell")
            .rename(columns={"source_h3_cell": "source_h3"})
        )
    missing_sample_cells = sorted(
        {str(cell) for cell in source_cells} - set(all_sample_points["source_h3_cell"].astype(str))
    )
    if missing_sample_cells:
        raise ValueError(
            "Missing prepared observer samples for source cells: " f"{missing_sample_cells[:10]}"
        )
    batch_sampling_summary = summarize_source_sampling_diagnostics(
        sampling_diagnostics.assign(
            duplicate_sample_coordinate_count=(
                0
                if "duplicate_sample_coordinate_count" not in sampling_diagnostics.columns
                else sampling_diagnostics["duplicate_sample_coordinate_count"]
            )
        )
    )
    LOGGER.info(
        "source_sampling_batch batch_id=%s sampling_mode=%s max_samples_per_cell=%d "
        "min_samples_per_cell=%d requested_min=%s requested_median=%s "
        "requested_mean=%s requested_max=%s actual_min=%s actual_median=%s "
        "actual_mean=%s actual_max=%s",
        batch_id,
        app.h3.source_sampling_mode,
        int(app.h3.sample_points_per_source_cell),
        int(app.h3.min_sample_points_per_source_cell),
        batch_sampling_summary["sample_points_requested_min"],
        batch_sampling_summary["sample_points_requested_median"],
        batch_sampling_summary["sample_points_requested_mean"],
        batch_sampling_summary["sample_points_requested_max"],
        batch_sampling_summary["sample_points_actual_min"],
        batch_sampling_summary["sample_points_actual_median"],
        batch_sampling_summary["sample_points_actual_mean"],
        batch_sampling_summary["sample_points_actual_max"],
    )
    all_observers = build_observers_from_sample_points(all_sample_points, config)
    observers_projected = all_observers.to_crs(config.crs_projected)

    observer_buffers = [
        geom.buffer(config.max_distance_m + config.aoi_margin_m)
        for geom in observers_projected.geometry
    ]
    aoi_projected = gpd.GeoDataFrame(
        [{"name": f"{batch_id}_aoi"}],
        geometry=[safe_polygonal_union(gpd.GeoSeries(observer_buffers, crs=config.crs_projected))],
        crs=config.crs_projected,
    )
    aoi_wgs84 = aoi_projected.to_crs(CRS_WGS84)

    batch_dir = app.paths.output_dir / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    surface_model = normalize_surface_model(config.surface_model)
    canonical_stack = ensure_canonical_raster_stack(
        app,
        include_canopy=surface_model == "canopy",
    )
    raw_dem = app.paths.regional_dem_path
    dem_projected = canonical_stack.projected_dem_path
    dem_clip = batch_dir / f"{batch_id}_3dep_{config.dem_resolution_m}m_clip.tif"
    water_mask_path = batch_dir / f"{batch_id}_water_mask.tif"
    endpoint_dem = batch_dir / f"{batch_id}_dem_endpoint_water_flattened.tif"

    clip_raster_to_bounds(
        dem_projected,
        dem_clip,
        tuple(aoi_projected.total_bounds),
        overwrite=app.run.overwrite,
        compress=app.raster.intermediate_compress,
        block_size=app.raster.block_size,
    )
    clip_raster_to_bounds(
        canonical_stack.water_mask_path,
        water_mask_path,
        tuple(aoi_projected.total_bounds),
        overwrite=app.run.overwrite,
        compress=app.raster.intermediate_compress,
        block_size=app.raster.block_size,
    )
    # Endpoint rasters are refreshed from the immutable canonical source before
    # applying any observer-specific coastal nodata repairs.
    clip_raster_to_bounds(
        canonical_stack.endpoint_dem_path,
        endpoint_dem,
        tuple(aoi_projected.total_bounds),
        overwrite=True,
        compress=app.raster.intermediate_compress,
        block_size=app.raster.block_size,
    )
    validate_raster_grid_alignment(
        endpoint_dem,
        water_mask_path,
        label_a="endpoint_dem",
        label_b="water_mask",
    )
    observer_endpoint_diagnostics = repair_observer_endpoint_nodata(
        endpoint_dem,
        raw_dem,
        observers_projected,
    )
    endpoint_dem_nodata_diagnostics = apply_los_dem_nodata_policy(
        endpoint_dem,
        policy=config.dem_nodata_policy,
        opaque_barrier_height_m=config.dem_nodata_barrier_height_m,
    )

    analysis_dem = endpoint_dem
    aligned_canopy_height_path: Path | None = None
    canopy_surface_result = None
    if surface_model == "canopy":
        if canonical_stack.aligned_canopy_path is None:
            raise RuntimeError("Canonical canopy stack is missing the aligned CHM.")
        if canonical_stack.base_canopy_surface_path is None:
            raise RuntimeError("Canonical canopy stack is missing the base DEM+CHM surface.")
        aligned_canopy_height_path = batch_dir / f"{batch_id}_{app.config_hash}_chm_aligned.tif"
        canopy_base_clip_path = (
            batch_dir / f"{batch_id}_{app.config_hash}_dem_plus_chm_base_clip.tif"
        )
        canopy_surface_path = (
            batch_dir / f"{batch_id}_{app.config_hash}_dem_plus_chm_obstacle_surface.tif"
        )
        clip_raster_to_bounds(
            canonical_stack.aligned_canopy_path,
            aligned_canopy_height_path,
            tuple(aoi_projected.total_bounds),
            overwrite=app.run.overwrite,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
        clip_raster_to_bounds(
            canonical_stack.base_canopy_surface_path,
            canopy_base_clip_path,
            tuple(aoi_projected.total_bounds),
            overwrite=app.run.overwrite,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
        canopy_surface_result = build_observer_grounded_canopy_surface_from_base(
            endpoint_dem_path=endpoint_dem,
            water_mask_path=water_mask_path,
            aligned_canopy_path=aligned_canopy_height_path,
            base_surface_path=canopy_base_clip_path,
            output_surface_path=canopy_surface_path,
            observers_projected=observers_projected,
            nodata_policy=config.canopy_nodata_policy,
            minimum_canopy_height_m=config.minimum_canopy_height_m,
            observer_clearance_radius_m=config.observer_canopy_clearance_radius_m,
            overwrite=True,
            compress=app.raster.intermediate_compress,
        )
        analysis_dem = canopy_surface_result.surface_path
        validate_raster_grid_alignment(
            analysis_dem,
            endpoint_dem,
            label_a="canopy_obstacle_surface",
            label_b="endpoint_dem",
        )

    dem_nodata_diagnostics = endpoint_dem_nodata_diagnostics
    if Path(analysis_dem) != Path(endpoint_dem):
        dem_nodata_diagnostics = apply_los_dem_nodata_policy(
            analysis_dem,
            policy=config.dem_nodata_policy,
            opaque_barrier_height_m=config.dem_nodata_barrier_height_m,
        )

    surface_metadata: dict[str, Any] = {
        "terrain_surface_model": surface_model,
        "observer_base_surface": "endpoint_dtm",
        "target_base_surface": "water_flattened_endpoint_dem",
        "landcover_in_terrain_los": False,
        "dem_nodata_los_policy": dem_nodata_diagnostics["policy"],
        "dem_nodata_los_pixel_count": dem_nodata_diagnostics["invalid_pixel_count"],
        "endpoint_dem_nodata_los_pixel_count": endpoint_dem_nodata_diagnostics[
            "invalid_pixel_count"
        ],
        "dem_nodata_barrier_height_m": dem_nodata_diagnostics["opaque_barrier_height_m"],
        "observer_endpoint_invalid_count": observer_endpoint_diagnostics["invalid_observer_count"],
        "observer_endpoint_repaired_count": observer_endpoint_diagnostics[
            "repaired_observer_count"
        ],
        "observer_endpoint_repaired_pixel_count": observer_endpoint_diagnostics[
            "repaired_pixel_count"
        ],
        "observer_endpoint_exact_source_repaired_count": observer_endpoint_diagnostics[
            "exact_source_repaired_observer_count"
        ],
        "observer_endpoint_neighborhood_repaired_count": observer_endpoint_diagnostics[
            "neighborhood_repaired_observer_count"
        ],
        "canonical_raster_core_fingerprint": canonical_stack.core_fingerprint,
        "canonical_raster_core_metadata_path": str(canonical_stack.core_metadata_path),
        "canonical_canopy_fingerprint": canonical_stack.canopy_fingerprint,
        "canonical_canopy_metadata_path": (
            str(canonical_stack.canopy_metadata_path)
            if canonical_stack.canopy_metadata_path is not None
            else None
        ),
    }
    if canopy_surface_result is not None:
        land_pixel_count = max(1, canopy_surface_result.land_pixel_count)
        surface_metadata.update(
            {
                "intervening_obstacle_surface": "endpoint_dem_plus_chm",
                "observer_grounded_pixel_count": canopy_surface_result.observer_pixel_count,
                "canopy_land_pixel_count": canopy_surface_result.land_pixel_count,
                "canopy_masked_land_pixel_count": (
                    canopy_surface_result.missing_land_canopy_pixel_count
                ),
                "canopy_masked_land_fraction": (
                    canopy_surface_result.missing_land_canopy_pixel_count / land_pixel_count
                ),
                "positive_canopy_pixel_count": (canopy_surface_result.positive_canopy_pixel_count),
                "maximum_canopy_height_m": canopy_surface_result.maximum_canopy_height_m,
            }
        )

    with rasterio.open(water_mask_path) as mask_src:
        water_mask_arr = mask_src.read(1) == 1
        water_transform = mask_src.transform
        water_crs = mask_src.crs
        raster_width = int(mask_src.width)
        raster_height = int(mask_src.height)
        water_pixel_area_m2 = abs(float(mask_src.transform.a * mask_src.transform.e))

    total_pixels = int(raster_width * raster_height)
    water_pixels = int(np.count_nonzero(water_mask_arr))
    estimated_memory_mb = estimated_batch_memory_mb(total_pixels)
    validate_batch_guardrails(
        batch_id=batch_id,
        total_pixels=total_pixels,
        estimated_memory_mb=estimated_memory_mb,
        max_batch_aoi_pixels=app.batch.max_batch_aoi_pixels,
        max_estimated_batch_memory_mb=app.batch.max_estimated_batch_memory_mb,
    )

    batch_start.__exit__(None, None, None)
    surface_metadata["batch_preparation_elapsed_seconds"] = float(
        batch_timer["elapsed_seconds"] or 0.0
    )
    surface_metadata["batch_total_pixels"] = total_pixels
    surface_metadata["batch_water_pixel_count"] = water_pixels
    LOGGER.info(
        "batch_profile batch_id=%s n_source_cells=%d n_observers=%d "
        "aoi_bounds=%s raster_width=%d raster_height=%d total_pixels=%d "
        "water_pixel_count=%d estimated_memory_mb=%.1f elapsed_seconds=%.2f "
        "process_memory_mb=%s terrain_surface_model=%s "
        "observer_grounded_pixel_count=%s positive_canopy_pixel_count=%s",
        batch_id,
        len(source_cells),
        len(all_observers),
        tuple(float(v) for v in aoi_projected.total_bounds),
        raster_width,
        raster_height,
        total_pixels,
        water_pixels,
        estimated_memory_mb,
        float(batch_timer["elapsed_seconds"] or 0.0),
        current_process_memory_mb(),
        surface_model,
        (canopy_surface_result.observer_pixel_count if canopy_surface_result is not None else None),
        (
            canopy_surface_result.positive_canopy_pixel_count
            if canopy_surface_result is not None
            else None
        ),
    )

    return BatchContext(
        batch_id=batch_id,
        batch_index=batch_index,
        source_cells=list(source_cells),
        all_sample_points=all_sample_points,
        aoi_wgs84=aoi_wgs84,
        aoi_projected=aoi_projected,
        water_mask_path=water_mask_path,
        analysis_dem_path=analysis_dem,
        endpoint_dem_path=endpoint_dem,
        water_mask_arr=water_mask_arr,
        water_transform=water_transform,
        source_cell_metadata=source_metadata,
        raw_dem_path=raw_dem,
        dem_projected_path=dem_projected,
        dem_clip_path=dem_clip,
        water_crs=water_crs,
        water_shape=(raster_height, raster_width),
        water_pixel_area_m2=water_pixel_area_m2,
        canonical_water_mask_path=canonical_stack.water_mask_path,
        aligned_canopy_height_path=aligned_canopy_height_path,
        surface_metadata=surface_metadata,
    )
