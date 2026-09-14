"""End-to-end viewshed visualization exports and manifest generation."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import geopandas as gpd
import h3
import pandas as pd
import polars as pl

from viewshed_toolkit._internal.artifacts import checksum_path

from ..config import load_app_config
from ..config.loader import AppConfig
from ..contracts.artifacts import final_artifact_paths
from ..finalize.final_artifacts import (
    materialize_static_viewability_output,
    static_scientific_config_hash,
)
from .data import (
    FACTOR_MAP_SPECS,
    FACTOR_SPECS,
    EdgeTable,
    StaticMapOutputPaths,
    ViewshedMapConfig,
    _aggregate_edges,
    _build_map_context,
    _MapContext,
    _selected_source_edges,
    _SmoothingGrid,
    _source_cells,
    _static_edges,
    aggregate_target_weight_sums,
    select_source_for_viewshed_map,
    static_map_output_paths,
    static_map_settings,
)
from .interactive_maps import (
    _MapGeometryAssets,
    _shared_map_geometry_assets,
    _write_aggregate_map,
    _write_selected_map,
    write_selected_h3_viewshed_map,
    write_selected_source_generalized_map,
    write_selected_source_smoothed_map,
    write_smoothed_weight_map,
    write_target_h3_weight_map,
)
from .static_maps import (
    _build_smoothing_grid,
    _render_smoothed_metric,
    _write_generalized_visibility_class_artifacts,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ViewshedMapExportResult:
    """Paths and selections produced by :func:`export_viewshed_weight_maps`."""

    selected_source_h3: str
    value_table: Path
    selected_h3_map: Path
    manifest: Path
    maps: dict[str, dict[str, Path]]
    selected_source_maps: dict[str, Path]


@dataclass(frozen=True)
class StaticMapExportResult:
    selected_source_h3: str
    selected_html: Path
    land_aggregate_html: Path
    water_aggregate_html: Path
    manifest: Path
    selected_values: Path
    land_target_aggregate_values: Path
    land_source_aggregate_values: Path
    water_target_aggregate_values: Path
    water_source_aggregate_values: Path


@dataclass(frozen=True)
class SourceTypeStaticMapExportResult:
    """Artifacts produced by a land-only or water-only aggregate export."""

    source_type: str
    aggregate_html: Path
    manifest: Path
    target_aggregate_values: Path
    source_aggregate_values: Path


def export_selected_source_smoothed_viewshed(
    edges: pd.DataFrame,
    context: _MapContext,
    grid: _SmoothingGrid,
    *,
    source_h3: str,
    output_dir: Path,
    config: ViewshedMapConfig,
    assets: _MapGeometryAssets | None = None,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Export continuous and generalized smoothed viewsheds for one source."""

    metric = str(config.selected_source_metric)
    if metric not in edges.columns:
        raise KeyError(f"Selected-source smoothing metric is missing: {metric}")
    selected = edges.loc[edges["source_h3"].astype(str).eq(str(source_h3))].copy()
    if selected.empty:
        raise ValueError(f"No candidate pairs exist for selected source {source_h3}.")
    selected = selected[["target_h3", metric]].copy()
    caption = "Selected-source smoothed final visibility"
    smooth_dir = Path(output_dir) / "smoothed"
    stem = f"selected_h3_{source_h3}_smoothed_viewshed"
    continuous_png = smooth_dir / f"{stem}.png"
    continuous_tif = smooth_dir / f"{stem}.tif"
    continuous_html = Path(output_dir) / f"{stem}.html"
    selected_config = replace(config, support_boundary_smoothing_km=0.0)
    render = _render_smoothed_metric(
        selected,
        grid,
        metric=metric,
        caption=caption,
        png_path=continuous_png,
        tif_path=continuous_tif,
        config=selected_config,
        target_aggregation="single_selected_source",
        pair_combination="distance_weighted_terrain_times_conditional_canopy",
        source_h3=source_h3,
    )
    write_selected_source_smoothed_map(
        edges,
        context,
        grid,
        source_h3=source_h3,
        visibility_metric=metric,
        png_path=continuous_png,
        output_path=continuous_html,
        caption=caption,
        vmax=render.vmax,
        support_geometry_wgs84=render.support_geometry_wgs84,
        config=selected_config,
        assets=assets,
    )

    class_stem = (
        f"selected_h3_{source_h3}_generalized_" f"{config.visibility_class_count}class_viewshed"
    )
    class_png = smooth_dir / f"{class_stem}.png"
    class_tif = smooth_dir / f"{class_stem}.tif"
    class_json = smooth_dir / f"{class_stem}.json"
    class_html = Path(output_dir) / f"{class_stem}.html"
    class_metadata = _write_generalized_visibility_class_artifacts(
        continuous_tif,
        class_tif_path=class_tif,
        class_png_path=class_png,
        metadata_path=class_json,
        source_h3=source_h3,
        visibility_metric=metric,
        config=config,
    )
    write_selected_source_generalized_map(
        edges,
        context,
        grid,
        source_h3=source_h3,
        visibility_metric=metric,
        class_png_path=class_png,
        output_path=class_html,
        support_geometry_wgs84=render.support_geometry_wgs84,
        config=config,
        assets=assets,
    )
    return (
        {
            "smoothed_html": continuous_html,
            "smoothed_png": continuous_png,
            "smoothed_tif": continuous_tif,
            "generalized_html": class_html,
            "generalized_png": class_png,
            "generalized_tif": class_tif,
            "generalized_metadata": class_json,
        },
        class_metadata,
    )


def export_viewshed_weight_maps(
    edges: EdgeTable,
    source_cells: gpd.GeoDataFrame,
    water_polygon_path: Path,
    output_dir: Path,
    *,
    value_table_path: Path,
    center_lat: float,
    center_lon: float,
    source_selection_radius_km: float,
    viewshed_radius_km: float,
    requested_source_h3: str | None = None,
    config: ViewshedMapConfig | None = None,
) -> ViewshedMapExportResult:
    """Export discrete and smoothed distance, vegetation, terrain, and combined maps."""

    cfg = config or ViewshedMapConfig()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    aggregate = aggregate_target_weight_sums(edges)
    value_table_path = Path(value_table_path)
    value_table_path.parent.mkdir(parents=True, exist_ok=True)
    aggregate.to_parquet(value_table_path, index=False)
    selected_source_h3 = select_source_for_viewshed_map(
        edges,
        source_cells,
        requested_source_h3=requested_source_h3,
    )
    selected_edges = _selected_source_edges(edges, selected_source_h3)
    context = _build_map_context(
        aggregate,
        source_cells,
        center_lat=center_lat,
        center_lon=center_lon,
        source_selection_radius_km=source_selection_radius_km,
        viewshed_radius_km=viewshed_radius_km,
        projected_crs=cfg.projected_crs,
    )
    geometry_assets = (
        _shared_map_geometry_assets(
            context,
            selected_edges,
            source_h3=selected_source_h3,
            output_dir=output_dir,
        )
        if cfg.externalize_map_assets
        else None
    )
    selected_h3_map = write_selected_h3_viewshed_map(
        selected_edges,
        context,
        source_h3=selected_source_h3,
        output_path=output_dir / f"selected_h3_viewshed_{selected_source_h3}.html",
        config=cfg,
        assets=geometry_assets,
    )

    smoothing_grid = _build_smoothing_grid(context, Path(water_polygon_path), cfg)
    maps: dict[str, dict[str, Path]] = {}
    smooth_dir = output_dir / "smoothed"
    for slug, (metric, caption) in FACTOR_MAP_SPECS.items():
        h3_path = write_target_h3_weight_map(
            context,
            metric=metric,
            caption=caption,
            output_path=output_dir / f"h3_{slug}_weight_sum.html",
            config=cfg,
            assets=geometry_assets,
        )
        png_path = smooth_dir / f"smoothed_{slug}_weight_sum.png"
        tif_path = smooth_dir / f"smoothed_{slug}_weight_sum.tif"
        render = _render_smoothed_metric(
            aggregate,
            smoothing_grid,
            metric=metric,
            caption=caption,
            png_path=png_path,
            tif_path=tif_path,
            config=cfg,
        )
        smooth_map_path = write_smoothed_weight_map(
            context,
            smoothing_grid,
            png_path=png_path,
            tif_path=tif_path,
            output_path=output_dir / f"smoothed_{slug}_weight_sum.html",
            metric=metric,
            caption=caption,
            vmax=render.vmax,
            support_geometry_wgs84=render.support_geometry_wgs84,
            config=cfg,
            assets=geometry_assets,
        )
        maps[slug] = {
            "h3_html": h3_path,
            "smoothed_html": smooth_map_path,
            "smoothed_png": png_path,
            "smoothed_tif": tif_path,
        }

    selected_source_maps, selected_source_class_metadata = export_selected_source_smoothed_viewshed(
        selected_edges,
        context,
        smoothing_grid,
        source_h3=selected_source_h3,
        output_dir=output_dir,
        config=cfg,
        assets=geometry_assets,
    )

    manifest_path = output_dir / "weight_map_manifest.json"
    manifest_payload = {
        "selected_source_h3": selected_source_h3,
        "selected_h3_map": str(selected_h3_map),
        "value_table": str(value_table_path),
        "target_aggregation": "sum_across_sources",
        "vegetation_aggregation": "sum_conditional_on_bare_earth_terrain_support",
        "combined_pair_weight": "distance_weighted_terrain_times_conditional_canopy",
        "map_config": asdict(cfg),
        "map_asset_mode": ("external_shared" if geometry_assets is not None else "embedded"),
        "shared_geometry_assets": (
            {name: str(asset.path) for name, asset in sorted(geometry_assets.by_name.items())}
            if geometry_assets is not None
            else {}
        ),
        "selected_source_maps": {name: str(path) for name, path in selected_source_maps.items()},
        "selected_source_smoothing": {
            "metric": cfg.selected_source_metric,
            "target_aggregation": "single_selected_source",
            "support_extent_method": "exact_positive_h3_union",
            "support_boundary_smoothing_km": 0.0,
            "support_containment": "never_outside_positive_metric_h3_union",
            "gaussian_boundary_normalization": True,
            "class_method": "positive_support_quantiles",
            "class_count": cfg.visibility_class_count,
            "class_metadata": selected_source_class_metadata,
        },
        "layers_on_every_factor_map": [
            "Modeled H3 target footprint",
            "Union of source viewshed radii",
            "Selected source H3 cells",
            "Selected point and parameter radii",
        ],
        "maps": {
            slug: {name: str(path) for name, path in paths.items()} for slug, paths in maps.items()
        },
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return ViewshedMapExportResult(
        selected_source_h3=selected_source_h3,
        value_table=value_table_path,
        selected_h3_map=selected_h3_map,
        manifest=manifest_path,
        maps=maps,
        selected_source_maps=selected_source_maps,
    )


def _existing_result(
    app: AppConfig,
    paths: StaticMapOutputPaths,
) -> StaticMapExportResult | None:
    required = (
        paths.selected_html,
        paths.land_aggregate_html,
        paths.water_aggregate_html,
        paths.manifest,
        paths.selected_values,
        paths.land_target_aggregate_values,
        paths.land_source_aggregate_values,
        paths.water_target_aggregate_values,
        paths.water_source_aggregate_values,
    )
    if not all(path.is_file() for path in required):
        return None
    payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if payload.get("config_hash") != app.config_hash:
        return None
    return StaticMapExportResult(
        selected_source_h3=str(payload["selected_source_h3"]),
        selected_html=paths.selected_html,
        land_aggregate_html=paths.land_aggregate_html,
        water_aggregate_html=paths.water_aggregate_html,
        manifest=paths.manifest,
        selected_values=paths.selected_values,
        land_target_aggregate_values=paths.land_target_aggregate_values,
        land_source_aggregate_values=paths.land_source_aggregate_values,
        water_target_aggregate_values=paths.water_target_aggregate_values,
        water_source_aggregate_values=paths.water_source_aggregate_values,
    )


def export_source_type_static_weight_map(
    config_path: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
) -> SourceTypeStaticMapExportResult:
    """Export one source domain without requiring the other to be complete."""

    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be 'land' or 'water'.")
    app = load_app_config(config_path)
    settings = static_map_settings(app)
    if not settings.enabled:
        raise ValueError("Static map export is disabled by static_maps.enabled=false.")
    paths = static_map_output_paths(app)
    aggregate_html = (
        paths.land_aggregate_html if source_type == "land" else paths.water_aggregate_html
    )
    target_values = (
        paths.land_target_aggregate_values
        if source_type == "land"
        else paths.water_target_aggregate_values
    )
    source_values = (
        paths.land_source_aggregate_values
        if source_type == "land"
        else paths.water_source_aggregate_values
    )
    manifest_path = paths.output_dir / f"{source_type}_source_static_weight_map_manifest.json"
    required = (aggregate_html, manifest_path, target_values, source_values)
    if all(path.is_file() for path in required) and not overwrite:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            payload.get("config_hash") == app.config_hash
            and payload.get("source_type") == source_type
        ):
            return SourceTypeStaticMapExportResult(
                source_type=source_type,
                aggregate_html=aggregate_html,
                manifest=manifest_path,
                target_aggregate_values=target_values,
                source_aggregate_values=source_values,
            )
    if not overwrite and any(path.exists() for path in required):
        raise FileExistsError(
            f"Partial {source_type} map outputs exist but do not match the active config. "
            "Rerun export-static-maps with --overwrite."
        )

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        edges = _static_edges(app, source_type=source_type)
    except FileNotFoundError:
        materialize_static_viewability_output(
            config_path,
            source_type=source_type,
            overwrite=overwrite,
        )
        edges = _static_edges(app, source_type=source_type)
    target_pl = _aggregate_edges(edges, by="target_h3")
    source_pl = _aggregate_edges(edges, by="source_h3")
    target_pl.write_parquet(target_values)
    source_pl.write_parquet(source_values)
    layers = _write_aggregate_map(
        target_pl.to_pandas(),
        source_pl.to_pandas(),
        _source_cells(edges),
        app=app,
        settings=settings,
        paths=paths,
        source_type=source_type,
        aggregate_html=aggregate_html,
    )
    manifest = {
        "schema_version": "1",
        "config_path": str(app.config_path),
        "config_hash": app.config_hash,
        "scientific_config_hash": static_scientific_config_hash(app.raw_config),
        "static_artifact_checksum": checksum_path(
            final_artifact_paths(config_path).land_static_weights
            if source_type == "land"
            else final_artifact_paths(config_path).water_static_weights
        ),
        "source_type": source_type,
        "html": str(aggregate_html),
        "value_tables": {
            "target_aggregate": str(target_values),
            "source_aggregate": str(source_values),
        },
        "factors": [spec.slug for spec in FACTOR_SPECS],
        "factor_contract": {
            "distance": "H3-centroid diagnostic only",
            "terrain": "observer-pixel LOS with configured distance kernel integrated",
            "vegetation": "conditional canopy support where terrain support is positive",
            "combined": "terrain times conditional vegetation; no second distance product",
        },
        "aggregations": {
            "target": "sum across source H3 cells",
            "source": "sum across target H3 cells",
        },
        "representations": ["original_h3", "smooth"],
        "map_config": asdict(settings.map_config),
        "aggregate_smooth_layers": layers,
        "excluded_dynamic_factors": ["weather", "daylight", "lunar"],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("Wrote %s-source aggregate map -> %s", source_type, aggregate_html)
    return SourceTypeStaticMapExportResult(
        source_type=source_type,
        aggregate_html=aggregate_html,
        manifest=manifest_path,
        target_aggregate_values=target_values,
        source_aggregate_values=source_values,
    )


def export_static_weight_maps(
    config_path: str | Path,
    *,
    overwrite: bool = False,
) -> StaticMapExportResult:
    """Create source-type-explicit static viewshed maps from final artifacts."""

    app = load_app_config(config_path)
    settings = static_map_settings(app)
    if not settings.enabled:
        raise ValueError("Static map export is disabled by static_maps.enabled=false.")
    paths = static_map_output_paths(app)
    existing = _existing_result(app, paths)
    if existing is not None and not overwrite:
        return existing
    if not overwrite and any(
        path.exists()
        for path in (
            paths.selected_html,
            paths.land_aggregate_html,
            paths.water_aggregate_html,
            paths.manifest,
        )
    ):
        raise FileExistsError(
            "Static map outputs exist but do not match the active config. "
            "Rerun export-static-maps with --overwrite."
        )

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    land_edges = _static_edges(app, source_type="land")
    water_edges = _static_edges(app, source_type="water")
    source_h3 = h3.latlng_to_cell(
        settings.selected_location.latitude,
        settings.selected_location.longitude,
        int(app.h3.source_resolution),
    )
    selected_pl = (
        land_edges.filter(pl.col("source_h3") == source_h3)
        .sort("target_h3")
        .collect(engine="streaming")
    )
    if selected_pl.is_empty():
        raise ValueError(
            "The configured static-map location resolves to a source H3 cell that "
            f"is not present in the finalized land weights: {source_h3} "
            f"({settings.selected_location.latitude}, "
            f"{settings.selected_location.longitude})."
        )

    land_target_pl = _aggregate_edges(land_edges, by="target_h3")
    land_source_pl = _aggregate_edges(land_edges, by="source_h3")
    water_target_pl = _aggregate_edges(water_edges, by="target_h3")
    water_source_pl = _aggregate_edges(water_edges, by="source_h3")
    selected_pl.write_parquet(paths.selected_values)
    land_target_pl.write_parquet(paths.land_target_aggregate_values)
    land_source_pl.write_parquet(paths.land_source_aggregate_values)
    water_target_pl.write_parquet(paths.water_target_aggregate_values)
    water_source_pl.write_parquet(paths.water_source_aggregate_values)
    selected = selected_pl.to_pandas()
    land_target = land_target_pl.to_pandas()
    land_source = land_source_pl.to_pandas()
    water_target = water_target_pl.to_pandas()
    water_source = water_source_pl.to_pandas()
    land_source_cells = _source_cells(land_edges)
    water_source_cells = _source_cells(water_edges)

    selected_layers = _write_selected_map(
        selected,
        land_source_cells,
        source_h3=source_h3,
        app=app,
        settings=settings,
        paths=paths,
    )
    land_aggregate_layers = _write_aggregate_map(
        land_target,
        land_source,
        land_source_cells,
        app=app,
        settings=settings,
        paths=paths,
        source_type="land",
        aggregate_html=paths.land_aggregate_html,
    )
    water_aggregate_layers = _write_aggregate_map(
        water_target,
        water_source,
        water_source_cells,
        app=app,
        settings=settings,
        paths=paths,
        source_type="water",
        aggregate_html=paths.water_aggregate_html,
    )
    manifest = {
        "schema_version": "2",
        "config_path": str(app.config_path),
        "config_hash": app.config_hash,
        "scientific_config_hash": static_scientific_config_hash(app.raw_config),
        "selected_location": asdict(settings.selected_location),
        "selected_source_h3": source_h3,
        "html": {
            "land_source_selected_location": str(paths.selected_html),
            "land_source_aggregate": str(paths.land_aggregate_html),
            "water_source_aggregate": str(paths.water_aggregate_html),
        },
        "value_tables": {
            "land_source_selected_location": str(paths.selected_values),
            "land_source_target_aggregate": str(paths.land_target_aggregate_values),
            "land_source_source_aggregate": str(paths.land_source_aggregate_values),
            "water_source_target_aggregate": str(paths.water_target_aggregate_values),
            "water_source_source_aggregate": str(paths.water_source_aggregate_values),
        },
        "source_types": ["land", "water"],
        "factors": [spec.slug for spec in FACTOR_SPECS],
        "factor_contract": {
            "distance": "H3-centroid diagnostic only",
            "terrain": "observer-pixel LOS with configured distance kernel integrated",
            "vegetation": "conditional canopy support where terrain support is positive",
            "combined": "terrain times conditional vegetation; no second distance product",
        },
        "aggregations": {
            "target": "sum across source H3 cells",
            "source": "sum across target H3 cells",
        },
        "representations": ["original_h3", "smooth"],
        "map_config": asdict(settings.map_config),
        "selected_smooth_layers": selected_layers,
        "aggregate_smooth_layers": {
            "land": land_aggregate_layers,
            "water": water_aggregate_layers,
        },
        "excluded_dynamic_factors": ["weather", "daylight", "lunar"],
    }
    static_paths = final_artifact_paths(config_path)
    manifest["static_artifacts"] = {
        "land": {
            "path": str(static_paths.land_static_weights),
            "checksum": checksum_path(static_paths.land_static_weights),
        },
        "water": {
            "path": str(static_paths.water_static_weights),
            "checksum": checksum_path(static_paths.water_static_weights),
        },
    }
    paths.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for source_type, aggregate_html, target_values, source_values, layers in (
        (
            "land",
            paths.land_aggregate_html,
            paths.land_target_aggregate_values,
            paths.land_source_aggregate_values,
            land_aggregate_layers,
        ),
        (
            "water",
            paths.water_aggregate_html,
            paths.water_target_aggregate_values,
            paths.water_source_aggregate_values,
            water_aggregate_layers,
        ),
    ):
        alias = {
            "schema_version": "2",
            "config_path": str(app.config_path),
            "config_hash": app.config_hash,
            "scientific_config_hash": static_scientific_config_hash(app.raw_config),
            "source_type": source_type,
            "html": str(aggregate_html),
            "value_tables": {
                "target_aggregate": str(target_values),
                "source_aggregate": str(source_values),
            },
            "factors": [spec.slug for spec in FACTOR_SPECS],
            "factor_contract": manifest["factor_contract"],
            "aggregations": manifest["aggregations"],
            "representations": manifest["representations"],
            "map_config": manifest["map_config"],
            "aggregate_smooth_layers": layers,
            "excluded_dynamic_factors": manifest["excluded_dynamic_factors"],
            "static_artifact": manifest["static_artifacts"][source_type],
        }
        alias_path = paths.output_dir / f"{source_type}_source_static_weight_map_manifest.json"
        alias_path.write_text(
            json.dumps(alias, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    LOGGER.info("Wrote selected static viewshed map -> %s", paths.selected_html)
    LOGGER.info("Wrote land-source aggregate map -> %s", paths.land_aggregate_html)
    LOGGER.info("Wrote water-source aggregate map -> %s", paths.water_aggregate_html)
    return StaticMapExportResult(
        selected_source_h3=source_h3,
        selected_html=paths.selected_html,
        land_aggregate_html=paths.land_aggregate_html,
        water_aggregate_html=paths.water_aggregate_html,
        manifest=paths.manifest,
        selected_values=paths.selected_values,
        land_target_aggregate_values=paths.land_target_aggregate_values,
        land_source_aggregate_values=paths.land_source_aggregate_values,
        water_target_aggregate_values=paths.water_target_aggregate_values,
        water_source_aggregate_values=paths.water_source_aggregate_values,
    )
