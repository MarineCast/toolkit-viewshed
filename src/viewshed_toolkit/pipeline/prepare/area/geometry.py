"""Persist and reuse canonical H3 geometry for viewshed stages."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import polars as pl
from pyproj import CRS
from shapely import from_wkb, to_wkb
from shapely.geometry.base import BaseGeometry

from viewshed_toolkit._internal.geo.geometry import CRS_WGS84
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon, get_resolution
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ...config import load_metadata_sidecar, write_metadata_sidecar
from ...config.distance import DistanceRuntime
from . import domains

LOGGER = logging.getLogger(__name__)

H3_GEOMETRY_SCHEMA: tuple[str, ...] = (
    "h3_cell",
    "geometry_wgs84",
    "geometry_projected",
    "water_geometry_projected",
    "water_area_m2",
)
H3_GEOMETRY_ALGORITHM_VERSION = "h3_geometry_v1"

_FRAME_CACHE: dict[tuple[str, int, int], pl.DataFrame] = {}
_LOOKUP_CACHE: dict[tuple[str, int, int, str], dict[str, BaseGeometry]] = {}


def h3_geometry_artifact_path(output_dir: Path, h3_resolution: int) -> Path:
    """Return the canonical geometry artifact path for an H3 resolution."""

    return Path(output_dir) / "lookup" / f"H3_GEOMETRY_H3R{int(h3_resolution)}.parquet"


def _geometry_signature(geometry: BaseGeometry) -> str:
    return hashlib.sha256(bytes(to_wkb(geometry, hex=False))).hexdigest()


def _cell_signature(cells: list[str]) -> str:
    digest = hashlib.sha256()
    for cell in cells:
        digest.update(cell.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _artifact_inputs(
    runtime: DistanceRuntime,
    *,
    cells: list[str],
    water_domain: BaseGeometry,
    projected_crs: str,
    equal_area_crs: str,
) -> dict[str, object]:
    return {
        "algorithm": H3_GEOMETRY_ALGORITHM_VERSION,
        "h3_resolution": int(runtime.source_resolution),
        "cells_sha256": _cell_signature(cells),
        "cell_count": len(cells),
        "water_geometry_sha256": _geometry_signature(water_domain),
        "projected_crs": CRS.from_user_input(projected_crs).to_string(),
        "water_area_crs": CRS.from_user_input(equal_area_crs).to_string(),
    }


def _validate_frame(frame: pl.DataFrame, path: Path) -> None:
    if tuple(frame.columns) != H3_GEOMETRY_SCHEMA:
        raise ValueError(
            f"H3 geometry artifact has invalid schema at {path}: {tuple(frame.columns)}"
        )
    if (
        frame.get_column("h3_cell").null_count()
        or frame.get_column("h3_cell").n_unique() != frame.height
    ):
        raise ValueError(f"H3 geometry artifact has null or duplicate cell IDs: {path}")
    if frame.get_column("water_area_m2").is_null().any():
        raise ValueError(f"H3 geometry artifact has null water areas: {path}")


def ensure_h3_geometry_artifact(
    runtime: DistanceRuntime,
    cells: Iterable[str],
    *,
    projected_crs: str | None = None,
    equal_area_crs: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Build the one canonical H3 geometry table used by all viewshed stages."""

    cell_ids = sorted({str(cell) for cell in cells})
    if not cell_ids:
        raise ValueError("Cannot build an H3 geometry artifact without cells.")
    if any(get_resolution(cell) != int(runtime.source_resolution) for cell in cell_ids):
        raise ValueError("H3 geometry cells do not match the configured source resolution.")

    projected = str(projected_crs or runtime.projected_crs)
    area_crs = str(
        equal_area_crs
        or (runtime.raw_config.get("water_viewing", {}) or {}).get(
            "target_area_equal_area_crs", "EPSG:6933"
        )
    )
    # Geometry is shared by source and target stages, so its water intersection
    # must cover the complete buffered target domain rather than the smaller
    # observer-source bbox.
    domain_geometries = domains.load_land_water_domains(runtime, extent="target")
    inputs = _artifact_inputs(
        runtime,
        cells=cell_ids,
        water_domain=domain_geometries.water_domain,
        projected_crs=projected,
        equal_area_crs=area_crs,
    )
    path = h3_geometry_artifact_path(runtime.output_dir, runtime.source_resolution)
    metadata = load_metadata_sidecar(path)
    if not overwrite and path.exists() and metadata is not None:
        existing_inputs = metadata.get("inputs")
        stable_keys = {
            "algorithm",
            "h3_resolution",
            "water_geometry_sha256",
            "projected_crs",
            "water_area_crs",
        }
        if isinstance(existing_inputs, dict) and all(
            existing_inputs.get(key) == inputs.get(key) for key in stable_keys
        ):
            existing = load_h3_geometry_frame(path)
            existing_cells = set(existing.get_column("h3_cell").to_list())
            if set(cell_ids).issubset(existing_cells):
                return path
            cell_ids = sorted(existing_cells.union(cell_ids))
            inputs = _artifact_inputs(
                runtime,
                cells=cell_ids,
                water_domain=domain_geometries.water_domain,
                projected_crs=projected,
                equal_area_crs=area_crs,
            )

    geometries_wgs84 = gpd.GeoSeries([cell_to_polygon(cell) for cell in cell_ids], crs=CRS_WGS84)
    geometries_projected = geometries_wgs84.to_crs(projected)
    water_wgs84 = geometries_wgs84.intersection(domain_geometries.water_domain)
    water_projected = gpd.GeoSeries(water_wgs84, crs=CRS_WGS84).to_crs(projected)
    water_equal_area = gpd.GeoSeries(water_wgs84, crs=CRS_WGS84).to_crs(area_crs)

    frame = pl.DataFrame(
        {
            "h3_cell": cell_ids,
            "geometry_wgs84": [bytes(value) for value in to_wkb(geometries_wgs84.array)],
            "geometry_projected": [bytes(value) for value in to_wkb(geometries_projected.array)],
            "water_geometry_projected": [bytes(value) for value in to_wkb(water_projected.array)],
            "water_area_m2": np.maximum(0.0, water_equal_area.area.to_numpy(dtype="float64")),
        }
    ).select(H3_GEOMETRY_SCHEMA)
    _validate_frame(frame, path)
    atomic_sink_parquet(frame.lazy(), path, overwrite=True)
    write_metadata_sidecar(
        path,
        runtime.raw_config,
        {
            "step": H3_GEOMETRY_ALGORITHM_VERSION,
            "inputs": inputs,
            "geometry_encoding": "WKB",
            "geometry_wgs84_crs": CRS_WGS84,
            "geometry_projected_crs": CRS.from_user_input(projected).to_string(),
            "water_geometry_projected_crs": CRS.from_user_input(projected).to_string(),
            "water_area_crs": CRS.from_user_input(area_crs).to_string(),
        },
    )
    clear_h3_geometry_memory_cache()
    LOGGER.info("h3_geometry cache_store rows=%d output=%s", frame.height, path)
    return path


