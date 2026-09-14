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

import logging
from pathlib import Path
from typing import Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import polars as pl

from ...config.distance import DistanceRuntime, DistanceWeightConfig
from ...contracts.pairs import ALLOWED_CELL_TYPES
from . import domains
from .geometry import (
    ensure_h3_geometry_artifact,
    load_h3_geometry_lookup,
)
from .land import build_land_cells_for_config

LOGGER = logging.getLogger(__name__)

_ALLOWED_CELL_TYPES = ALLOWED_CELL_TYPES


from .config import SourceTargetLookupConfig, SourceUniverse


def _physical_cell_type_frame(
    classification: gpd.GeoDataFrame,
    lookup_cfg: SourceTargetLookupConfig,
) -> pd.DataFrame:
    """Convert cell fractions to physical cell types.

    This is deliberately role-neutral. Modeling roles are assigned later via
    source_type and target universe filters.
    """

    required = {"h3_cell", "land_fraction", "water_fraction"}
    missing = required - set(classification.columns)
    if missing:
        raise ValueError(
            "Cell classification is missing required columns: " + ", ".join(sorted(missing))
        )

    out = classification[["h3_cell", "land_fraction", "water_fraction"]].copy()
    out["h3_cell"] = out["h3_cell"].astype(str)
    out["land_fraction"] = out["land_fraction"].astype(float).clip(0.0, 1.0)
    out["water_fraction"] = out["water_fraction"].astype(float).clip(0.0, 1.0)

    has_land = out["land_fraction"].to_numpy() >= float(lookup_cfg.min_land_fraction_for_source)
    has_water = out["water_fraction"].to_numpy() >= float(lookup_cfg.min_water_fraction_for_target)

    cell_type = np.full(len(out), "excluded", dtype=object)
    cell_type[has_land & ~has_water] = "land"
    cell_type[has_water & ~has_land] = "water"
    cell_type[has_land & has_water] = "mixed"
    out["cell_type"] = cell_type

    bad = set(out["cell_type"].dropna().unique()) - _ALLOWED_CELL_TYPES
    if bad:
        raise ValueError(f"Unexpected physical cell_type values: {sorted(bad)}")
    return out


def _read_land_source_cells(path: Path) -> list[str]:
    scan = pl.scan_parquet(str(path))
    columns = set(scan.collect_schema().names())
    for col in ("h3_cell", "source_h3", "h3", "cell"):
        if col in columns:
            return (
                scan.select(pl.col(col).cast(pl.Utf8).drop_nulls().unique().alias(col))
                .collect(engine="streaming")
                .get_column(col)
                .sort()
                .to_list()
            )
    raise ValueError(
        f"Could not find H3 cell column in land source file: {path}. "
        "Expected one of h3_cell, source_h3, h3, cell."
    )


def _classify_bbox_cells(
    runtime: DistanceRuntime,
    lookup_cfg: SourceTargetLookupConfig,
    *,
    cells: Sequence[str] | None = None,
    extent: str = "source",
) -> gpd.GeoDataFrame:
    """Classify bbox H3 cells by land/water fraction.

    This replaces the older ``domains.classify_bbox_h3_cells`` role-oriented
    behavior so this builder does not depend on ``mixed_cell_policy``.
    """

    domain_geoms = domains.load_land_water_domains(runtime, extent=extent)
    clip_polygon = domain_geoms.bbox_polygon
    cells = (
        list(cells)
        if cells is not None
        else domains.bbox_h3_cells(
            tuple(float(v) for v in clip_polygon.bounds),
            runtime.source_resolution,
            buffer_rings=lookup_cfg.bbox_buffer_rings,
            strict_intersection=False,
        )
    )
    projected_crs = lookup_cfg.projected_crs or runtime.projected_crs

    geometry_path = ensure_h3_geometry_artifact(runtime, cells, projected_crs=projected_crs)
    geometries_wgs84 = load_h3_geometry_lookup(geometry_path, "geometry_wgs84")
    geometries_projected = load_h3_geometry_lookup(geometry_path, "geometry_projected")
    water_geometries_projected = load_h3_geometry_lookup(geometry_path, "water_geometry_projected")
    cell_ids = [str(c) for c in cells]
    gdf = gpd.GeoDataFrame(
        {"h3_cell": cell_ids},
        geometry=[geometries_wgs84[cell] for cell in cell_ids],
        crs=domains.CRS_WGS84,
    )
    gdf = gdf[gdf.geometry.intersects(clip_polygon)].copy()
    if gdf.empty:
        raise ValueError("No H3 cells intersect the configured bbox after classification.")

    active_cells = gdf["h3_cell"].astype(str).tolist()
    cell_area = np.asarray(
        [geometries_projected[cell].area for cell in active_cells], dtype="float64"
    )
    land_geom = gdf.geometry.intersection(domain_geoms.land_domain)

    land_area = (
        gpd.GeoSeries(land_geom, crs=domains.CRS_WGS84)
        .to_crs(projected_crs)
        .area.to_numpy(dtype="float64")
    )
    water_area = np.asarray(
        [water_geometries_projected[cell].area for cell in active_cells], dtype="float64"
    )

    with np.errstate(divide="ignore", invalid="ignore"):
        land_fraction = np.where(cell_area > 0, land_area / cell_area, 0.0)
        water_fraction = np.where(cell_area > 0, water_area / cell_area, 0.0)

    gdf["cell_area_m2"] = cell_area.astype("float64")
    gdf["land_area_m2"] = land_area.astype("float64")
    gdf["water_area_m2"] = water_area.astype("float64")
    gdf["land_fraction"] = np.clip(land_fraction, 0.0, 1.0)
    gdf["water_fraction"] = np.clip(water_fraction, 0.0, 1.0)
    return gdf


