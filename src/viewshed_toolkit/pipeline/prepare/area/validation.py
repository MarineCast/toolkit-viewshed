"""Spatial packing guardrails and validation for viewshed batches."""

from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import geopandas as gpd

from viewshed_toolkit._internal.geo.h3 import cell_to_parent, get_resolution

from ...config import AppConfig, viewshed_config_from_app_config

LOGGER = logging.getLogger(__name__)


def _iter_batches(items: Sequence[str], batch_size: int) -> list[list[str]]:
    batch_size = max(1, int(batch_size))
    return [list(items[i : i + batch_size]) for i in range(0, len(items), batch_size)]


def _effective_parent_resolution(source_resolution: int) -> int:
    source_resolution = int(source_resolution)
    if source_resolution <= 0:
        raise ValueError(f"source_resolution must be positive, got {source_resolution}")
    return max(0, source_resolution - 1)


@dataclass(frozen=True)
class _BatchPackingGroup:
    cells: tuple[str, ...]
    min_x: float
    min_y: float
    max_x: float
    max_y: float


def _source_observer_bounds(
    source_cells_gdf: gpd.GeoDataFrame,
    source_sample_points_gdf: gpd.GeoDataFrame | None,
    *,
    projected_crs: str,
) -> dict[str, tuple[float, float, float, float]]:
    """Return deterministic projected observer bounds for every source cell."""

    source_ids = tuple(str(cell) for cell in source_cells_gdf["h3_cell"].tolist())
    if source_sample_points_gdf is not None:
        if "source_h3_cell" not in source_sample_points_gdf.columns:
            raise ValueError(
                "Prepared source samples must include source_h3_cell for spatial packing."
            )
        if source_sample_points_gdf.crs is None:
            raise ValueError("Prepared source samples must declare a CRS for spatial packing.")
        projected = source_sample_points_gdf.to_crs(projected_crs)
        bounds: dict[str, tuple[float, float, float, float]] = {}
        for source_cell, group in projected.groupby("source_h3_cell", sort=False):
            geometries = group.geometry[group.geometry.notna() & ~group.geometry.is_empty]
            if geometries.empty:
                continue
            min_x, min_y, max_x, max_y = geometries.total_bounds
            bounds[str(source_cell)] = (
                float(min_x),
                float(min_y),
                float(max_x),
                float(max_y),
            )
        missing = sorted(set(source_ids) - set(bounds))
        if missing:
            raise ValueError(
                "Prepared source samples are missing observer geometries for spatial "
                f"batch packing: {missing[:10]}"
            )
        return {cell: bounds[cell] for cell in source_ids}

    if source_cells_gdf.crs is None:
        raise ValueError("Source cells must declare a CRS for spatial batch packing.")
    projected_source = source_cells_gdf.to_crs(projected_crs)
    representative_points = projected_source.geometry.representative_point()
    return {
        str(cell): (
            float(point.x),
            float(point.y),
            float(point.x),
            float(point.y),
        )
        for cell, point in zip(
            projected_source["h3_cell"].astype(str),
            representative_points,
            strict=True,
        )
    }


def _packing_group(
    cells: Sequence[str],
    observer_bounds: dict[str, tuple[float, float, float, float]],
) -> _BatchPackingGroup:
    ordered = tuple(sorted(str(cell) for cell in cells))
    bounds = [observer_bounds[cell] for cell in ordered]
    return _BatchPackingGroup(
        cells=ordered,
        min_x=min(value[0] for value in bounds),
        min_y=min(value[1] for value in bounds),
        max_x=max(value[2] for value in bounds),
        max_y=max(value[3] for value in bounds),
    )


def _merge_packing_groups(
    left: _BatchPackingGroup,
    right: _BatchPackingGroup,
) -> _BatchPackingGroup:
    return _BatchPackingGroup(
        cells=tuple(sorted((*left.cells, *right.cells))),
        min_x=min(left.min_x, right.min_x),
        min_y=min(left.min_y, right.min_y),
        max_x=max(left.max_x, right.max_x),
        max_y=max(left.max_y, right.max_y),
    )


def _packing_group_pixel_count(
    group: _BatchPackingGroup,
    *,
    padding_m: float,
    resolution_m: float,
) -> int:
    # The two-pixel allowance conservatively covers raster-window snapping at
    # both sides of a non-grid-aligned observer-buffer envelope.
    width = max(
        1,
        int(math.ceil((group.max_x - group.min_x + 2.0 * padding_m) / resolution_m)) + 2,
    )
    height = max(
        1,
        int(math.ceil((group.max_y - group.min_y + 2.0 * padding_m) / resolution_m)) + 2,
    )
    return int(width * height)


