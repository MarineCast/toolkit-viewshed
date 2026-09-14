"""Canonical raster-aligned target-H3 codes for terrain aggregation."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import polars as pl
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.features import rasterize
from rasterio.windows import Window

from ...config import AppConfig, load_metadata_sidecar, write_metadata_sidecar
from ...prepare.area.geometry import (
    h3_geometry_artifact_path,
    h3_geometry_metadata,
    load_h3_geometry_lookup,
)

LOGGER = logging.getLogger(__name__)
CANONICAL_PIXEL_H3_INDEX_VERSION = "canonical_pixel_h3_index_v1"


@dataclass(frozen=True)
class WaterPixelH3Index:
    """Compact raster-aligned target-H3 membership for one batch window."""

    code_grid: np.ndarray
    h3_cells: tuple[str, ...]
    code_grid_path: Path | None = None
    lookup_path: Path | None = None

    @property
    def memory_bytes(self) -> int:
        return int(self.code_grid.nbytes) + sum(len(cell.encode("utf-8")) for cell in self.h3_cells)


@dataclass(frozen=True)
class CanonicalPixelH3Artifact:
    """Paths and lookup values for one immutable canonical code grid."""

    code_grid_path: Path
    lookup_path: Path
    h3_cells: tuple[str, ...]
    fingerprint: str


_ARTIFACT_CACHE: dict[tuple[Any, ...], CanonicalPixelH3Artifact] = {}


def _file_signature(path: Path) -> dict[str, str | int]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _raster_signature(path: Path) -> dict[str, Any]:
    signature: dict[str, Any] = _file_signature(path)
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"Canonical water mask has no CRS: {path}")
        signature["grid"] = {
            "crs": CRS.from_user_input(src.crs).to_string(),
            "shape": [int(src.height), int(src.width)],
            "transform": [float(value) for value in src.transform],
        }
    return signature


def _fingerprint(inputs: Mapping[str, Any]) -> str:
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_lookup(path: Path) -> tuple[str, ...]:
    frame = (
        pl.read_parquet(path)
        .select(
            pl.col("target_code").cast(pl.Int32),
            pl.col("target_h3").cast(pl.String),
        )
        .sort("target_code")
    )
    codes = frame.get_column("target_code").to_list()
    if codes != list(range(frame.height)):
        raise ValueError(f"Canonical pixel/H3 lookup has non-contiguous codes: {path}")
    cells = tuple(frame.get_column("target_h3").to_list())
    if len(cells) != len(set(cells)):
        raise ValueError(f"Canonical pixel/H3 lookup has duplicate target cells: {path}")
    return cells


def _artifact_is_reusable(
    code_grid_path: Path,
    lookup_path: Path,
    *,
    fingerprint: str,
    reference_path: Path,
) -> bool:
    metadata = load_metadata_sidecar(code_grid_path)
    if (
        not code_grid_path.exists()
        or not lookup_path.exists()
        or metadata is None
        or metadata.get("step") != CANONICAL_PIXEL_H3_INDEX_VERSION
        or metadata.get("fingerprint") != fingerprint
    ):
        return False
    try:
        cells = _load_lookup(lookup_path)
        with rasterio.open(reference_path) as reference, rasterio.open(code_grid_path) as codes:
            return bool(
                codes.count == 1
                and codes.dtypes[0] == "int32"
                and codes.nodata == -1
                and codes.shape == reference.shape
                and codes.transform.almost_equals(reference.transform)
                and CRS.from_user_input(codes.crs) == CRS.from_user_input(reference.crs)
                and int(metadata.get("target_count", -1)) == len(cells)
            )
    except (OSError, ValueError, TypeError, rasterio.errors.RasterioError):
        return False


def _build_code_grid(
    *,
    water_mask_path: Path,
    output_path: Path,
    target_cells: Sequence[str],
    geometry_by_cell: Mapping[str, Any],
    block_size: int,
) -> None:
    """Rasterize target codes one bounded tile at a time."""

    geometries = [geometry_by_cell[cell] for cell in target_cells]
    missing = [
        cell for cell, geometry in zip(target_cells, geometries, strict=True) if geometry is None
    ]
    if missing:
        raise ValueError(f"Missing projected H3 geometries for target cells: {missing[:10]}")
    bounds = np.asarray([geometry.bounds for geometry in geometries], dtype="float64")
    with rasterio.open(water_mask_path) as water:
        if water.crs is None:
            raise ValueError(f"Canonical water mask has no CRS: {water_mask_path}")
        tile_size = max(16, int(block_size))
        tile_size = max(16, (tile_size // 16) * 16)
        profile = water.profile.copy()
        profile.update(
            driver="GTiff",
            count=1,
            dtype="int32",
            nodata=-1,
            tiled=True,
            blockxsize=tile_size,
            blockysize=tile_size,
            compress="deflate",
            predictor=2,
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(output_path, "w", **profile) as destination:
            for _block_index, window in destination.block_windows(1):
                left, bottom, right, top = destination.window_bounds(window)
                candidate_indices = np.flatnonzero(
                    (bounds[:, 0] <= right)
                    & (bounds[:, 2] >= left)
                    & (bounds[:, 1] <= top)
                    & (bounds[:, 3] >= bottom)
                )
                shapes = [
                    (geometries[int(index)], int(index))
                    for index in candidate_indices
                    if not geometries[int(index)].is_empty
                ]
                shape = (int(window.height), int(window.width))
                if shapes:
                    codes = rasterize(
                        shapes,
                        out_shape=shape,
                        transform=destination.window_transform(window),
                        fill=-1,
                        all_touched=False,
                        dtype="int32",
                    )
                else:
                    codes = np.full(shape, -1, dtype="int32")
                water_values = water.read(1, window=window)
                codes[water_values != 1] = -1
                destination.write(codes, 1, window=window)


def ensure_canonical_pixel_h3_artifact(
    app: AppConfig,
    canonical_water_mask_path: Path,
) -> CanonicalPixelH3Artifact:
    """Create or reuse one target-code raster for the canonical water grid."""

    resolution = int(app.h3.output_resolution)
    lookup_path = (
        Path(app.paths.output_dir) / "lookup" / f"SOURCE_TARGET_LOOKUP_H3R{resolution}.parquet"
    )
    geometry_path = h3_geometry_artifact_path(app.paths.output_dir, resolution)
    if not lookup_path.exists():
        raise FileNotFoundError(f"Missing source-target lookup: {lookup_path}")
    if not geometry_path.exists():
        raise FileNotFoundError(f"Missing canonical H3 geometry artifact: {geometry_path}")
    geometry_metadata = h3_geometry_metadata(geometry_path)
    geometry_crs = geometry_metadata.get("geometry_projected_crs")
    with rasterio.open(canonical_water_mask_path) as water:
        if CRS.from_user_input(geometry_crs) != CRS.from_user_input(water.crs):
            raise ValueError(
                "Canonical H3 geometry and water-mask projections differ: "
                f"geometry={geometry_crs} water_mask={water.crs}"
            )

    inputs = {
        "algorithm": CANONICAL_PIXEL_H3_INDEX_VERSION,
        "h3_resolution": resolution,
        "canonical_water_mask": _raster_signature(canonical_water_mask_path),
        "source_target_lookup": _file_signature(lookup_path),
        "h3_geometry": _file_signature(geometry_path),
        "rasterization": "pixel_center_all_touched_false",
    }
    fingerprint = _fingerprint(inputs)
    cache_key = (fingerprint, int(app.raster.block_size))
    cached = _ARTIFACT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    directory = Path(canonical_water_mask_path).parent / "pixel_h3_index"
    stem = f"canonical_target_h3_codes_H3R{resolution}_{fingerprint[:16]}"
    code_grid_path = directory / f"{stem}.tif"
    target_lookup_path = directory / f"{stem}_lookup.parquet"
    if _artifact_is_reusable(
        code_grid_path,
        target_lookup_path,
        fingerprint=fingerprint,
        reference_path=canonical_water_mask_path,
    ):
        artifact = CanonicalPixelH3Artifact(
            code_grid_path=code_grid_path,
            lookup_path=target_lookup_path,
            h3_cells=_load_lookup(target_lookup_path),
            fingerprint=fingerprint,
        )
        _ARTIFACT_CACHE[cache_key] = artifact
        LOGGER.info("canonical_pixel_h3_index cache_hit grid=%s", code_grid_path)
        return artifact

    target_cells = tuple(
        pl.scan_parquet(str(lookup_path))
        .select(pl.col("target_h3").cast(pl.String))
        .unique()
        .sort("target_h3")
        .collect(engine="streaming")
        .get_column("target_h3")
        .to_list()
    )
    geometry_by_cell = load_h3_geometry_lookup(geometry_path, "geometry_projected")
    missing = sorted(set(target_cells) - set(geometry_by_cell))
    if missing:
        raise ValueError(
            f"Canonical H3 geometry artifact is missing {len(missing)} targets: {missing[:10]}"
        )

    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary_grid = directory / f".{stem}.{token}.tmp.tif"
    temporary_lookup = directory / f".{stem}.{token}.tmp.parquet"
    try:
        LOGGER.info(
            "canonical_pixel_h3_index build_start targets=%d water_mask=%s output=%s",
            len(target_cells),
            canonical_water_mask_path,
            code_grid_path,
        )
        _build_code_grid(
            water_mask_path=canonical_water_mask_path,
            output_path=temporary_grid,
            target_cells=target_cells,
            geometry_by_cell=geometry_by_cell,
            block_size=app.raster.block_size,
        )
        pl.DataFrame(
            {
                "target_code": pl.Series(range(len(target_cells)), dtype=pl.Int32),
                "target_h3": pl.Series(target_cells, dtype=pl.String),
            }
        ).write_parquet(temporary_lookup)
        temporary_grid.replace(code_grid_path)
        temporary_lookup.replace(target_lookup_path)
        write_metadata_sidecar(
            code_grid_path,
            app.raw_config,
            {
                "step": CANONICAL_PIXEL_H3_INDEX_VERSION,
                "fingerprint": fingerprint,
                "inputs": inputs,
                "lookup_path": str(target_lookup_path.resolve()),
                "target_count": len(target_cells),
                "background_code": -1,
            },
        )
    finally:
        temporary_grid.unlink(missing_ok=True)
        temporary_lookup.unlink(missing_ok=True)

    artifact = CanonicalPixelH3Artifact(
        code_grid_path=code_grid_path,
        lookup_path=target_lookup_path,
        h3_cells=target_cells,
        fingerprint=fingerprint,
    )
    _ARTIFACT_CACHE[cache_key] = artifact
    LOGGER.info(
        "canonical_pixel_h3_index cache_store targets=%d size_mb=%.2f grid=%s",
        len(target_cells),
        code_grid_path.stat().st_size / (1024.0 * 1024.0),
        code_grid_path,
    )
    return artifact


def _aligned_window(
    canonical_transform: Affine,
    batch_transform: Affine,
    batch_shape: tuple[int, int],
) -> Window:
    if not (
        np.isclose(canonical_transform.a, batch_transform.a, atol=1e-9)
        and np.isclose(canonical_transform.b, batch_transform.b, atol=1e-9)
        and np.isclose(canonical_transform.d, batch_transform.d, atol=1e-9)
        and np.isclose(canonical_transform.e, batch_transform.e, atol=1e-9)
    ):
        raise ValueError("Batch and canonical pixel/H3 grids have different pixel geometry.")
    inverse = ~canonical_transform
    col_offset_float, row_offset_float = inverse * (batch_transform.c, batch_transform.f)
    col_offset = int(round(col_offset_float))
    row_offset = int(round(row_offset_float))
    if not (
        np.isclose(col_offset_float, col_offset, atol=1e-7)
        and np.isclose(row_offset_float, row_offset, atol=1e-7)
    ):
        raise ValueError(
            "Batch water grid is not pixel-aligned to the canonical H3 index: "
            f"row_offset={row_offset_float} col_offset={col_offset_float}"
        )
    expected = canonical_transform * Affine.translation(col_offset, row_offset)
    if not expected.almost_equals(batch_transform, precision=1e-7):
        raise ValueError("Batch transform does not match its canonical index window.")
    return Window(
        col_off=col_offset,
        row_off=row_offset,
        width=int(batch_shape[1]),
        height=int(batch_shape[0]),
    )


def canonical_pixel_h3_window(
    app: AppConfig,
    canonical_water_mask_path: Path,
    *,
    batch_transform: Affine,
    batch_crs: Any,
    batch_water_mask: np.ndarray,
) -> WaterPixelH3Index:
    """Load one aligned batch window from the canonical target-code raster."""

    artifact = ensure_canonical_pixel_h3_artifact(app, canonical_water_mask_path)
    mask = np.asarray(batch_water_mask, dtype=bool)
    with rasterio.open(artifact.code_grid_path) as source:
        if CRS.from_user_input(source.crs) != CRS.from_user_input(batch_crs):
            raise ValueError(
                "Batch CRS differs from the canonical pixel/H3 index: "
                f"batch={batch_crs} canonical={source.crs}"
            )
        window = _aligned_window(source.transform, batch_transform, mask.shape)
        if (
            window.col_off < 0
            or window.row_off < 0
            or window.col_off + window.width > source.width
            or window.row_off + window.height > source.height
        ):
            raise ValueError("Batch pixel/H3 window falls outside the canonical grid.")
        codes = source.read(1, window=window)
    if codes.shape != mask.shape:
        raise ValueError(
            f"Canonical pixel/H3 window shape mismatch: codes={codes.shape} mask={mask.shape}"
        )
    codes[~mask] = -1
    return WaterPixelH3Index(
        code_grid=codes,
        h3_cells=artifact.h3_cells,
        code_grid_path=artifact.code_grid_path,
        lookup_path=artifact.lookup_path,
    )


def clear_canonical_pixel_h3_cache() -> None:
    """Release process-local artifact metadata without deleting the grid."""

    _ARTIFACT_CACHE.clear()
