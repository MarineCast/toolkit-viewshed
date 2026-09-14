"""Source-cell observer sampling and sampling diagnostics."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import CRS
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from viewshed_toolkit._internal.geo.geometry import polygons_from_any, safe_make_valid
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon, latlng_to_cell

from ...config import (
    SOURCE_SAMPLING_ALGORITHM_VERSION,
    SOURCE_SAMPLING_CANDIDATE_GRID_SIDE,
    AppConfig,
)

CRS_WGS84 = "EPSG:4326"
LOGGER = logging.getLogger(__name__)


def h3_cell_for_latlon(lat: float, lon: float, h3_resolution: int) -> str:
    return latlng_to_cell(float(lat), float(lon), int(h3_resolution))


def calculate_source_sample_count(
    *,
    source_type: str,
    land_fraction: float | None,
    water_fraction: float | None,
    sampling_mode: str,
    max_samples: int,
    min_samples: int,
) -> tuple[float, int]:
    """Return the active source fraction and deterministic requested count."""

    normalized_source_type = str(source_type).strip().lower()
    if normalized_source_type not in {"land", "water"}:
        raise ValueError("source_type must be one of: land, water")
    mode = str(sampling_mode).strip().lower()
    if mode not in {"fixed", "active_fraction"}:
        raise ValueError("sampling_mode must be one of: fixed, active_fraction")
    max_count = int(max_samples)
    min_count = int(min_samples)
    if max_count < 1:
        raise ValueError("max_samples must be >= 1")
    if min_count < 1:
        raise ValueError("min_samples must be >= 1")
    if min_count > max_count:
        raise ValueError("min_samples must be <= max_samples")

    raw_fraction = land_fraction if normalized_source_type == "land" else water_fraction
    if raw_fraction is None or not np.isfinite(float(raw_fraction)):
        if mode == "active_fraction":
            fraction_name = (
                "land_fraction" if normalized_source_type == "land" else "water_fraction"
            )
            raise ValueError(f"Adaptive source sampling requires a finite {fraction_name}.")
        active_fraction = 1.0
    else:
        active_fraction = float(np.clip(float(raw_fraction), 0.0, 1.0))

    if mode == "fixed":
        return active_fraction, max_count

    raw_count = float(max_count) * active_fraction
    requested = math.floor(raw_count + 0.5)
    requested = max(min_count, requested)
    requested = min(requested, max_count)
    return active_fraction, int(requested)


def _point_coordinate_key(point: Point) -> tuple[float, float]:
    return (round(float(point.x), 15), round(float(point.y), 15))


def _sample_points_frame(
    source_cell: str,
    selected: Sequence[Point],
    requested_count: int,
) -> gpd.GeoDataFrame:
    unique: list[Point] = []
    seen: set[tuple[float, float]] = set()
    for point in selected:
        key = _point_coordinate_key(point)
        if key in seen:
            continue
        seen.add(key)
        unique.append(point)
    actual_count = len(unique)
    return gpd.GeoDataFrame(
        {
            "sample_id": [f"{source_cell}_sample_{idx + 1:03d}" for idx in range(actual_count)],
            "source_h3_cell": [source_cell] * actual_count,
            "sample_points_requested": [int(requested_count)] * actual_count,
            "sample_points_actual": [int(actual_count)] * actual_count,
            "sample_index": list(range(1, actual_count + 1)),
            "lat": [point.y for point in unique],
            "lon": [point.x for point in unique],
        },
        geometry=unique,
        crs=CRS_WGS84,
    )


def sample_points_in_h3_cell_id(
    source_cell: str,
    n_points: int,
    include_centroid: bool = True,
    *,
    projected_crs: str | None = None,
    max_design_points: int | None = None,
) -> gpd.GeoDataFrame:
    return sample_points_in_source_geometry(
        source_cell,
        cell_to_polygon(source_cell),
        n_points,
        include_centroid=include_centroid,
        geometry_crs=CRS_WGS84,
        projected_crs=projected_crs,
        max_design_points=max_design_points,
    )


def sample_points_in_source_geometry(
    source_cell: str,
    geometry: BaseGeometry,
    n_points: int,
    include_centroid: bool = True,
    *,
    geometry_crs: str | CRS = CRS_WGS84,
    projected_crs: str | CRS | None = None,
    max_design_points: int | None = None,
) -> gpd.GeoDataFrame:
    """Return a prefix of one metric, projected maximin source design.

    Candidate generation and the ordered maximin sequence are constructed for
    ``max_design_points``, independently of the requested ``n_points``. Calls
    using the same geometry, CRS, and design maximum are therefore nested: an
    n-point result is the first n points of every larger result. The selected
    observer points are returned in WGS84 for the downstream terrain interface.
    When no projected CRS is supplied, a local UTM CRS is estimated from the
    source geometry.
    """

    if n_points <= 0:
        raise ValueError("n_points must be positive.")
    design_count = n_points if max_design_points is None else int(max_design_points)
    if design_count < n_points:
        raise ValueError("max_design_points must be greater than or equal to n_points.")
    if geometry is None or geometry.is_empty:
        raise ValueError(f"Source geometry is empty for source cell: {source_cell}")

    source_crs = CRS.from_user_input(geometry_crs)
    source_geometry = gpd.GeoSeries([geometry], crs=source_crs)
    if projected_crs is None:
        sampling_crs = source_crs if source_crs.is_projected else source_geometry.estimate_utm_crs()
        if sampling_crs is None:
            raise ValueError(
                f"Could not estimate a projected sampling CRS for source cell: {source_cell}"
            )
    else:
        sampling_crs = CRS.from_user_input(projected_crs)
    sampling_crs = CRS.from_user_input(sampling_crs)
    if not sampling_crs.is_projected:
        raise ValueError("projected_crs must be a projected coordinate reference system.")

    polygon = source_geometry.to_crs(sampling_crs).iloc[0]
    minx, miny, maxx, maxy = polygon.bounds
    central_point = polygon.representative_point()
    selected: list[Point] = []
    if include_centroid and polygon.covers(central_point):
        selected.append(central_point)

    polygonal = polygons_from_any(safe_make_valid(polygon))
    if polygonal is None:
        components: list[BaseGeometry] = []
    elif polygonal.geom_type == "Polygon":
        components = [polygonal]
    else:
        components = list(polygonal.geoms)
    components = sorted(
        components,
        key=lambda component: (
            -float(component.area),
            round(float(component.centroid.x), 6),
            round(float(component.centroid.y), 6),
        ),
    )

    # Seed every disconnected polygon component before filling the design with
    # the global maximin sequence. The seed order is itself maximin and is
    # independent of the requested prefix length.
    component_seeds = [component.representative_point() for component in components]
    selected_keys = {_point_coordinate_key(point) for point in selected}
    component_seeds = [
        point for point in component_seeds if _point_coordinate_key(point) not in selected_keys
    ]
    while len(selected) < design_count and component_seeds:
        if not selected:
            next_idx = 0
        else:
            seed_distances = np.round(
                [min(point.distance(chosen) for chosen in selected) for point in component_seeds],
                6,
            )
            next_idx = int(np.argmax(seed_distances))
        selected.append(component_seeds.pop(next_idx))

    candidates_by_coordinate: dict[tuple[float, float], Point] = {}
    grid_side = SOURCE_SAMPLING_CANDIDATE_GRID_SIDE
    if components:
        total_component_area = sum(float(component.area) for component in components)
        candidate_domains = [
            (
                component,
                max(
                    4,
                    int(round(grid_side * math.sqrt(float(component.area) / total_component_area))),
                ),
            )
            for component in components
        ]
    else:
        candidate_domains = [(polygon, grid_side)]
    for component, component_grid_side in candidate_domains:
        component_minx, component_miny, component_maxx, component_maxy = component.bounds
        xs = np.linspace(component_minx, component_maxx, component_grid_side + 2)[1:-1]
        ys = np.linspace(component_miny, component_maxy, component_grid_side + 2)[1:-1]
        for y in ys:
            for x in xs:
                point = Point(float(x), float(y))
                if component.covers(point):
                    candidates_by_coordinate.setdefault(_point_coordinate_key(point), point)

    selected_keys = {_point_coordinate_key(point) for point in selected}
    remaining = [
        point for key, point in candidates_by_coordinate.items() if key not in selected_keys
    ]
    while len(selected) < design_count and remaining:
        if not selected:
            central_distances = np.round([point.distance(central_point) for point in remaining], 6)
            next_idx = int(np.argmin(central_distances))
        else:
            maximin_distances = np.round(
                [min(point.distance(chosen) for chosen in selected) for point in remaining],
                6,
            )
            next_idx = int(np.argmax(maximin_distances))
        selected.append(remaining.pop(next_idx))

    selected = selected[:n_points]
    if selected:
        selected_wgs84 = gpd.GeoSeries(selected, crs=sampling_crs).to_crs(CRS_WGS84)
        output_points = list(selected_wgs84)
    else:
        output_points = []
    return _sample_points_frame(source_cell, output_points, n_points)


def prepare_source_samples(
    app: AppConfig,
    source_cells_gdf: gpd.GeoDataFrame,
    source_cells: Sequence[str] | None = None,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Generate unique adaptive source samples and source-level diagnostics."""

    if "h3_cell" not in source_cells_gdf.columns:
        raise ValueError("Source cells must include h3_cell.")
    selected_cells = (
        [str(cell) for cell in source_cells]
        if source_cells is not None
        else [str(cell) for cell in source_cells_gdf["h3_cell"].tolist()]
    )
    source_metadata = source_cells_gdf[
        source_cells_gdf["h3_cell"].astype(str).isin(set(selected_cells))
    ].copy()
    metadata_by_cell = {str(row["h3_cell"]): row for row in source_metadata.to_dict("records")}
    source_type = str(getattr(app, "source_type", "land") or "land").strip().lower()
    sampling_mode = str(app.h3.source_sampling_mode).strip().lower()
    max_samples = int(app.h3.sample_points_per_source_cell)
    min_samples = int(app.h3.min_sample_points_per_source_cell)
    source_crs = source_cells_gdf.crs
    if source_crs is None:
        raise ValueError("Source cells must define a CRS for metric observer sampling.")
    sampling_projected_crs = str(app.viewshed.crs_projected)

    sample_frames: list[gpd.GeoDataFrame] = []
    diagnostic_rows: list[dict[str, Any]] = []
    for cell in selected_cells:
        row = metadata_by_cell.get(cell)
        land_fraction = None if row is None else row.get("land_fraction")
        water_fraction = None if row is None else row.get("water_fraction")
        active_fraction, requested_count = calculate_source_sample_count(
            source_type=source_type,
            land_fraction=land_fraction,
            water_fraction=water_fraction,
            sampling_mode=sampling_mode,
            max_samples=max_samples,
            min_samples=min_samples,
        )

        sample_geometry: BaseGeometry | None = None
        if row is not None:
            geometry_key = "land_geometry" if source_type == "land" else "water_geometry"
            candidate_geometry = row.get(geometry_key)
            if isinstance(candidate_geometry, BaseGeometry) and not candidate_geometry.is_empty:
                sample_geometry = candidate_geometry
            elif isinstance(row.get("geometry"), BaseGeometry) and not row["geometry"].is_empty:
                sample_geometry = row["geometry"]
        if sample_geometry is None or sample_geometry.is_empty:
            if sampling_mode == "active_fraction":
                raise ValueError(
                    f"Adaptive source sampling requires valid {source_type} geometry for {cell}."
                )
            points = sample_points_in_h3_cell_id(
                cell,
                requested_count,
                include_centroid=app.h3.include_centroid,
                projected_crs=sampling_projected_crs,
                max_design_points=max_samples,
            )
            sample_geometry = cell_to_polygon(cell)
            sample_geometry_crs: str | CRS = CRS_WGS84
        else:
            points = sample_points_in_source_geometry(
                cell,
                sample_geometry,
                requested_count,
                include_centroid=app.h3.include_centroid,
                geometry_crs=source_crs,
                projected_crs=sampling_projected_crs,
                max_design_points=max_samples,
            )
            sample_geometry_crs = source_crs

        unique_count = int(points[["lon", "lat"]].drop_duplicates().shape[0])
        actual_count = int(len(points))
        duplicate_count = actual_count - unique_count
        if actual_count < 1:
            raise ValueError(f"No valid unique observer points generated for source cell: {cell}")
        if duplicate_count:
            raise ValueError(
                f"Duplicate observer coordinates generated for {cell}: {duplicate_count}"
            )
        projected_validation_geometry = (
            gpd.GeoSeries([sample_geometry], crs=sample_geometry_crs)
            .to_crs(sampling_projected_crs)
            .iloc[0]
        )
        repaired_validation_geometry = safe_make_valid(projected_validation_geometry)
        polygonal_validation_geometry = polygons_from_any(repaired_validation_geometry)
        validation_geometry = (
            polygonal_validation_geometry
            if polygonal_validation_geometry is not None
            else repaired_validation_geometry
        )
        if validation_geometry is None or validation_geometry.is_empty:
            raise ValueError(
                f"Projected {source_type} sampling geometry is empty after repair: {cell}"
            )
        validation_points = points.to_crs(sampling_projected_crs).geometry
        if not all(
            validation_geometry.covers(point) or point.distance(validation_geometry) <= 1e-6
            for point in validation_points
        ):
            raise ValueError(f"Observer sample fell outside valid {source_type} geometry: {cell}")

        sampling_warning = ""
        if actual_count < requested_count:
            warning_payload = {
                "event": "source_sampling_shortfall",
                "source_h3": cell,
                "source_type": source_type,
                "active_source_fraction": active_fraction,
                "sample_points_requested": requested_count,
                "sample_points_actual": actual_count,
            }
            sampling_warning = json.dumps(warning_payload, sort_keys=True)
            LOGGER.warning("source_sampling_shortfall %s", sampling_warning)

        points["source_type"] = source_type
        points["active_source_fraction"] = float(active_fraction)
        points["source_sampling_mode"] = sampling_mode
        points["source_sampling_projected_crs"] = sampling_projected_crs
        points["source_sampling_candidate_grid_side"] = SOURCE_SAMPLING_CANDIDATE_GRID_SIDE
        points["source_sampling_max_design_points"] = max_samples
        projected_sample_geometry = (
            gpd.GeoSeries([sample_geometry], crs=sample_geometry_crs)
            .to_crs(sampling_projected_crs)
            .iloc[0]
        )
        polygonal_sample_geometry = polygons_from_any(safe_make_valid(projected_sample_geometry))
        source_sampling_component_count = (
            0
            if polygonal_sample_geometry is None
            else (
                1
                if polygonal_sample_geometry.geom_type == "Polygon"
                else len(polygonal_sample_geometry.geoms)
            )
        )
        points["source_sampling_component_count"] = source_sampling_component_count
        points["sample_points_max"] = max_samples
        points["sample_points_min"] = min_samples
        points["sample_points_requested"] = requested_count
        points["sample_points_actual"] = actual_count
        sample_frames.append(points)
        diagnostic_rows.append(
            {
                "source_h3": cell,
                "source_type": source_type,
                "land_fraction": (np.nan if land_fraction is None else float(land_fraction)),
                "water_fraction": (np.nan if water_fraction is None else float(water_fraction)),
                "active_source_fraction": float(active_fraction),
                "source_sampling_mode": sampling_mode,
                "source_sampling_projected_crs": sampling_projected_crs,
                "source_sampling_candidate_grid_side": (SOURCE_SAMPLING_CANDIDATE_GRID_SIDE),
                "source_sampling_max_design_points": max_samples,
                "source_sampling_component_count": source_sampling_component_count,
                "sample_points_max": max_samples,
                "sample_points_min": min_samples,
                "sample_points_requested": requested_count,
                "sample_points_actual": actual_count,
                "unique_sample_points": unique_count,
                "duplicate_sample_coordinate_count": duplicate_count,
                "sampling_warning": sampling_warning,
            }
        )

    if not sample_frames:
        raise ValueError("No source sample points were generated.")
    all_samples = gpd.GeoDataFrame(
        pd.concat(sample_frames, ignore_index=True), geometry="geometry", crs=CRS_WGS84
    )
    diagnostics = pd.DataFrame(diagnostic_rows).sort_values("source_h3").reset_index(drop=True)
    return all_samples, diagnostics


