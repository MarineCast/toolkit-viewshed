"""Terrain preparation utilities for the viewshed pipeline.

Scientific and operational role
-------------------------------
This module supports the viewshed scientific stages without owning their core
model equations. It provides common configuration dataclasses, raster helpers,
H3 geometry helpers, batching logic, water-domain preparation, input validation,
Parquet utilities, and final lookup materialization.

The central scientific contract supported here is:

    viewability_weight =
        weight_terrain * weight_vegetation

The terrain kernel integrates observer-to-pixel distance decay. The centroid
distance artifact is retained as a diagnostic; final composition multiplies
the terrain kernel only by conditional vegetation attenuation.

Current compact factor-table schemas are:

    terrain:    source_h3, target_h3, weight_terrain
    distance:   source_h3, target_h3, distance_km, weight_distance
    vegetation: source_h3, target_h3, weight_vegetation

Major responsibilities
----------------------
- Load the viewshed YAML config into typed runtime dataclasses.
- Prepare projected DEM and batch-level water masks for terrain viewsheds.
- Generate source-cell sample points and source-cell batches.
- Validate required DEM, water, and land/source H3 inputs.
- Stream/merge Parquet partition outputs without eager full Pandas reads where
  possible.
- Compose final terrain, distance, vegetation, and viewability lookup tables.
- Optionally clean intermediate partitions after safe finalization.

Boundary of responsibility
--------------------------
Do not add terrain line-of-sight math, distance-decay models, or vegetation
attenuation science here. Those belong in `weights.terrain`, `weights.distance`,
and `weights.vegetation`. This module should remain shared plumbing plus the
explicit final composition step.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.geo import raster as core_raster

from ...config import (
    current_process_memory_mb,
    timer,
)
from .cache import cache_fingerprint as _cache_fingerprint
from .cache import input_signature as _input_signature
from .cache import raster_cache_matches as _raster_cache_matches

GEOTIFF_BLOCK_SIZE = 256
LOGGER = logging.getLogger(__name__)
_PROJECTED_DEM_READY_THIS_PROCESS: set[Path] = set()


# -----------------------------------------------------------------------------
# Config dataclasses
# -----------------------------------------------------------------------------


from ...config import AppConfig, viewshed_config_from_app_config


def _geometry_signature(frame: gpd.GeoDataFrame, *, target_crs: Any) -> str:
    """Hash rasterization geometry independent of feature ordering."""

    projected = frame.to_crs(target_crs)
    geometries = sorted(
        geom.wkb_hex for geom in projected.geometry if geom is not None and not geom.is_empty
    )
    return _cache_fingerprint(
        {
            "crs": str(target_crs),
            "geometry_wkb_hex": geometries,
        }
    )


def projected_dem_cache_path(app: AppConfig) -> Path:
    return app.paths.projected_dem_path


def projected_dem_metadata_path(projected_dem_path: Path) -> Path:
    return projected_dem_path.with_suffix(projected_dem_path.suffix + ".metadata.json")


def expected_projected_dem_metadata(app: AppConfig) -> dict[str, Any]:
    bbox = app.raw_config.get("region", {}).get("bbox_wgs84", {})
    source_metadata_path = app.paths.regional_dem_path.with_suffix(
        app.paths.regional_dem_path.suffix + ".source.json"
    )
    return {
        "step": "projected_dem_cache",
        "run_version": app.run.version,
        "config_hash": app.config_hash,
        "raw_dem_path": str(app.paths.regional_dem_path.resolve()),
        "raw_dem_signature": _input_signature(app.paths.regional_dem_path),
        "raw_dem_source_metadata": (
            {
                "path": str(source_metadata_path.resolve()),
                "checksum": checksum_path(source_metadata_path),
            }
            if source_metadata_path.exists()
            else {"status": "missing"}
        ),
        "projected_dem_path": str(app.paths.projected_dem_path.resolve()),
        "target_crs": app.viewshed.crs_projected,
        "dem_resolution_m": int(app.viewshed.dem_resolution_m),
        "bbox_wgs84": bbox,
    }


def projected_dem_metadata_matches(
    actual: dict[str, Any] | None,
    expected: dict[str, Any],
) -> bool:
    if not isinstance(actual, dict):
        return False
    for key in (
        "raw_dem_path",
        "raw_dem_signature",
        "raw_dem_source_metadata",
        "target_crs",
        "dem_resolution_m",
    ):
        if key not in actual:
            return False
        if key == "dem_resolution_m":
            if int(actual[key]) != int(expected[key]):
                return False
        elif key in {"raw_dem_signature", "raw_dem_source_metadata"}:
            if actual[key] != expected[key]:
                return False
        else:
            if str(actual[key]) != str(expected[key]):
                return False
    return True


def read_projected_dem_metadata(projected_dem_path: Path) -> dict[str, Any] | None:
    path = projected_dem_metadata_path(projected_dem_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def write_projected_dem_metadata(
    projected_dem_path: Path,
    metadata: dict[str, Any],
) -> Path:
    from datetime import datetime, timezone

    out = dict(metadata)
    out["created_at_utc"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    path = projected_dem_metadata_path(projected_dem_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n")
    return path


def ensure_projected_regional_dem(app: AppConfig) -> Path:
    config = viewshed_config_from_app_config(app)
    raw_dem = app.paths.regional_dem_path
    if not raw_dem.exists():
        raise FileNotFoundError(
            f"Configured regional DEM is missing: {raw_dem}. "
            "Run the DEM download/build step first. This viewshed module will not download DEMs."
        )
    projected = projected_dem_cache_path(app)
    expected_metadata = expected_projected_dem_metadata(app)
    actual_metadata = read_projected_dem_metadata(projected)
    metadata_matches = projected_dem_metadata_matches(actual_metadata, expected_metadata)
    # A path marked ready earlier in this process can belong to an older
    # configuration. Explicit overwrite must still replace that stale cache.
    overwrite = app.run.overwrite and (
        projected not in _PROJECTED_DEM_READY_THIS_PROCESS or not metadata_matches
    )
    if projected.exists() and core_raster.valid_raster(projected) and not overwrite:
        if not metadata_matches:
            raise ValueError(
                "Projected DEM cache exists but metadata does not match this run. "
                "Delete the cached DEM, set overwrite=True, or use a versioned projected_dem_path."
            )
        _PROJECTED_DEM_READY_THIS_PROCESS.add(projected)
        LOGGER.info("Reusing validated projected DEM cache: %s", projected)
        return projected

    with timer("project_regional_dem") as t:
        core_raster.reproject_raster(
            raw_dem,
            projected,
            config.crs_projected,
            config.dem_resolution_m,
            overwrite=overwrite,
            compress=app.raster.intermediate_compress,
            block_size=app.raster.block_size,
        )
    write_projected_dem_metadata(projected, expected_metadata)
    _PROJECTED_DEM_READY_THIS_PROCESS.add(projected)
    LOGGER.info(
        "Projected regional DEM ready path=%s elapsed_seconds=%.2f memory_mb=%s",
        projected,
        float(t["elapsed_seconds"] or 0.0),
        current_process_memory_mb(),
    )
    return projected


def estimated_batch_memory_mb(total_pixels: int) -> float:
    return float(int(total_pixels) * 32) / (1024 * 1024)


def validate_batch_guardrails(
    *,
    batch_id: str,
    total_pixels: int,
    estimated_memory_mb: float,
    max_batch_aoi_pixels: int | None,
    max_estimated_batch_memory_mb: float | None,
) -> None:
    if max_batch_aoi_pixels is not None and total_pixels > int(max_batch_aoi_pixels):
        raise ValueError(
            f"Batch AOI is too large for {batch_id}: {total_pixels:,} pixels "
            f"> max_batch_aoi_pixels={int(max_batch_aoi_pixels):,}. "
            "Reduce batch.batch_size_cells or use spatial batching."
        )
    if max_estimated_batch_memory_mb is not None and estimated_memory_mb > float(
        max_estimated_batch_memory_mb
    ):
        raise ValueError(
            f"Estimated batch memory is too high for {batch_id}: "
            f"{estimated_memory_mb:.1f} MB > "
            f"max_estimated_batch_memory_mb={float(max_estimated_batch_memory_mb):.1f}. "
            "Reduce batch.batch_size_cells or use spatial batching."
        )


def clip_raster_to_bounds(
    src_path: Path,
    dst_path: Path,
    bounds: tuple[float, float, float, float],
    overwrite: bool = False,
    compress: str | None = "none",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    fingerprint = _cache_fingerprint(
        {
            "algorithm_version": "viewshed_batch_dem_clip_v2",
            "source_raster": _input_signature(src_path),
            "bounds": [float(value) for value in bounds],
        }
    )
    if dst_path.exists() and not overwrite and _raster_cache_matches(dst_path, fingerprint):
        return dst_path
    result = core_raster.clip_raster_to_bounds(
        src_path,
        dst_path,
        bounds,
        overwrite=True,
        compress=compress,
        block_size=block_size,
    )
    with rasterio.open(result, "r+") as dst:
        dst.update_tags(
            algorithm_version="viewshed_batch_dem_clip_v2",
            cache_fingerprint=fingerprint,
            source_raster_path=str(Path(src_path).resolve()),
        )
    return result


def rasterize_water_to_match_dem(
    water_projected: gpd.GeoDataFrame,
    dem_path: Path,
    out_path: Path,
    overwrite: bool = False,
    compress: str | None = "none",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    with rasterio.open(dem_path) as dem_source:
        water_geometry_signature = _geometry_signature(
            water_projected,
            target_crs=dem_source.crs,
        )
    fingerprint = _cache_fingerprint(
        {
            "algorithm_version": "viewshed_water_mask_v2",
            "reference_dem": _input_signature(dem_path),
            "water_geometry_signature": water_geometry_signature,
            "all_touched": True,
        }
    )
    if out_path.exists() and not overwrite and _raster_cache_matches(out_path, fingerprint):
        return out_path
    if out_path.exists():
        out_path.unlink()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(dem_path) as src:
            water_match = water_projected.to_crs(src.crs)
            water_match = water_match[
                water_match.geometry.notna() & ~water_match.geometry.is_empty
            ].copy()

            water_arr = rasterize(
                [(geom, 1) for geom in water_match.geometry],
                out_shape=(src.height, src.width),
                transform=src.transform,
                fill=0,
                dtype="uint8",
                all_touched=True,
            )

            profile = core_raster.update_geotiff_profile(
                src.profile,
                compress=compress,
                block_size=block_size,
                dtype="uint8",
                count=1,
                nodata=0,
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(water_arr, 1)
                dst.update_tags(
                    algorithm_version="viewshed_water_mask_v2",
                    cache_fingerprint=fingerprint,
                    reference_dem_path=str(Path(dem_path).resolve()),
                    water_geometry_signature=water_geometry_signature,
                    all_touched="true",
                )
    except Exception:
        core_raster.remove_partial(out_path)
        raise
    return out_path


def flatten_water_pixels_to_sea_level(
    dem_path: Path,
    water_mask_path: Path,
    out_path: Path,
    sea_level_m: float = 0.0,
    overwrite: bool = False,
    compress: str | None = "none",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    core_raster.validate_raster_grid_alignment(
        dem_path,
        water_mask_path,
        label_a="batch_dem",
        label_b="water_mask",
    )
    fingerprint = _cache_fingerprint(
        {
            "algorithm_version": "viewshed_water_flattened_endpoint_v3",
            "dem": _input_signature(dem_path),
            "water_mask": _input_signature(water_mask_path),
            "sea_level_m": float(sea_level_m),
        }
    )
    if out_path.exists() and not overwrite and _raster_cache_matches(out_path, fingerprint):
        return out_path
    if out_path.exists():
        out_path.unlink()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(dem_path) as dem_src, rasterio.open(water_mask_path) as water_src:
            profile = core_raster.update_geotiff_profile(
                dem_src.profile,
                compress=compress,
                block_size=block_size,
                dtype="float32",
                count=1,
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                for _, window in dem_src.block_windows(1):
                    analysis = dem_src.read(1, window=window).astype("float32")
                    water_mask = water_src.read(1, window=window) == 1
                    # The canonical water mask, rather than DEM coverage, owns
                    # endpoint elevation over water. Projected DEMs can contain
                    # small coastal nodata holes even though modeled water
                    # endpoint elevation is explicitly sea level.
                    analysis[water_mask] = sea_level_m
                    dst.write(analysis, 1, window=window)
                dst.update_tags(
                    algorithm_version="viewshed_water_flattened_endpoint_v3",
                    cache_fingerprint=fingerprint,
                    source_dem_path=str(Path(dem_path).resolve()),
                    source_water_mask_path=str(Path(water_mask_path).resolve()),
                    sea_level_m=str(float(sea_level_m)),
                )
    except Exception:
        core_raster.remove_partial(out_path)
        raise
    return out_path


def apply_los_dem_nodata_policy(
    raster_path: Path,
    *,
    policy: str,
    opaque_barrier_height_m: float,
) -> dict[str, float | int | str]:
    """Resolve remaining DEM nodata before GDAL LOS execution.

    Observer endpoints are validated and repaired before this function runs.
    The opaque-barrier policy is deliberately conservative: unknown terrain can
    block a sightline but can never create false visibility through a DEM gap.
    """

    normalized_policy = str(policy).strip().lower()
    if normalized_policy not in {"error", "opaque_barrier"}:
        raise ValueError(
            "DEM nodata policy must be one of: 'error', 'opaque_barrier'. " f"Got {policy!r}."
        )
    barrier_height = float(opaque_barrier_height_m)
    if not math.isfinite(barrier_height) or barrier_height <= 0:
        raise ValueError("Opaque DEM nodata barrier height must be finite and > 0.")

    raster_path = Path(raster_path)
    invalid_count = 0
    with rasterio.open(raster_path, "r+") as raster:
        nodata = raster.nodata
        for _, window in raster.block_windows(1):
            values = raster.read(1, window=window)
            invalid = ~np.isfinite(values)
            if nodata is not None and math.isfinite(float(nodata)):
                invalid |= values == float(nodata)
            window_invalid_count = int(np.count_nonzero(invalid))
            if not window_invalid_count:
                continue
            invalid_count += window_invalid_count
            if normalized_policy == "opaque_barrier":
                values[invalid] = barrier_height
                raster.write(values, 1, window=window)

        if invalid_count and normalized_policy == "error":
            raise ValueError(
                "LOS analysis DEM contains nodata after observer endpoint validation: "
                f"path={raster_path} invalid_pixel_count={invalid_count:,}. Set "
                "viewshed.dem_nodata_policy='opaque_barrier' to conservatively block "
                "sightlines through missing DEM coverage."
            )
        raster.update_tags(
            los_dem_nodata_policy=normalized_policy,
            los_dem_nodata_pixel_count=str(invalid_count),
            los_dem_nodata_barrier_height_m=(
                str(barrier_height) if normalized_policy == "opaque_barrier" else ""
            ),
        )

    return {
        "policy": normalized_policy,
        "invalid_pixel_count": invalid_count,
        "opaque_barrier_height_m": barrier_height,
    }


def canonical_endpoint_repair(endpoint_dem_path: Path, source_dem_path: Path) -> dict[str, int]:
    """Repair nodata at fixed endpoint-grid centers, independent of observers.

    Exact source-grid center samples are preferred. Isolated source voids use
    the deterministic median of at least three immediate valid neighbors.
    Unresolved pixels remain nodata for the configured fail/opaque policy.
    Work is tiled; the repair is idempotent and does not use batch sample sets.
    """
    from pyproj import Transformer

    exact = neighborhood_count = 0
    with (
        rasterio.open(endpoint_dem_path, "r+") as endpoint,
        rasterio.open(source_dem_path) as source,
    ):
        converter = Transformer.from_crs(endpoint.crs, source.crs, always_xy=True)
        for _, window in endpoint.block_windows(1):
            values = endpoint.read(1, window=window, masked=True)
            invalid = np.ma.getmaskarray(values) | ~np.isfinite(values.data)
            rr, cc = np.nonzero(invalid)
            xs, ys = rasterio.transform.xy(endpoint.window_transform(window), rr, cc)
            xs, ys = converter.transform(xs, ys)
            # Read a source window once per endpoint tile, preserving exact
            # source.index floor semantics and the immediate 3x3 median rule.
            if len(rr):
                sr, sc = rasterio.transform.rowcol(source.transform, xs, ys)
                sr, sc = np.asarray(sr), np.asarray(sc)
                inside = (sr >= 0) & (sr < source.height) & (sc >= 0) & (sc < source.width)
                rr, cc, sr, sc = rr[inside], cc[inside], sr[inside], sc[inside]
                if len(rr):
                    r0, r1 = max(0, int(sr.min()) - 1), min(source.height, int(sr.max()) + 2)
                    c0, c1 = max(0, int(sc.min()) - 1), min(source.width, int(sc.max()) + 2)
                    block = source.read(1, window=((r0, r1), (c0, c1)), masked=True)
                    data = np.asarray(block.data, dtype=float).copy()
                    data[np.ma.getmaskarray(block) | ~np.isfinite(data)] = np.nan
                    samples = data[sr - r0, sc - c0]
                    valid = np.isfinite(samples)
                    values.data[rr[valid], cc[valid]] = samples[valid]
                    exact += int(valid.sum())
                    missing = ~valid
                    if missing.any():
                        padded = np.pad(data, 1, constant_values=np.nan)
                        r, c = sr[missing] - r0 + 1, sc[missing] - c0 + 1
                        neighbors = np.stack(
                            [padded[r + dr, c + dc] for dr in (-1, 0, 1) for dc in (-1, 0, 1)],
                            axis=1,
                        )
                        repair = np.isfinite(neighbors).sum(axis=1) >= 3
                        values.data[rr[missing][repair], cc[missing][repair]] = np.nanmedian(
                            neighbors[repair], axis=1
                        )
                        neighborhood_count += int(repair.sum())
            endpoint.write(values.data, 1, window=window)
        endpoint.update_tags(
            observer_endpoint_repair_algorithm_version="canonical_grid_center_repair_v3"
        )
    return {"exact": exact, "neighborhood": neighborhood_count}


def repair_observer_endpoint_nodata(
    endpoint_dem_path: Path,
    source_dem_path: Path,
    observers: gpd.GeoDataFrame,
) -> dict[str, int]:
    """Apply canonical repair and validate observer ground without sample-set edits."""
    if observers.crs is None:
        raise ValueError("Observer points must have a CRS before DEM endpoint repair.")
    with rasterio.open(endpoint_dem_path) as endpoint:
        projected = observers.to_crs(endpoint.crs)
        coords = [(p.x, p.y) for p in projected.geometry]
        before = list(endpoint.sample(coords, masked=True))
    invalid_count = sum(np.ma.is_masked(v[0]) or not np.isfinite(v[0]) for v in before)
    counts = canonical_endpoint_repair(endpoint_dem_path, source_dem_path)
    with rasterio.open(endpoint_dem_path) as endpoint:
        for value in endpoint.sample(coords, masked=True):
            if np.ma.is_masked(value[0]) or not np.isfinite(value[0]):
                raise ValueError(
                    "Observer ground remains unresolved: fewer than three immediate authoritative neighbor pixels are valid at the canonical grid center"
                )
    return {
        "observer_count": len(observers),
        "invalid_observer_count": int(invalid_count),
        "repaired_observer_count": int(invalid_count),
        "repaired_pixel_count": counts["exact"] + counts["neighborhood"],
        "exact_source_repaired_observer_count": counts["exact"],
        "neighborhood_repaired_observer_count": counts["neighborhood"],
    }


# -----------------------------------------------------------------------------
# H3 helpers
# -----------------------------------------------------------------------------
