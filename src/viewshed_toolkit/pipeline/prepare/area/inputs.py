"""Viewshed preparation input resolution and validation."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from viewshed_toolkit._internal.geo.raster import valid_raster

from ...config import AppConfig
from ..elevation.canopy import normalize_surface_model


def _require_existing_file(path: Path, label: str, hint: str | None = None) -> Path:
    if path.exists():
        return path

    message = f"Missing required {label}: {path}"
    if hint:
        message += f"\n{hint}"
    raise FileNotFoundError(message)


def source_cells_input_path(app: AppConfig) -> Path:
    """Return the configured source-cell input path without building anything."""
    if app.paths.land_h3_path.exists():
        return app.paths.land_h3_path
    if app.paths.source_cells_path.exists():
        return app.paths.source_cells_path

    raise FileNotFoundError(
        "Missing configured source/land H3 cells. Checked:\n"
        f"  land_h3_path: {app.paths.land_h3_path}\n"
        f"  source_cells_path: {app.paths.source_cells_path}\n"
        "Run the land/source-cell build step first, then rerun this viewshed command."
    )


def load_source_cells(app: AppConfig) -> gpd.GeoDataFrame:
    """Load prebuilt source cells from config. No fallbacks, no builders."""
    path = source_cells_input_path(app)
    source = gpd.read_parquet(path)

    if "h3_cell" not in source.columns:
        raise ValueError(f"Source-cell file is missing required h3_cell column: {path}")
    if source.empty:
        raise ValueError(f"Source-cell file is empty: {path}")

    return source


def validate_viewshed_inputs(app: AppConfig) -> None:
    """Validate upstream products required by this viewshed runner."""
    _require_existing_file(
        app.paths.water_polygon_path,
        "water polygon parquet",
        "Expected this to be produced by the water/land preprocessing step.",
    )
    if app.source_type == "land":
        _require_existing_file(
            app.paths.regional_dem_path,
            "regional DEM raster",
            "Run the DEM download/build step first. This viewshed module will not download DEMs.",
        )
        if not valid_raster(app.paths.regional_dem_path):
            raise ValueError(
                f"Configured regional DEM is not a readable raster: {app.paths.regional_dem_path}"
            )

    if normalize_surface_model(app.viewshed.surface_model) == "canopy":
        _require_existing_file(
            app.paths.canopy_height_path,
            "canopy-height raster",
            "Prepare the CHM first or set paths.canopy_height_path to a readable CHM.",
        )
        if not valid_raster(app.paths.canopy_height_path):
            raise ValueError(
                "Configured canopy-height raster is not readable: "
                f"{app.paths.canopy_height_path}"
            )

    source_cells_input_path(app)
