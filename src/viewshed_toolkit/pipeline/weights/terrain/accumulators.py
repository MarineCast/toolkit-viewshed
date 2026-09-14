"""Sparse tiled accumulators for per-source terrain visibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from .summarize import (
    DISTANCE_SUM_SCALE,
    _accumulate_visible,
    _init_cumulative_arrays,
)


@dataclass
class TileAccumulator:
    """The six cumulative arrays owned by one lazily allocated raster tile."""

    row_start: int
    col_start: int
    visible_count: np.ndarray
    min_distance: np.ndarray
    max_distance: np.ndarray
    distance_sum: np.ndarray
    distance_weight_sum: np.ndarray
    observer_mask: np.ndarray | None

    @property
    def shape(self) -> tuple[int, int]:
        return self.visible_count.shape

    @property
    def memory_bytes(self) -> int:
        arrays = (
            self.visible_count,
            self.min_distance,
            self.max_distance,
            self.distance_sum,
            self.distance_weight_sum,
            self.observer_mask,
        )
        return sum(array.nbytes for array in arrays if array is not None)

    def finalized_distances(self) -> tuple[np.ndarray, np.ndarray]:
        """Return normalized minimum and mean distance arrays for this tile."""

        valid = self.visible_count > 0
        minimum = self.min_distance.copy()
        minimum[~valid] = 0.0
        mean = np.zeros(self.shape, dtype="float32")
        mean[valid] = (
            self.distance_sum[valid] / float(DISTANCE_SUM_SCALE) / self.visible_count[valid]
        )
        return minimum, mean


@dataclass(frozen=True)
class SparseAccumulatorArrays:
    """Visible pixels extracted from allocated tiles for H3 reduction."""

    rows: np.ndarray
    cols: np.ndarray
    visible_count: np.ndarray
    min_distance: np.ndarray
    mean_distance: np.ndarray
    max_distance: np.ndarray
    distance_weight_sum: np.ndarray
    observer_mask: np.ndarray | None


class TiledAccumulator:
    """Allocate cumulative arrays only for tiles touched by visible water."""

    def __init__(
        self,
        shape: tuple[int, int],
        *,
        row_offset: int,
        col_offset: int,
        n_observers: int,
        tile_size: int = 512,
    ) -> None:
        if len(shape) != 2 or min(shape) <= 0:
            raise ValueError(f"Accumulator shape must be two positive dimensions: {shape}")
        if int(tile_size) <= 0:
            raise ValueError("Accumulator tile_size must be positive.")
        self.shape = (int(shape[0]), int(shape[1]))
        self.row_offset = int(row_offset)
        self.col_offset = int(col_offset)
        self.n_observers = int(n_observers)
        self.tile_size = int(tile_size)
        self.tiles: dict[tuple[int, int], TileAccumulator] = {}

    @property
    def allocated_pixel_count(self) -> int:
        return sum(tile.visible_count.size for tile in self.tiles.values())

    @property
    def visible_pixel_count(self) -> int:
        return sum(int(np.count_nonzero(tile.visible_count)) for tile in self.tiles.values())

    @property
    def memory_bytes(self) -> int:
        return sum(tile.memory_bytes for tile in self.tiles.values())

    def _tile(self, tile_row: int, tile_col: int) -> TileAccumulator:
        key = (int(tile_row), int(tile_col))
        cached = self.tiles.get(key)
        if cached is not None:
            return cached
        local_row_start = key[0] * self.tile_size
        local_col_start = key[1] * self.tile_size
        tile_shape = (
            min(self.tile_size, self.shape[0] - local_row_start),
            min(self.tile_size, self.shape[1] - local_col_start),
        )
        arrays = _init_cumulative_arrays(tile_shape, self.n_observers)
        cached = TileAccumulator(
            row_start=self.row_offset + local_row_start,
            col_start=self.col_offset + local_col_start,
            visible_count=arrays[0],
            min_distance=arrays[1],
            max_distance=arrays[2],
            distance_sum=arrays[3],
            distance_weight_sum=arrays[4],
            observer_mask=arrays[5],
        )
        self.tiles[key] = cached
        return cached

    def accumulate(
        self,
        *,
        visible: np.ndarray,
        row_start: int,
        col_start: int,
        transform: Any,
        observer_x: float,
        observer_y: float,
        sample_index: int,
        distance_weight_config: Any,
        distance_weight_max_km: float,
        use_compiled: bool | None = None,
    ) -> None:
        """Split one observer window across touched tiles and release it promptly."""

        if visible.ndim != 2:
            raise ValueError("Observer visibility arrays must be two-dimensional.")
        row_end = int(row_start) + int(visible.shape[0])
        col_end = int(col_start) + int(visible.shape[1])
        accumulator_row_end = self.row_offset + self.shape[0]
        accumulator_col_end = self.col_offset + self.shape[1]
        if (
            row_start < self.row_offset
            or col_start < self.col_offset
            or row_end > accumulator_row_end
            or col_end > accumulator_col_end
        ):
            raise ValueError("Observer result exceeds the tiled accumulator window.")

        first_tile_row = (int(row_start) - self.row_offset) // self.tile_size
        last_tile_row = (row_end - 1 - self.row_offset) // self.tile_size
        first_tile_col = (int(col_start) - self.col_offset) // self.tile_size
        last_tile_col = (col_end - 1 - self.col_offset) // self.tile_size
        for tile_row in range(first_tile_row, last_tile_row + 1):
            tile_global_row = self.row_offset + tile_row * self.tile_size
            tile_global_row_end = min(tile_global_row + self.tile_size, accumulator_row_end)
            intersect_row_start = max(int(row_start), tile_global_row)
            intersect_row_end = min(row_end, tile_global_row_end)
            for tile_col in range(first_tile_col, last_tile_col + 1):
                tile_global_col = self.col_offset + tile_col * self.tile_size
                tile_global_col_end = min(tile_global_col + self.tile_size, accumulator_col_end)
                intersect_col_start = max(int(col_start), tile_global_col)
                intersect_col_end = min(col_end, tile_global_col_end)
                visible_slice = visible[
                    intersect_row_start - int(row_start) : intersect_row_end - int(row_start),
                    intersect_col_start - int(col_start) : intersect_col_end - int(col_start),
                ]
                if not np.any(visible_slice):
                    continue
                tile = self._tile(tile_row, tile_col)
                _accumulate_visible(
                    visible=visible_slice,
                    transform=transform,
                    observer_x=observer_x,
                    observer_y=observer_y,
                    sample_index=sample_index,
                    visible_count=tile.visible_count,
                    min_distance=tile.min_distance,
                    max_distance=tile.max_distance,
                    distance_sum=tile.distance_sum,
                    distance_weight_sum=tile.distance_weight_sum,
                    distance_weight_config=distance_weight_config,
                    distance_weight_max_km=distance_weight_max_km,
                    observer_mask=tile.observer_mask,
                    row_offset=intersect_row_start,
                    col_offset=intersect_col_start,
                    storage_row_offset=intersect_row_start - tile.row_start,
                    storage_col_offset=intersect_col_start - tile.col_start,
                    use_compiled=use_compiled,
                )

    def iter_tiles(self) -> Iterator[tuple[tuple[int, int], TileAccumulator]]:
        for key in sorted(self.tiles):
            yield key, self.tiles[key]

    def sparse_arrays(self, *, stride: int = 1) -> SparseAccumulatorArrays:
        """Finalize tiles independently and concatenate only visible pixels."""

        columns: dict[str, list[np.ndarray]] = {
            name: []
            for name in (
                "rows",
                "cols",
                "visible_count",
                "min_distance",
                "mean_distance",
                "max_distance",
                "distance_weight_sum",
                "observer_mask",
            )
        }
        has_observer_mask = self.n_observers <= 63
        for _, tile in self.iter_tiles():
            local_rows, local_cols = np.where(tile.visible_count > 0)
            if local_rows.size == 0:
                continue
            global_rows = local_rows + tile.row_start
            global_cols = local_cols + tile.col_start
            if int(stride) > 1:
                keep = (global_rows % int(stride) == 0) & (global_cols % int(stride) == 0)
                local_rows = local_rows[keep]
                local_cols = local_cols[keep]
                global_rows = global_rows[keep]
                global_cols = global_cols[keep]
            if local_rows.size == 0:
                continue
            minimum, mean = tile.finalized_distances()
            selected = (local_rows, local_cols)
            columns["rows"].append(global_rows.astype("int32", copy=False))
            columns["cols"].append(global_cols.astype("int32", copy=False))
            columns["visible_count"].append(tile.visible_count[selected])
            columns["min_distance"].append(minimum[selected])
            columns["mean_distance"].append(mean[selected])
            columns["max_distance"].append(tile.max_distance[selected])
            columns["distance_weight_sum"].append(tile.distance_weight_sum[selected])
            if has_observer_mask and tile.observer_mask is not None:
                columns["observer_mask"].append(tile.observer_mask[selected])

        def joined(name: str, dtype: str) -> np.ndarray:
            values = columns[name]
            return (
                np.concatenate(values).astype(dtype, copy=False)
                if values
                else np.empty(0, dtype=dtype)
            )

        return SparseAccumulatorArrays(
            rows=joined("rows", "int32"),
            cols=joined("cols", "int32"),
            visible_count=joined("visible_count", "uint16"),
            min_distance=joined("min_distance", "float32"),
            mean_distance=joined("mean_distance", "float32"),
            max_distance=joined("max_distance", "float32"),
            distance_weight_sum=joined("distance_weight_sum", "int64"),
            observer_mask=(joined("observer_mask", "uint64") if has_observer_mask else None),
        )

    def materialize(self, field: str) -> np.ndarray:
        """Materialize one optional diagnostic raster without restoring six dense arrays."""

        dtype_by_field = {
            "visible_count": "uint16",
            "min_distance": "float32",
            "mean_distance": "float32",
            "max_distance": "float32",
        }
        if field not in dtype_by_field:
            raise ValueError(f"Unsupported materialized accumulator field: {field}")
        output = np.zeros(self.shape, dtype=dtype_by_field[field])
        for _, tile in self.iter_tiles():
            local_row = tile.row_start - self.row_offset
            local_col = tile.col_start - self.col_offset
            destination = (
                slice(local_row, local_row + tile.shape[0]),
                slice(local_col, local_col + tile.shape[1]),
            )
            if field in {"min_distance", "mean_distance"}:
                minimum, mean = tile.finalized_distances()
                output[destination] = minimum if field == "min_distance" else mean
            else:
                output[destination] = getattr(tile, field)
        return output
