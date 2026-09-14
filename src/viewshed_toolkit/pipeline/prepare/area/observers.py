"""Observer-frame construction for viewshed source samples."""

from __future__ import annotations

import geopandas as gpd

from ...config import ViewshedConfig

CRS_WGS84 = "EPSG:4326"


def build_observers_from_sample_points(
    sample_points: gpd.GeoDataFrame,
    config: ViewshedConfig,
) -> gpd.GeoDataFrame:
    observers = sample_points.rename(columns={"sample_id": "observer_id"}).copy()
    observers["site_name"] = observers.apply(
        lambda row: f"{row['source_h3_cell']} sample {int(row['sample_index'])}",
        axis=1,
    )
    observers["observer_height_m"] = config.observer_eye_height_m
    observers["target_height_m"] = config.target_height_m
    observers["max_distance_m"] = config.max_distance_m
    columns = [
        "site_name",
        "observer_id",
        "source_h3_cell",
        "sample_index",
        "lon",
        "lat",
        "observer_height_m",
        "target_height_m",
        "max_distance_m",
    ]
    columns.extend(
        column
        for column in [
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
        if column in observers.columns
    )
    columns.append("geometry")
    return gpd.GeoDataFrame(
        observers[columns],
        geometry="geometry",
        crs=CRS_WGS84,
    )
