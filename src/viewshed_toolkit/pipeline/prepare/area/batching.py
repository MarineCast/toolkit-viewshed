"""Source-cell batch identification and scheduling."""

from __future__ import annotations

from typing import Sequence

import geopandas as gpd

from ...config import AppConfig
from .validation import _iter_batches, validated_h3_parent_batches


def make_batch_id(app: AppConfig, batch_index: int, source_cells: Sequence[str]) -> str:
    first_cell = source_cells[0]
    last_cell = source_cells[-1]
    return (
        f"{app.run.name}_batch{batch_index:04d}_"
        f"{len(source_cells)}cells_{first_cell[-6:]}_{last_cell[-6:]}"
    )


def iter_source_batches(
    source_cells_gdf: gpd.GeoDataFrame,
    app: AppConfig,
    source_sample_points_gdf: gpd.GeoDataFrame | None = None,
) -> list[list[str]]:
    batch_size = max(1, int(app.batch.batch_size_cells))
    strategy = app.batch.strategy
    source = source_cells_gdf.copy()
    if "h3_cell" not in source.columns:
        raise ValueError("Source cells must include h3_cell.")

    if strategy == "sequential":
        ordered = [str(cell) for cell in source["h3_cell"].tolist()]
        return _iter_batches(ordered, batch_size)

    if strategy == "spatial_sort":
        geom = source.geometry
        centroids = geom.representative_point()
        source["_sort_lon"] = centroids.x
        source["_sort_lat"] = centroids.y
        source = source.sort_values(["_sort_lon", "_sort_lat", "h3_cell"])
        return _iter_batches([str(cell) for cell in source["h3_cell"].tolist()], batch_size)

    if strategy == "h3_parent":
        return validated_h3_parent_batches(
            source,
            app,
            source_sample_points_gdf=source_sample_points_gdf,
            batch_size=batch_size,
        )

    raise ValueError(f"Unsupported batch.strategy: {strategy}")