def load_h3_geometry_frame(path: Path) -> pl.DataFrame:
    """Load the artifact once per process and return its immutable frame."""

    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), int(stat.st_mtime_ns), int(stat.st_size))
    cached = _FRAME_CACHE.get(key)
    if cached is None:
        frame = pl.read_parquet(resolved)
        _validate_frame(frame, resolved)
        _FRAME_CACHE.clear()
        _LOOKUP_CACHE.clear()
        _FRAME_CACHE[key] = frame
        cached = frame
    return cached


def load_h3_geometry_lookup(path: Path, column: str) -> dict[str, BaseGeometry]:
    """Decode one geometry column once per process into a cell-keyed lookup."""

    if column not in H3_GEOMETRY_SCHEMA[1:4]:
        raise ValueError(f"Unsupported H3 geometry column: {column}")
    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), int(stat.st_mtime_ns), int(stat.st_size), column)
    cached = _LOOKUP_CACHE.get(key)
    if cached is None:
        frame = load_h3_geometry_frame(resolved)
        values = from_wkb(frame.get_column(column).to_numpy())
        cached = dict(zip(frame.get_column("h3_cell").to_list(), values, strict=True))
        _LOOKUP_CACHE[key] = cached
    return cached


def h3_geometry_metadata(path: Path) -> dict[str, object]:
    metadata = load_metadata_sidecar(path)
    if metadata is None or metadata.get("step") != H3_GEOMETRY_ALGORITHM_VERSION:
        raise ValueError(f"Missing or invalid H3 geometry metadata: {path}")
    return metadata


def clear_h3_geometry_memory_cache() -> None:
    """Release process-local decoded geometry; persisted artifacts remain."""

    _FRAME_CACHE.clear()
    _LOOKUP_CACHE.clear()