def summarize_source_sampling_diagnostics(diagnostics: pd.DataFrame) -> dict[str, Any]:
    """Return required run/batch source-sampling summary statistics."""

    requested = pd.to_numeric(diagnostics["sample_points_requested"], errors="raise")
    actual = pd.to_numeric(diagnostics["sample_points_actual"], errors="raise")
    projected_crs_values = diagnostics["source_sampling_projected_crs"].dropna().unique()
    if len(projected_crs_values) != 1:
        raise ValueError("Source sampling diagnostics must contain exactly one projected CRS.")
    max_design_values = pd.to_numeric(
        diagnostics["source_sampling_max_design_points"], errors="raise"
    ).unique()
    if len(max_design_values) != 1:
        raise ValueError(
            "Source sampling diagnostics must contain exactly one maximum design size."
        )
    candidate_grid_values = pd.to_numeric(
        diagnostics["source_sampling_candidate_grid_side"], errors="raise"
    ).unique()
    if len(candidate_grid_values) != 1:
        raise ValueError(
            "Source sampling diagnostics must contain exactly one candidate grid size."
        )
    component_counts = (
        pd.to_numeric(diagnostics["source_sampling_component_count"], errors="raise")
        if "source_sampling_component_count" in diagnostics
        else pd.Series([0], dtype="int64")
    )
    return {
        "source_sampling_projected_crs": str(projected_crs_values[0]),
        "source_sampling_candidate_grid_side": int(candidate_grid_values[0]),
        "source_sampling_max_design_points": int(max_design_values[0]),
        "source_sampling_component_count_max": int(component_counts.max()),
        "source_cell_count": int(len(diagnostics)),
        "sample_points_requested_total": int(requested.sum()),
        "sample_points_actual_total": int(actual.sum()),
        "sample_points_requested_min": int(requested.min()),
        "sample_points_requested_median": float(requested.median()),
        "sample_points_requested_mean": float(requested.mean()),
        "sample_points_requested_max": int(requested.max()),
        "sample_points_actual_min": int(actual.min()),
        "sample_points_actual_median": float(actual.median()),
        "sample_points_actual_mean": float(actual.mean()),
        "sample_points_actual_max": int(actual.max()),
        "sources_with_fewer_samples_than_requested": int((actual < requested).sum()),
        "duplicate_sample_coordinate_count": int(
            diagnostics["duplicate_sample_coordinate_count"].sum()
        ),
    }


def source_sampling_diagnostics_path(app: AppConfig) -> Path:
    return app.paths.partitioned_visibility_dir.parent / "source_sampling_diagnostics.parquet"


def write_source_sampling_diagnostics(
    app: AppConfig, diagnostics: pd.DataFrame
) -> tuple[Path, dict[str, Any]]:
    path = source_sampling_diagnostics_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.to_parquet(path, index=False)
    summary = summarize_source_sampling_diagnostics(diagnostics)
    metadata = {
        "source_sampling_algorithm_version": SOURCE_SAMPLING_ALGORITHM_VERSION,
        "source_sampling_mode": str(app.h3.source_sampling_mode),
        "max_sample_points_per_source_cell": int(app.h3.sample_points_per_source_cell),
        "min_sample_points_per_source_cell": int(app.h3.min_sample_points_per_source_cell),
        **summary,
    }
    path.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path, metadata


def source_cell_polygon(cell: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"h3_cell": [cell]}, geometry=[cell_to_polygon(cell)], crs=CRS_WGS84)