def _build_source_universe(
    config_path: str | Path,
    *,
    runtime: DistanceRuntime,
    cfg: DistanceWeightConfig,
    lookup_cfg: SourceTargetLookupConfig,
    overwrite: bool,
) -> SourceUniverse:
    resolution = int(lookup_cfg.h3_resolution or runtime.source_resolution)

    source_cells = domains.bbox_h3_cells(
        runtime.bbox_wgs84,
        resolution,
        buffer_rings=lookup_cfg.bbox_buffer_rings,
        strict_intersection=lookup_cfg.strict_bbox_intersection,
    )
    source_classification = _classify_bbox_cells(
        runtime,
        lookup_cfg,
        cells=source_cells,
        extent="source",
    )
    source_type_df = _physical_cell_type_frame(source_classification, lookup_cfg)

    target_polygon = domains.target_domain_polygon(runtime)
    target_candidates = domains.bbox_h3_cells(
        tuple(float(v) for v in target_polygon.bounds),
        resolution,
        buffer_rings=lookup_cfg.bbox_buffer_rings,
        strict_intersection=False,
    )
    target_classification = _classify_bbox_cells(
        runtime,
        lookup_cfg,
        cells=target_candidates,
        extent="target",
    )
    target_type_df = _physical_cell_type_frame(target_classification, lookup_cfg)

    type_by_cell = dict(zip(source_type_df["h3_cell"], source_type_df["cell_type"]))

    target_types: set[str] = set()
    if lookup_cfg.include_water_targets:
        target_types.add("water")
    if lookup_cfg.include_mixed_as_water_targets:
        target_types.add("mixed")
    target_cells = set(
        target_type_df.loc[target_type_df["cell_type"].isin(target_types), "h3_cell"].astype(str)
    )

    land_source_cells: list[str] = []
    if lookup_cfg.include_land_sources:
        land_result = build_land_cells_for_config(
            config_path,
            overwrite=overwrite,
            h3_resolution=resolution,
        )
        prepared_land_cells = _read_land_source_cells(land_result.land_h3_path)
        allowed_land_types = {"land"}
        if lookup_cfg.include_mixed_as_land_sources:
            allowed_land_types.add("mixed")
        land_source_cells = sorted(
            cell
            for cell in prepared_land_cells
            if type_by_cell.get(str(cell)) in allowed_land_types
        )

    water_source_cells: list[str] = []
    if lookup_cfg.include_water_sources:
        water_types = {"water"}
        if lookup_cfg.include_mixed_as_water_sources:
            water_types.add("mixed")
        water_source_cells = sorted(
            source_type_df.loc[source_type_df["cell_type"].isin(water_types), "h3_cell"].astype(str)
        )

    if not land_source_cells and not water_source_cells:
        raise ValueError(
            "No source cells available after source universe filters. "
            f"include_land_sources={lookup_cfg.include_land_sources} "
            f"include_water_sources={lookup_cfg.include_water_sources}"
        )
    if not target_cells:
        raise ValueError(
            "No target cells available after target universe filters. "
            "Targets must be water or mixed cells."
        )

    LOGGER.info(
        "Built source universe cells=%d land_sources=%d water_sources=%d targets=%d "
        "cell_type_counts=%s",
        len(source_type_df),
        len(land_source_cells),
        len(water_source_cells),
        len(target_cells),
        source_type_df["cell_type"].value_counts(dropna=False).to_dict(),
    )

    cell_attributes = (
        pd.concat([source_type_df, target_type_df], ignore_index=True)
        .drop_duplicates(subset=["h3_cell"], keep="first")
        .reset_index(drop=True)
    )

    return SourceUniverse(
        land_source_cells=land_source_cells,
        water_source_cells=water_source_cells,
        target_cells=target_cells,
        cell_attributes=cell_attributes,
    )


# -----------------------------------------------------------------------------
# Pair construction
# -----------------------------------------------------------------------------