def _split_packing_group_to_pixel_limit(
    group: _BatchPackingGroup,
    *,
    observer_bounds: dict[str, tuple[float, float, float, float]],
    padding_m: float,
    resolution_m: float,
    max_pixels: int | None,
) -> list[_BatchPackingGroup]:
    """Split an unsafe candidate batch into deterministic spatial halves."""

    pixel_count = _packing_group_pixel_count(
        group,
        padding_m=padding_m,
        resolution_m=resolution_m,
    )
    if max_pixels is None or pixel_count <= max_pixels:
        return [group]
    if len(group.cells) == 1:
        raise ValueError(
            "A single viewshed source exceeds the configured batch guardrails: "
            f"source_h3_cell={group.cells[0]} estimated_pixels={pixel_count:,} "
            f"max_pixels={int(max_pixels):,}. Reduce the viewshed radius or raster "
            "resolution, or raise the guardrail after validating available memory."
        )

    axis = 0 if (group.max_x - group.min_x) >= (group.max_y - group.min_y) else 1

    def spatial_key(cell: str) -> tuple[float, float, str]:
        min_x, min_y, max_x, max_y = observer_bounds[cell]
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
        if axis == 0:
            return (center_x, center_y, cell)
        return (center_y, center_x, cell)

    ordered = sorted(group.cells, key=spatial_key)
    midpoint = len(ordered) // 2
    children = (
        _packing_group(ordered[:midpoint], observer_bounds),
        _packing_group(ordered[midpoint:], observer_bounds),
    )
    return [
        split
        for child in children
        for split in _split_packing_group_to_pixel_limit(
            child,
            observer_bounds=observer_bounds,
            padding_m=padding_m,
            resolution_m=resolution_m,
            max_pixels=max_pixels,
        )
    ]


def _minimum_runtime_memory_pixel_limit(app: AppConfig) -> int | None:
    """Pixel limit matching the runtime estimator with one source coordinator."""

    maximum_mb = app.batch.max_estimated_batch_memory_mb
    if maximum_mb is None:
        return None
    resolution_m = float(app.viewshed.dem_resolution_m)
    radius_pixels = int(math.ceil(app.viewshed.max_distance_m / resolution_m))
    source_window_cap = int((2 * radius_pixels + 5) ** 2)
    # Keep this synchronized with runner._estimated_runtime_peak_memory_mb:
    # 36 base bytes/pixel, 34 accumulator bytes/window pixel, and at most
    # max_workers + 1 one-byte observer results with one active coordinator.
    source_window_bytes = 34 + max(1, int(app.batch.max_workers)) + 1
    budget_bytes = float(maximum_mb) * 1024.0 * 1024.0
    capped_window_threshold = source_window_cap * (36 + source_window_bytes)
    if budget_bytes <= capped_window_threshold:
        return max(1, int(math.floor(budget_bytes / (36 + source_window_bytes))))
    return max(
        1,
        int(math.floor((budget_bytes - source_window_cap * source_window_bytes) / 36.0)),
    )


def _pack_h3_parent_remainders(
    remainder_groups: Sequence[_BatchPackingGroup],
    *,
    batch_size: int,
    padding_m: float,
    resolution_m: float,
    max_pixels: int | None,
) -> list[_BatchPackingGroup]:
    """Apply the deterministic greedy policy without rescoring unchanged pairs.

    A merge changes only candidates involving its two removed groups and its
    new group. Keep other candidates in a heap and discard stale entries when
    popped. This preserves the exhaustive planner's ordering with quadratic
    candidate evaluations instead of a complete rescan after every merge.
    """

    active = dict(enumerate(remainder_groups))
    pixels = {
        key: _packing_group_pixel_count(group, padding_m=padding_m, resolution_m=resolution_m)
        for key, group in active.items()
    }
    candidates: list[tuple[tuple[int, int, int, tuple[str, ...]], int, int]] = []

    def add_candidate(left_id: int, right_id: int) -> None:
        left, right = active[left_id], active[right_id]
        if len(left.cells) + len(right.cells) > batch_size:
            return
        merged = _merge_packing_groups(left, right)
        merged_pixels = _packing_group_pixel_count(
            merged, padding_m=padding_m, resolution_m=resolution_m
        )
        if max_pixels is not None and merged_pixels > max_pixels:
            return
        savings = pixels[left_id] + pixels[right_id] - merged_pixels
        if savings < 0:
            return
        score = (-len(merged.cells), -savings, merged_pixels, merged.cells)
        heapq.heappush(candidates, (score, left_id, right_id))

    for left_id in active:
        for right_id in range(left_id + 1, len(active)):
            add_candidate(left_id, right_id)
    next_id = len(active)
    while candidates:
        _, left_id, right_id = heapq.heappop(candidates)
        if left_id not in active or right_id not in active:
            continue
        merged = _merge_packing_groups(active.pop(left_id), active.pop(right_id))
        pixels.pop(left_id)
        pixels.pop(right_id)
        active[next_id] = merged
        pixels[next_id] = _packing_group_pixel_count(
            merged, padding_m=padding_m, resolution_m=resolution_m
        )
        for other_id in active:
            if other_id != next_id:
                add_candidate(other_id, next_id)
        next_id += 1
    return list(active.values())


def validated_h3_parent_batches(
    source: gpd.GeoDataFrame,
    app: AppConfig,
    *,
    source_sample_points_gdf: gpd.GeoDataFrame | None,
    batch_size: int,
) -> list[list[str]]:
    """Pack H3-parent batches while enforcing raster and memory guardrails."""

    if hasattr(app, "h3") and hasattr(app.h3, "source_resolution"):
        source_resolution = int(app.h3.source_resolution)
    else:
        source_resolution = int(get_resolution(str(source["h3_cell"].iloc[0])))
    parent_resolution = _effective_parent_resolution(source_resolution)
    source["_parent"] = source["h3_cell"].map(
        lambda cell: cell_to_parent(str(cell), parent_resolution)
    )
    full_batches: list[list[str]] = []
    remainder_cells: list[list[str]] = []
    for _, group in source.sort_values(["_parent", "h3_cell"]).groupby("_parent", sort=True):
        cells = [str(cell) for cell in group["h3_cell"].tolist()]
        parent_batches = _iter_batches(cells, batch_size)
        full_batches.extend(batch for batch in parent_batches if len(batch) == batch_size)
        remainder_cells.extend(batch for batch in parent_batches if len(batch) < batch_size)

    config = viewshed_config_from_app_config(app)
    observer_bounds = _source_observer_bounds(
        source,
        source_sample_points_gdf,
        projected_crs=config.crs_projected,
    )
    remainder_groups = [_packing_group(cells, observer_bounds) for cells in remainder_cells]
    pixel_limits = [
        int(value)
        for value in (
            app.batch.max_batch_aoi_pixels,
            _minimum_runtime_memory_pixel_limit(app),
        )
        if value is not None
    ]
    max_pixels = min(pixel_limits) if pixel_limits else None
    padding_m = float(config.max_distance_m + config.aoi_margin_m)
    split_full_groups = [
        split
        for batch in full_batches
        for split in _split_packing_group_to_pixel_limit(
            _packing_group(batch, observer_bounds),
            observer_bounds=observer_bounds,
            padding_m=padding_m,
            resolution_m=float(config.dem_resolution_m),
            max_pixels=max_pixels,
        )
    ]
    safe_remainder_groups = [
        split
        for group in remainder_groups
        for split in _split_packing_group_to_pixel_limit(
            group,
            observer_bounds=observer_bounds,
            padding_m=padding_m,
            resolution_m=float(config.dem_resolution_m),
            max_pixels=max_pixels,
        )
    ]
    packed_remainders = _pack_h3_parent_remainders(
        safe_remainder_groups,
        batch_size=batch_size,
        padding_m=padding_m,
        resolution_m=float(config.dem_resolution_m),
        max_pixels=max_pixels,
    )
    groups = split_full_groups + packed_remainders
    groups.sort(key=lambda group: (group.min_x, group.min_y, group.cells))
    LOGGER.info(
        "h3_parent_batch_packing source_count=%d batch_size=%d "
        "parent_batch_count=%d packed_batch_count=%d "
        "parent_remainder_count=%d packed_remainder_count=%d max_pixels=%s",
        len(source),
        batch_size,
        len(full_batches) + len(remainder_cells),
        len(groups),
        len(remainder_cells),
        len(packed_remainders),
        max_pixels,
    )
    return [list(group.cells) for group in groups]
