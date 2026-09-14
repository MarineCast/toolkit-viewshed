"""Raster smoothing and PNG/GeoTIFF rendering for viewshed maps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import array_bounds, from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds
from scipy.ndimage import gaussian_filter
from shapely.geometry import box, mapping
from shapely.ops import unary_union

from .data import ViewshedMapConfig, _MapContext, _SmoothedMetricRender, _SmoothingGrid
from .styling import _display_max, _map_colors, _resolve_colormap


def quantile_visibility_classes(
    values: np.ndarray,
    *,
    class_count: int = 10,
    positive_threshold: float = 0.0,
) -> tuple[np.ndarray, list[float], int]:
    """Classify positive visibility support from poor (1) to high (N).

    Zero, negative, and non-finite values remain class 0 (no modeled support).
    Quantile breaks are calculated only from positive support so the generalized
    map uses its classes to distinguish the visible portion of the viewshed.
    """

    class_count = int(class_count)
    if class_count < 2 or class_count > 254:
        raise ValueError("class_count must be between 2 and 254.")
    array = np.asarray(values, dtype="float64")
    classes = np.zeros(array.shape, dtype="uint8")
    positive = np.isfinite(array) & (array > float(positive_threshold))
    count = int(np.count_nonzero(positive))
    if count == 0:
        return classes, [0.0] * (class_count + 1), 0

    quantiles = np.linspace(0.0, 1.0, class_count + 1)
    breaks = np.quantile(array[positive], quantiles)
    boundaries = breaks[1:-1]
    classes[positive] = (np.searchsorted(boundaries, array[positive], side="right") + 1).astype(
        "uint8"
    )
    return classes, [float(value) for value in breaks], count


def _polygon_components(geometry: Any) -> list[Any]:
    """Return nonempty polygon components without retaining collections."""

    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    components: list[Any] = []
    for part in getattr(geometry, "geoms", ()):  # MultiPolygon/GeometryCollection
        components.extend(_polygon_components(part))
    return components


def generalize_viewshed_support_geometry(
    geometry: Any,
    *,
    smoothing_distance_m: float,
) -> Any:
    """Round an H3 support boundary without expanding beyond its source cells.

    A per-component morphological opening removes small outward stair-steps and
    rounds corners.  The opening radius is capped for narrow components so
    one-cell islands and thin but real viewshed arms are retained.  The final
    intersection is a numerical containment gate: generalized support cannot
    create visibility outside the original positive-support H3 union.
    """

    components = _polygon_components(geometry)
    if not components:
        raise ValueError("Viewshed support geometry is empty.")
    requested_distance = max(0.0, float(smoothing_distance_m))
    if requested_distance == 0.0:
        return unary_union(components)

    generalized_components: list[Any] = []
    for component in components:
        min_x, min_y, max_x, max_y = component.bounds
        minimum_dimension = min(max_x - min_x, max_y - min_y)
        component_distance = min(requested_distance, minimum_dimension * 0.22)
        if component_distance <= 0.0:
            generalized_components.append(component)
            continue
        eroded = component.buffer(-component_distance)
        rounded = eroded.buffer(component_distance) if not eroded.is_empty else component
        if rounded.is_empty:
            rounded = component
        contained = rounded.intersection(component)
        generalized_components.append(contained if not contained.is_empty else component)

    generalized = unary_union(generalized_components)
    original = unary_union(components)
    contained = generalized.intersection(original)
    if contained.is_empty:
        raise ValueError("Boundary generalization removed the complete viewshed support.")
    return contained


def _masked_gaussian_filter(
    values: np.ndarray,
    support_mask: np.ndarray,
    *,
    sigma_pixels: float,
) -> np.ndarray:
    """Smooth values inside support without diffusion or attenuation at its edge."""

    mask = np.asarray(support_mask, dtype=bool)
    value_array = np.asarray(values, dtype="float32")
    output = np.full(value_array.shape, np.nan, dtype="float32")
    if not mask.any():
        return output
    if float(sigma_pixels) <= 0.0:
        output[mask] = value_array[mask]
        return output

    numerator = gaussian_filter(
        np.where(mask, value_array, 0.0),
        sigma=(sigma_pixels, sigma_pixels),
        mode="constant",
        cval=0.0,
        truncate=3.0,
        output=np.float32,
    )
    denominator = gaussian_filter(
        mask.astype("float32"),
        sigma=(sigma_pixels, sigma_pixels),
        mode="constant",
        cval=0.0,
        truncate=3.0,
        output=np.float32,
    )
    np.divide(
        numerator,
        denominator,
        out=output,
        where=mask & (denominator > np.finfo("float32").eps),
    )
    output[~mask] = np.nan
    return output


def _build_smoothing_grid(
    context: _MapContext,
    water_polygon_path: Path | None,
    config: ViewshedMapConfig,
) -> _SmoothingGrid:
    target_projected = context.target_gdf.to_crs(config.projected_crs)
    min_x, min_y, max_x, max_y = target_projected.total_bounds
    padding_m = float(config.raster_padding_km) * 1_000.0
    pixel = float(config.analysis_pixel_size_m)
    west = np.floor((min_x - padding_m) / pixel) * pixel
    south = np.floor((min_y - padding_m) / pixel) * pixel
    east = np.ceil((max_x + padding_m) / pixel) * pixel
    north = np.ceil((max_y + padding_m) / pixel) * pixel
    width = max(1, int(round((east - west) / pixel)))
    height = max(1, int(round((north - south) / pixel)))
    transform = from_origin(west, north, pixel, pixel)
    target_geometries = {
        str(row.target_h3): row.geometry
        for row in target_projected[["target_h3", "geometry"]].itertuples(index=False)
    }
    target_shapes = {cell: mapping(geometry) for cell, geometry in target_geometries.items()}
    footprint_mask = rasterize(
        [(geometry, 1) for geometry in target_shapes.values()],
        out_shape=(height, width),
        transform=transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    )

    if water_polygon_path is None:
        valid_mask = footprint_mask.astype("uint8")
    else:
        water = gpd.read_parquet(water_polygon_path).to_crs(config.projected_crs)
        water = water.loc[water.geometry.notna() & ~water.geometry.is_empty].copy()
        water = water.loc[water.geometry.intersects(box(west, south, east, north))]
        if water.empty:
            raise ValueError("Water polygon does not overlap the viewshed map footprint.")
        water_mask = rasterize(
            [(mapping(geometry), 1) for geometry in water.geometry],
            out_shape=(height, width),
            transform=transform,
            fill=0,
            all_touched=False,
            dtype="uint8",
        )
        valid_mask = (footprint_mask.astype(bool) & water_mask.astype(bool)).astype("uint8")
    if not valid_mask.any():
        domain = "water" if water_polygon_path is not None else "H3"
        raise ValueError(f"Smoothed viewshed footprint contains no modeled {domain} pixels.")

    left, bottom, right, top = array_bounds(height, width, transform)
    output_transform, output_width, output_height = calculate_default_transform(
        config.projected_crs,
        config.output_crs,
        width,
        height,
        left,
        bottom,
        right,
        top,
        resolution=float(config.output_pixel_size_m),
    )
    output_valid_mask = np.zeros((output_height, output_width), dtype="uint8")
    reproject(
        source=valid_mask,
        destination=output_valid_mask,
        src_transform=transform,
        src_crs=config.projected_crs,
        dst_transform=output_transform,
        dst_crs=config.output_crs,
        src_nodata=0,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    bounds_wgs84 = transform_bounds(
        config.output_crs,
        "EPSG:4326",
        *array_bounds(output_height, output_width, output_transform),
    )
    return _SmoothingGrid(
        projected_crs=config.projected_crs,
        transform=transform,
        width=width,
        height=height,
        valid_mask=valid_mask,
        target_shapes=target_shapes,
        target_geometries=target_geometries,
        output_crs=config.output_crs,
        output_transform=output_transform,
        output_width=output_width,
        output_height=output_height,
        output_valid_mask=output_valid_mask,
        bounds_wgs84=bounds_wgs84,
    )


def _metric_support(
    aggregate: pd.DataFrame,
    grid: _SmoothingGrid,
    *,
    metric: str,
    config: ViewshedMapConfig,
) -> tuple[np.ndarray, np.ndarray, Any, int]:
    """Build contained analysis/output masks from positive H3 metric support."""

    numeric = pd.to_numeric(aggregate[metric], errors="coerce").fillna(0.0)
    cells = aggregate.loc[numeric > 0.0, "target_h3"].astype(str).drop_duplicates()
    geometries = [grid.target_geometries[cell] for cell in cells if cell in grid.target_geometries]
    if not geometries:
        raise ValueError(f"Smoothed metric has no positive H3 support: {metric}")
    original_support = unary_union(geometries)
    generalized_support = generalize_viewshed_support_geometry(
        original_support,
        smoothing_distance_m=float(config.support_boundary_smoothing_km) * 1_000.0,
    )
    support_mask = rasterize(
        [(mapping(generalized_support), 1)],
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        all_touched=False,
        dtype="uint8",
    )
    support_mask = (support_mask.astype(bool) & grid.valid_mask.astype(bool)).astype("uint8")
    if not support_mask.any():
        raise ValueError(f"Generalized support contains no modeled water pixels: {metric}")

    support_series = gpd.GeoSeries([generalized_support], crs=grid.projected_crs)
    support_output_geometry = support_series.to_crs(grid.output_crs).iloc[0]
    output_support_mask = rasterize(
        [(mapping(support_output_geometry), 1)],
        out_shape=(grid.output_height, grid.output_width),
        transform=grid.output_transform,
        fill=0,
        all_touched=False,
        dtype="uint8",
    )
    output_support_mask = (
        output_support_mask.astype(bool) & grid.output_valid_mask.astype(bool)
    ).astype("uint8")
    if not output_support_mask.any():
        raise ValueError(f"Generalized support has no output-grid pixels: {metric}")
    support_wgs84 = support_series.to_crs("EPSG:4326").iloc[0]
    return support_mask, output_support_mask, support_wgs84, int(len(cells))


def _render_smoothed_metric(
    aggregate: pd.DataFrame,
    grid: _SmoothingGrid,
    *,
    metric: str,
    caption: str,
    png_path: Path,
    tif_path: Path,
    config: ViewshedMapConfig,
    target_aggregation: str = "sum_across_sources",
    pair_combination: str | None = None,
    source_h3: str | None = None,
    clip_domain: str = "modeled_water",
) -> _SmoothedMetricRender:
    support_mask, output_support_mask, support_wgs84, positive_cell_count = _metric_support(
        aggregate, grid, metric=metric, config=config
    )
    values_by_cell = dict(
        zip(
            aggregate["target_h3"].astype(str),
            pd.to_numeric(aggregate[metric], errors="coerce").fillna(0.0),
            strict=True,
        )
    )
    value_raster = rasterize(
        [
            (grid.target_shapes[cell], float(value))
            for cell, value in values_by_cell.items()
            if cell in grid.target_shapes
        ],
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0.0,
        all_touched=True,
        dtype="float32",
    )
    value_raster *= support_mask
    sigma_pixels = max(
        0.0,
        float(config.gaussian_sigma_km) * 1_000.0 / float(config.analysis_pixel_size_m),
    )
    smoothed = _masked_gaussian_filter(
        value_raster,
        support_mask,
        sigma_pixels=sigma_pixels,
    )

    output = np.full((grid.output_height, grid.output_width), np.nan, dtype="float32")
    reproject(
        source=smoothed,
        destination=output,
        src_transform=grid.transform,
        src_crs=grid.projected_crs,
        dst_transform=grid.output_transform,
        dst_crs=grid.output_crs,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    output[~output_support_mask.astype(bool)] = np.nan
    vmax = _display_max(output, config.display_quantile)
    normalized = np.clip(np.nan_to_num(output, nan=0.0) / vmax, 0.0, 1.0)
    rgba = _resolve_colormap(config.colormap_name)(normalized)
    positive_support = np.isfinite(output) & (output > max(vmax * 1e-6, 1e-12))
    rgba[..., 3] = np.where(
        output_support_mask.astype(bool) & positive_support,
        config.smoothed_fill_opacity,
        0.0,
    )
    rgba_u8 = np.clip(rgba * 255.0, 0, 255).astype("uint8")

    png_path.parent.mkdir(parents=True, exist_ok=True)
    tif_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba_u8, mode="RGBA").save(png_path, optimize=True)
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=grid.output_height,
        width=grid.output_width,
        count=1,
        dtype="float32",
        crs=grid.output_crs,
        transform=grid.output_transform,
        nodata=np.nan,
        compress="deflate",
    ) as destination:
        destination.write(output, 1)
        tags = dict(
            metric=metric,
            caption=caption,
            target_aggregation=target_aggregation,
            pair_combination=pair_combination
            or (
                "distance_weighted_terrain_times_conditional_canopy"
                if metric in {"target_static_kernel_sum", "net_static_weight"}
                else "single_factor"
            ),
            gaussian_sigma_km=str(config.gaussian_sigma_km),
            support_boundary_smoothing_km=str(config.support_boundary_smoothing_km),
            positive_support_h3_cell_count=str(positive_cell_count),
            analysis_pixel_size_m=str(config.analysis_pixel_size_m),
            output_pixel_size_m=str(config.output_pixel_size_m),
            colormap=config.colormap_name,
            clip_domain=str(clip_domain),
            clipped_to_modeled_water_footprint=str(clip_domain == "modeled_water").lower(),
            clipped_to_positive_metric_support="true",
            support_extent_method=(
                "exact_positive_h3_union"
                if config.support_boundary_smoothing_km == 0.0
                else "contained_morphological_opening"
            ),
            gaussian_boundary_normalization="true",
            zero_support_transparent="true",
        )
        if source_h3 is not None:
            tags["source_h3"] = str(source_h3)
        destination.update_tags(**tags)
    return _SmoothedMetricRender(
        vmax=vmax,
        support_geometry_wgs84=support_wgs84,
        positive_cell_count=positive_cell_count,
    )


def _write_generalized_visibility_class_artifacts(
    continuous_tif_path: Path,
    *,
    class_tif_path: Path,
    class_png_path: Path,
    metadata_path: Path,
    source_h3: str,
    visibility_metric: str,
    config: ViewshedMapConfig,
) -> dict[str, Any]:
    """Create a ten-class poor-to-high visibility surface from a smooth raster."""

    with rasterio.open(continuous_tif_path) as source:
        continuous = source.read(1).astype("float32")
        profile = source.profile.copy()
    display_max = _display_max(continuous, config.display_quantile)
    positive_threshold = max(display_max * 1e-6, 1e-12)
    classes, breaks, positive_pixel_count = quantile_visibility_classes(
        continuous,
        class_count=config.visibility_class_count,
        positive_threshold=positive_threshold,
    )
    colors = _map_colors(config, config.visibility_class_count)
    rgba = np.zeros((*classes.shape, 4), dtype="uint8")
    for class_index, color in enumerate(colors, start=1):
        rgb = tuple(int(color[offset : offset + 2], 16) for offset in (1, 3, 5))
        mask = classes == class_index
        rgba[..., 0][mask] = rgb[0]
        rgba[..., 1][mask] = rgb[1]
        rgba[..., 2][mask] = rgb[2]
        rgba[..., 3][mask] = int(round(255.0 * config.smoothed_fill_opacity))

    class_png_path.parent.mkdir(parents=True, exist_ok=True)
    class_tif_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(class_png_path, optimize=True)
    profile.update(dtype="uint8", count=1, nodata=0, compress="deflate")
    with rasterio.open(class_tif_path, "w", **profile) as destination:
        destination.write(classes, 1)
        destination.update_tags(
            source_h3=str(source_h3),
            source_visibility_metric=visibility_metric,
            class_method="positive_support_quantiles",
            class_count=str(config.visibility_class_count),
            class_breaks=json.dumps(breaks),
            class_zero="no_modeled_visibility",
            class_one="poor_visibility",
            class_high=str(config.visibility_class_count),
            class_high_label="high_visibility",
            colormap=config.colormap_name,
            support_extent_method=(
                "exact_positive_h3_union"
                if config.support_boundary_smoothing_km == 0.0
                else "contained_morphological_opening"
            ),
            support_boundary_smoothing_km=str(config.support_boundary_smoothing_km),
        )

    metadata = {
        "source_h3": str(source_h3),
        "source_visibility_metric": visibility_metric,
        "class_method": "positive_support_quantiles",
        "class_count": int(config.visibility_class_count),
        "class_breaks": breaks,
        "positive_threshold": float(positive_threshold),
        "positive_pixel_count": int(positive_pixel_count),
        "support_extent_method": (
            "exact_positive_h3_union"
            if config.support_boundary_smoothing_km == 0.0
            else "contained_morphological_opening"
        ),
        "support_boundary_smoothing_km": float(config.support_boundary_smoothing_km),
        "labels": {
            "0": "No modeled visibility",
            "1": "Poor visibility",
            str(config.visibility_class_count): "High visibility",
        },
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata
