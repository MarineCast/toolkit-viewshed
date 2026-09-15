"""Canonical DEM, water-mask, endpoint, and canopy raster stack construction."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from shapely.geometry import box

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file
from viewshed_toolkit._internal.geo import raster as core_raster
from viewshed_toolkit._internal.geo.geometry import (
    safe_polygonal_difference,
    safe_polygonal_intersection,
    safe_polygonal_union,
)
from viewshed_toolkit._internal.geo.raster import validate_raster_grid_alignment

from ...config import AppConfig, ViewshedConfig, viewshed_config_from_app_config
from ..elevation.canopy import (
    CANONICAL_CANOPY_BASE_ALGORITHM_VERSION,
    CANOPY_ALIGNMENT_ALGORITHM_VERSION,
    align_canopy_height_to_endpoint_dem,
    build_canonical_canopy_base_surface,
)
from ..elevation.terrain import (
    canonical_endpoint_repair,
    ensure_projected_regional_dem,
    flatten_water_pixels_to_sea_level,
    rasterize_water_to_match_dem,
)

CRS_WGS84 = "EPSG:4326"
LOGGER = logging.getLogger(__name__)
CANONICAL_RASTER_STACK_ALGORITHM_VERSION = "viewshed_canonical_raster_stack_v2"
_CANONICAL_RASTER_STACK_READY_THIS_PROCESS: dict[str, "CanonicalRasterStack"] = {}


@dataclass(frozen=True)
class CanonicalRasterStack:
    """Immutable domain-aligned raster inputs shared by terrain batches."""

    projected_dem_path: Path
    water_mask_path: Path
    endpoint_dem_path: Path
    aligned_canopy_path: Path | None
    base_canopy_surface_path: Path | None
    core_metadata_path: Path
    canopy_metadata_path: Path | None
    core_fingerprint: str
    canopy_fingerprint: str | None


@lru_cache(maxsize=4)
def _load_water_layer_identity(path_str: str, identity: str) -> gpd.GeoDataFrame:
    water = gpd.read_parquet(path_str)
    if water.crs is None:
        raise ValueError("Water polygon has no CRS metadata.")
    if _geometry_identity(path_str) != identity:
        raise ValueError("Water geometry changed while being read")
    return water.to_crs(CRS_WGS84)


@lru_cache(maxsize=4)
def _load_land_identity(path_str: str, identity: str) -> gpd.GeoDataFrame:
    land_path = Path(path_str)
    land = gpd.read_file(land_path)
    if land.crs is None:
        raise ValueError(f"Natural Earth land layer has no CRS metadata: {land_path}")
    if _geometry_identity(path_str) != identity:
        raise ValueError("Land geometry changed while being read")
    return land.to_crs(CRS_WGS84)


def _geometry_identity(path_str: str) -> str:
    return _canonical_contract_fingerprint(_canonical_dataset_signature(Path(path_str)))


def _load_water_layer_cached(path_str: str) -> gpd.GeoDataFrame:
    return _load_water_layer_identity(path_str, _geometry_identity(path_str)).copy()


def _load_land_cached(path_str: str) -> gpd.GeoDataFrame:
    return _load_land_identity(path_str, _geometry_identity(path_str)).copy()


def _filled_water_domain(
    water: gpd.GeoDataFrame,
    aoi_wgs84: gpd.GeoDataFrame,
    land_path: Path,
) -> gpd.GeoDataFrame:
    aoi_geom = safe_polygonal_union(aoi_wgs84)
    if aoi_geom.is_empty:
        return gpd.GeoDataFrame(columns=["source"], geometry=[], crs=CRS_WGS84)

    try:
        water_union = safe_polygonal_union(water, clip_geometry=aoi_geom)
    except ValueError as exc:
        if "No polygonal geometry remains" not in str(exc):
            raise
        water_union = None

    land = _load_land_cached(str(Path(land_path).resolve()))
    try:
        land_union = safe_polygonal_union(land, clip_geometry=aoi_geom)
    except ValueError as exc:
        if "No polygonal geometry remains" not in str(exc):
            raise
        land_union = None

    if water_union is not None and not water_union.is_empty:
        natural_earth_water = (
            safe_polygonal_difference(
                aoi_geom,
                land_union,
                label="batch AOI minus Natural Earth land",
            )
            if land_union is not None and not land_union.is_empty
            else aoi_geom
        )
        offshore_water = safe_polygonal_difference(
            natural_earth_water,
            water_union,
            label="batch open water minus canonical water",
        )
        boundary_zone = aoi_geom.boundary.buffer(1e-9)
        water_parts = [water_union]
        if not offshore_water.is_empty:
            for geom in getattr(offshore_water, "geoms", [offshore_water]):
                if not geom.is_empty and geom.intersects(boundary_zone):
                    water_parts.append(geom)
        water_parts_union = safe_polygonal_union(gpd.GeoSeries(water_parts, crs=CRS_WGS84))
        filled_water = safe_polygonal_intersection(
            water_parts_union,
            aoi_geom,
            label="batch filled water clipped to AOI",
        )
    else:
        filled_water = (
            safe_polygonal_difference(
                aoi_geom,
                land_union,
                label="batch fallback water as AOI minus land",
            )
            if land_union is not None and not land_union.is_empty
            else aoi_geom
        )

    if filled_water.is_empty:
        return gpd.GeoDataFrame(columns=["source"], geometry=[], crs=CRS_WGS84)
    return gpd.GeoDataFrame(
        {"source": ["high_res_water_plus_edge_open_water"]},
        geometry=[filled_water],
        crs=CRS_WGS84,
    )


def load_and_clip_water(
    config: ViewshedConfig,
    aoi_wgs84: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    water = _load_water_layer_cached(str(config.water_polygon_path))
    clipped = _filled_water_domain(water, aoi_wgs84, config.land_polygon_path)
    return clipped, clipped.to_crs(config.crs_projected)


def _canonical_dataset_signature(path: Path) -> dict[str, Any]:
    """Return a content-addressed signature for a raster or vector dataset."""

    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Canonical raster input does not exist: {resolved}")
    members = (
        sorted(resolved.parent.glob(f"{resolved.stem}.*"))
        if resolved.suffix.lower() == ".shp"
        else [resolved]
    )
    return {
        "path": str(resolved),
        "members": [
            {
                "path": str(member.resolve()),
                "size": int(member.stat().st_size),
                "sha256": checksum_unchanged_file(member),
            }
            for member in members
            if member.is_file()
        ],
    }


@lru_cache(maxsize=32)
def _checksum_for_unchanged_member(path_str: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    return checksum_path(path_str)


def _cached_member_checksum(path: Path) -> str:
    stat = path.stat()
    return _checksum_for_unchanged_member(
        str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns)
    )


def _canonical_contract_fingerprint(contract: dict[str, Any]) -> str:
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_raster_signature(path: Path) -> dict[str, Any]:
    signature = _canonical_dataset_signature(path)
    with rasterio.open(path) as src:
        signature["grid"] = {
            "crs": str(src.crs),
            "width": int(src.width),
            "height": int(src.height),
            "transform": [float(value) for value in tuple(src.transform)],
            "dtype": str(src.dtypes[0]),
            "nodata": (
                None
                if src.nodata is None
                else "nan" if np.isnan(float(src.nodata)) else float(src.nodata)
            ),
        }
        signature["tags"] = dict(src.tags())
    return signature


def _canonical_metadata_matches(
    metadata_path: Path,
    *,
    fingerprint: str,
    output_paths: dict[str, Path],
    reference_path: Path,
) -> bool:
    if not metadata_path.exists():
        return False
    try:
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("fingerprint") != fingerprint:
            return False
        expected_outputs = metadata.get("outputs")
        if not isinstance(expected_outputs, dict):
            return False
        for name, path in output_paths.items():
            if not path.exists() or not core_raster.valid_raster(path):
                return False
            validate_raster_grid_alignment(
                path,
                reference_path,
                label_a=f"canonical_{name}",
                label_b="canonical_projected_dem",
            )
            if expected_outputs.get(name) != _canonical_raster_signature(path):
                return False
    except (OSError, ValueError, TypeError, rasterio.errors.RasterioError):
        return False
    return True


def _write_canonical_metadata(
    metadata_path: Path,
    *,
    fingerprint: str,
    contract: dict[str, Any],
    output_paths: dict[str, Path],
) -> None:
    payload = {
        "algorithm_version": CANONICAL_RASTER_STACK_ALGORITHM_VERSION,
        "fingerprint": fingerprint,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "contract": contract,
        "outputs": {name: _canonical_raster_signature(path) for name, path in output_paths.items()},
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        temporary.replace(metadata_path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_water_source_contract(app: AppConfig) -> dict[str, Any]:
    return {
        "water_polygon": _canonical_dataset_signature(app.paths.water_polygon_path),
        "natural_earth_land": _canonical_dataset_signature(app.paths.land_polygon_path),
        "water_fill_algorithm": "high_res_water_plus_edge_open_water_v1",
        "water_mask_algorithm": "viewshed_water_mask_v2",
        "water_mask_all_touched": True,
    }


def ensure_canonical_raster_stack(
    app: AppConfig,
    *,
    include_canopy: bool,
) -> CanonicalRasterStack:
    """Create or reuse the immutable domain-aligned terrain raster stack."""

    config = viewshed_config_from_app_config(app)
    projected_dem = ensure_projected_regional_dem(app)
    core_contract = {
        "algorithm_version": CANONICAL_RASTER_STACK_ALGORITHM_VERSION,
        "projected_dem": _canonical_raster_signature(projected_dem),
        "water_sources": _canonical_water_source_contract(app),
        "projected_crs": str(config.crs_projected),
        "dem_resolution_m": float(config.dem_resolution_m),
        "sea_level_m": float(config.sea_level_m),
        "endpoint_algorithm": "viewshed_water_flattened_endpoint_v3",
    }
    core_fingerprint = _canonical_contract_fingerprint(core_contract)
    cache_root = projected_dem.parent / "canonical_domain_rasters" / core_fingerprint[:20]
    water_mask_path = cache_root / "canonical_water_mask.tif"
    endpoint_dem_path = cache_root / "canonical_water_flattened_endpoint_dem.tif"
    core_metadata_path = cache_root / "core_metadata.json"
    core_outputs = {
        "water_mask": water_mask_path,
        "endpoint_dem": endpoint_dem_path,
    }

    canopy_contract: dict[str, Any] | None = None
    canopy_fingerprint: str | None = None
    aligned_canopy_path: Path | None = None
    base_canopy_surface_path: Path | None = None
    canopy_metadata_path: Path | None = None
    if include_canopy:
        canopy_contract = {
            "algorithm_version": CANONICAL_RASTER_STACK_ALGORITHM_VERSION,
            "core_fingerprint": core_fingerprint,
            "canopy_height": _canonical_dataset_signature(config.canopy_height_path),
            "alignment_algorithm": CANOPY_ALIGNMENT_ALGORITHM_VERSION,
            "canopy_resampling": str(config.canopy_resampling),
            "base_surface_algorithm": CANONICAL_CANOPY_BASE_ALGORITHM_VERSION,
            "minimum_canopy_height_m": max(
                0.0,
                float(config.minimum_canopy_height_m),
            ),
            "missing_canopy_base_value_m": 0.0,
            "nodata_policy_validation": "deferred_to_batch",
        }
        canopy_fingerprint = _canonical_contract_fingerprint(canopy_contract)
        canopy_root = cache_root / "canopy" / canopy_fingerprint[:20]
        aligned_canopy_path = canopy_root / "canonical_chm_aligned.tif"
        base_canopy_surface_path = canopy_root / "canonical_dem_plus_chm_base_surface.tif"
        canopy_metadata_path = canopy_root / "canopy_metadata.json"

    ready_key = f"{core_fingerprint}:{canopy_fingerprint or 'bare'}"
    cached = _CANONICAL_RASTER_STACK_READY_THIS_PROCESS.get(ready_key)
    if cached is not None:
        return cached

    core_valid = _canonical_metadata_matches(
        core_metadata_path,
        fingerprint=core_fingerprint,
        output_paths=core_outputs,
        reference_path=projected_dem,
    )
    if not core_valid:
        with rasterio.open(projected_dem) as projected_source:
            if projected_source.crs is None:
                raise ValueError(f"Projected DEM has no CRS: {projected_dem}")
            domain_projected = gpd.GeoDataFrame(
                {"name": ["canonical_projected_dem_domain"]},
                geometry=[box(*projected_source.bounds)],
                crs=projected_source.crs,
            )
        domain_wgs84 = domain_projected.to_crs(CRS_WGS84)
        _, water_projected = load_and_clip_water(config, domain_wgs84)
        cache_root.mkdir(parents=True, exist_ok=True)
        rasterize_water_to_match_dem(
            water_projected,
            projected_dem,
            water_mask_path,
            overwrite=True,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
        flatten_water_pixels_to_sea_level(
            projected_dem,
            water_mask_path,
            endpoint_dem_path,
            sea_level_m=config.sea_level_m,
            overwrite=True,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
        canonical_endpoint_repair(endpoint_dem_path, app.paths.regional_dem_path)
        if _canonical_water_source_contract(app) != core_contract["water_sources"]:
            raise ValueError("Canonical source geometry changed during raster preparation")
        _write_canonical_metadata(
            core_metadata_path,
            fingerprint=core_fingerprint,
            contract=core_contract,
            output_paths=core_outputs,
        )
        LOGGER.info(
            "Built canonical terrain raster core cache=%s fingerprint=%s",
            cache_root,
            core_fingerprint,
        )
    else:
        LOGGER.info(
            "Reusing canonical terrain raster core cache=%s fingerprint=%s",
            cache_root,
            core_fingerprint,
        )

    if include_canopy:
        assert canopy_contract is not None
        assert canopy_fingerprint is not None
        assert aligned_canopy_path is not None
        assert base_canopy_surface_path is not None
        assert canopy_metadata_path is not None
        canopy_outputs = {
            "aligned_canopy": aligned_canopy_path,
            "base_canopy_surface": base_canopy_surface_path,
        }
        canopy_valid = _canonical_metadata_matches(
            canopy_metadata_path,
            fingerprint=canopy_fingerprint,
            output_paths=canopy_outputs,
            reference_path=endpoint_dem_path,
        )
        if not canopy_valid:
            align_canopy_height_to_endpoint_dem(
                config.canopy_height_path,
                endpoint_dem_path,
                aligned_canopy_path,
                resampling=config.canopy_resampling,
                overwrite=True,
                compress=app.raster.intermediate_compress,
            )
            build_canonical_canopy_base_surface(
                endpoint_dem_path=endpoint_dem_path,
                water_mask_path=water_mask_path,
                aligned_canopy_path=aligned_canopy_path,
                output_surface_path=base_canopy_surface_path,
                minimum_canopy_height_m=config.minimum_canopy_height_m,
                overwrite=True,
                compress=app.raster.intermediate_compress,
            )
            _write_canonical_metadata(
                canopy_metadata_path,
                fingerprint=canopy_fingerprint,
                contract=canopy_contract,
                output_paths=canopy_outputs,
            )
            LOGGER.info(
                "Built canonical canopy raster cache=%s fingerprint=%s",
                canopy_metadata_path.parent,
                canopy_fingerprint,
            )
        else:
            LOGGER.info(
                "Reusing canonical canopy raster cache=%s fingerprint=%s",
                canopy_metadata_path.parent,
                canopy_fingerprint,
            )

    stack = CanonicalRasterStack(
        projected_dem_path=projected_dem,
        water_mask_path=water_mask_path,
        endpoint_dem_path=endpoint_dem_path,
        aligned_canopy_path=aligned_canopy_path,
        base_canopy_surface_path=base_canopy_surface_path,
        core_metadata_path=core_metadata_path,
        canopy_metadata_path=canopy_metadata_path,
        core_fingerprint=core_fingerprint,
        canopy_fingerprint=canopy_fingerprint,
    )
    _CANONICAL_RASTER_STACK_READY_THIS_PROCESS[ready_key] = stack
    return stack
