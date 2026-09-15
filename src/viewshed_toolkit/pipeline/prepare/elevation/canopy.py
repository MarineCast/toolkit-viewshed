"""Prepare a canopy-aware elevation surface for radius viewsheds.

The terrain viewshed needs three different elevation semantics:

* observer base elevation: bare-earth DTM at the sampled source point;
* target base elevation: the water-flattened endpoint DEM;
* intervening obstruction: endpoint DEM plus canopy height on land.

GDAL accepts only one input elevation raster. Batch preparation keeps an
observer-neutral obstruction surface. Each GDAL LOS invocation overlays a private clearance patch on that surface
and restores only its own observer pixels to the endpoint DTM. Water pixels are never raised by CHM. A
GDAL observer height is then correctly interpreted as eye height above ground,
and the target height is correctly interpreted above the water surface.

Land-cover classes are deliberately outside this module.  They may be modeled
as a separate accessibility or attenuation factor without changing the hard
canopy line-of-sight surface created here.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import rowcol, xy
from rasterio.warp import Resampling, reproject

from .cache import cache_fingerprint as _cache_fingerprint
from .cache import input_signature as _input_signature
from .cache import raster_cache_matches as _raster_cache_matches

LOGGER = logging.getLogger(__name__)
VALID_SURFACE_MODELS = {"bare_earth", "canopy"}
VALID_CANOPY_NODATA_POLICIES = {"error", "zero"}
CANOPY_ALIGNMENT_ALGORITHM_VERSION = "canopy_alignment_v2"
CANOPY_SURFACE_ALGORITHM_VERSION = "single_observer_canopy_obstacle_surface_v3"
CANONICAL_CANOPY_BASE_ALGORITHM_VERSION = "canonical_canopy_base_surface_v1"
OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION = "observer_isolated_canopy_surface_v2"


@dataclass(frozen=True)
class CanopySurfaceResult:
    """Paths and diagnostics for one prepared canopy obstacle surface."""

    surface_path: Path
    aligned_canopy_path: Path
    observer_pixel_count: int
    land_pixel_count: int
    missing_land_canopy_pixel_count: int
    positive_canopy_pixel_count: int
    maximum_canopy_height_m: float


def normalize_surface_model(value: str | None) -> str:
    """Return a validated terrain surface model name."""

    normalized = str(value or "bare_earth").strip().lower()
    aliases = {
        "dtm": "bare_earth",
        "dem": "bare_earth",
        "dsm": "canopy",
        "dem_plus_chm": "canopy",
        "dtm_plus_chm": "canopy",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_SURFACE_MODELS:
        raise ValueError(
            "viewshed.surface_model must be one of: 'bare_earth', 'canopy'. " f"Got {value!r}."
        )
    return normalized


def normalize_canopy_nodata_policy(value: str | None) -> str:
    """Return a validated policy for CHM gaps over DEM-defined land."""

    normalized = str(value or "error").strip().lower()
    if normalized not in VALID_CANOPY_NODATA_POLICIES:
        raise ValueError(
            "viewshed.canopy_nodata_policy must be one of: 'error', 'zero'. " f"Got {value!r}."
        )
    return normalized


def canopy_resampling(value: str | None) -> Resampling:
    """Resolve a rasterio resampling method for continuous canopy height."""

    normalized = str(value or "max").strip().lower()
    if not hasattr(Resampling, normalized):
        raise ValueError(f"Unsupported viewshed.canopy_resampling method: {value!r}")
    return getattr(Resampling, normalized)


def _strict_grid_problems(path: Path, reference_path: Path) -> list[str]:
    with rasterio.open(path) as src, rasterio.open(reference_path) as ref:
        problems: list[str] = []
        if src.crs != ref.crs:
            problems.append(f"CRS {src.crs} != {ref.crs}")
        if src.width != ref.width or src.height != ref.height:
            problems.append(f"shape {(src.height, src.width)} != {(ref.height, ref.width)}")
        if not src.transform.almost_equals(ref.transform):
            problems.append(f"transform {src.transform} != {ref.transform}")
        return problems


def validate_strict_grid(path: Path, reference_path: Path, *, label: str) -> None:
    """Require an exact CRS, shape, orientation, resolution, and origin match."""

    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")
    problems = _strict_grid_problems(path, reference_path)
    if problems:
        raise ValueError(
            f"{label} is not aligned to the endpoint DEM grid: {path}; " + "; ".join(problems)
        )


def _atomic_raster_path(path: Path) -> Path:
    return path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")


def _observer_design_signature(observers_projected: gpd.GeoDataFrame) -> list[list[float]]:
    if observers_projected.empty:
        return []
    coordinates = sorted(
        [round(float(point.x), 6), round(float(point.y), 6)]
        for point in observers_projected.geometry
        if point is not None and not point.is_empty
    )
    return coordinates


def align_canopy_height_to_endpoint_dem(
    canopy_height_path: Path,
    endpoint_dem_path: Path,
    output_path: Path,
    *,
    resampling: str = "max",
    overwrite: bool = False,
    compress: str | None = "deflate",
) -> Path:
    """Warp CHM onto the exact endpoint DEM grid without using land cover."""

    canopy_height_path = Path(canopy_height_path)
    endpoint_dem_path = Path(endpoint_dem_path)
    output_path = Path(output_path)
    if not canopy_height_path.exists():
        raise FileNotFoundError(f"Missing canopy-height raster: {canopy_height_path}")
    alignment_payload = {
        "algorithm_version": CANOPY_ALIGNMENT_ALGORITHM_VERSION,
        "source_canopy": _input_signature(canopy_height_path),
        "endpoint_dem": _input_signature(endpoint_dem_path),
        "resampling": str(resampling),
    }
    alignment_fingerprint = _cache_fingerprint(alignment_payload)
    if output_path.exists() and not overwrite:
        validate_strict_grid(output_path, endpoint_dem_path, label="aligned canopy raster")
        if _raster_cache_matches(output_path, alignment_fingerprint):
            return output_path
        LOGGER.info("Rebuilding stale aligned canopy raster: %s", output_path)
        output_path.unlink()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_raster_path(output_path)
    tmp_path.unlink(missing_ok=True)
    try:
        with (
            rasterio.open(canopy_height_path) as source,
            rasterio.open(endpoint_dem_path) as endpoint,
        ):
            if source.crs is None:
                raise ValueError(f"Canopy-height raster has no CRS: {canopy_height_path}")
            if endpoint.crs is None:
                raise ValueError(f"Endpoint DEM has no CRS: {endpoint_dem_path}")
            profile = endpoint.profile.copy()
            profile.update(
                dtype="float32",
                count=1,
                nodata=np.nan,
                compress=compress or "none",
                BIGTIFF="IF_SAFER",
            )
            with rasterio.open(tmp_path, "w", **profile) as destination:
                reproject(
                    source=rasterio.band(source, 1),
                    destination=rasterio.band(destination, 1),
                    src_transform=source.transform,
                    src_crs=source.crs,
                    src_nodata=source.nodata,
                    dst_transform=endpoint.transform,
                    dst_crs=endpoint.crs,
                    dst_nodata=np.nan,
                    init_dest_nodata=True,
                    resampling=canopy_resampling(resampling),
                )
                destination.update_tags(
                    algorithm_version=CANOPY_ALIGNMENT_ALGORITHM_VERSION,
                    cache_fingerprint=alignment_fingerprint,
                    model_role="canopy_height",
                    source_canopy_height_path=str(canopy_height_path.resolve()),
                    reference_endpoint_dem_path=str(endpoint_dem_path.resolve()),
                    resampling=str(resampling),
                )
        tmp_path.replace(output_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    validate_strict_grid(output_path, endpoint_dem_path, label="aligned canopy raster")
    return output_path


def build_canonical_canopy_base_surface(
    *,
    endpoint_dem_path: Path,
    water_mask_path: Path,
    aligned_canopy_path: Path,
    output_surface_path: Path,
    minimum_canopy_height_m: float = 0.0,
    overwrite: bool = False,
    compress: str | None = "deflate",
) -> Path:
    """Build an observer-neutral domain DEM+CHM surface block by block.

    Missing CHM is represented as zero in this reusable base. Batch preparation
    still validates missing land CHM against the configured nodata policy using
    the aligned CHM raster before grounding observer pixels.
    """

    endpoint_dem_path = Path(endpoint_dem_path)
    water_mask_path = Path(water_mask_path)
    aligned_canopy_path = Path(aligned_canopy_path)
    output_surface_path = Path(output_surface_path)
    validate_strict_grid(water_mask_path, endpoint_dem_path, label="water mask")
    validate_strict_grid(
        aligned_canopy_path,
        endpoint_dem_path,
        label="aligned canopy raster",
    )
    threshold = max(0.0, float(minimum_canopy_height_m))
    payload = {
        "algorithm_version": CANONICAL_CANOPY_BASE_ALGORITHM_VERSION,
        "endpoint_dem": _input_signature(endpoint_dem_path),
        "water_mask": _input_signature(water_mask_path),
        "aligned_canopy": _input_signature(aligned_canopy_path),
        "minimum_canopy_height_m": threshold,
        "missing_canopy_base_value_m": 0.0,
    }
    fingerprint = _cache_fingerprint(payload)
    if output_surface_path.exists() and not overwrite:
        validate_strict_grid(
            output_surface_path,
            endpoint_dem_path,
            label="canonical canopy base surface",
        )
        if _raster_cache_matches(output_surface_path, fingerprint):
            return output_surface_path
        LOGGER.info("Rebuilding stale canonical canopy base: %s", output_surface_path)
        output_surface_path.unlink()

    output_surface_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_raster_path(output_surface_path)
    tmp_path.unlink(missing_ok=True)
    try:
        with (
            rasterio.open(endpoint_dem_path) as endpoint_source,
            rasterio.open(water_mask_path) as water_source,
            rasterio.open(aligned_canopy_path) as canopy_source,
        ):
            profile = endpoint_source.profile.copy()
            profile.update(
                dtype="float32",
                count=1,
                nodata=np.nan,
                compress=compress or "none",
                BIGTIFF="IF_SAFER",
            )
            with rasterio.open(tmp_path, "w", **profile) as destination:
                for _, window in endpoint_source.block_windows(1):
                    endpoint = (
                        endpoint_source.read(1, window=window, masked=True)
                        .filled(np.nan)
                        .astype("float32")
                    )
                    water = water_source.read(1, window=window) == 1
                    canopy = (
                        canopy_source.read(1, window=window, masked=True)
                        .filled(np.nan)
                        .astype("float32")
                    )
                    endpoint_valid = np.isfinite(endpoint)
                    land = endpoint_valid & ~water
                    canopy_height = np.where(
                        np.isfinite(canopy),
                        np.maximum(canopy, 0.0),
                        0.0,
                    ).astype("float32")
                    if threshold:
                        canopy_height[canopy_height < threshold] = 0.0
                    canopy_height[water] = 0.0
                    surface = endpoint.copy()
                    surface[land] = endpoint[land] + canopy_height[land]
                    destination.write(surface, 1, window=window)
                destination.update_tags(
                    algorithm_version=CANONICAL_CANOPY_BASE_ALGORITHM_VERSION,
                    cache_fingerprint=fingerprint,
                    terrain_surface_model="canonical_canopy_base",
                    observer_grounding="deferred_to_batch",
                    missing_canopy_base_value_m="0.0",
                    minimum_canopy_height_m=str(threshold),
                    source_endpoint_dem_path=str(endpoint_dem_path.resolve()),
                    source_water_mask_path=str(water_mask_path.resolve()),
                    aligned_canopy_height_path=str(aligned_canopy_path.resolve()),
                )
        tmp_path.replace(output_surface_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    validate_strict_grid(
        output_surface_path,
        endpoint_dem_path,
        label="canonical canopy base surface",
    )
    return output_surface_path


def _observer_clearance_pixels(
    observers_projected: gpd.GeoDataFrame,
    *,
    transform: Any,
    raster_crs: Any,
    shape: tuple[int, int],
    clearance_radius_m: float,
) -> set[tuple[int, int]]:
    if observers_projected.crs is None:
        raise ValueError("Observer points must declare a CRS before canopy preparation.")
    if raster_crs is None:
        raise ValueError("Endpoint DEM must declare a CRS before canopy preparation.")
    observers = observers_projected
    if observers.crs != raster_crs:
        observers = observers.to_crs(raster_crs)

    height, width = shape
    radius_m = max(0.0, float(clearance_radius_m))
    pixel_width_m = abs(float(transform.a))
    pixel_height_m = abs(float(transform.e))
    row_radius = int(math.ceil(radius_m / pixel_height_m)) if radius_m else 0
    col_radius = int(math.ceil(radius_m / pixel_width_m)) if radius_m else 0
    pixels: set[tuple[int, int]] = set()

    for point in observers.geometry:
        if point is None or point.is_empty:
            continue
        center_row, center_col = rowcol(transform, float(point.x), float(point.y))
        if not (0 <= center_row < height and 0 <= center_col < width):
            raise ValueError(
                "Observer falls outside the endpoint DEM while preparing the canopy surface: "
                f"x={float(point.x)} y={float(point.y)} row={center_row} col={center_col}"
            )
        pixels.add((int(center_row), int(center_col)))
        for row_index in range(
            max(0, center_row - row_radius), min(height, center_row + row_radius + 1)
        ):
            for col_index in range(
                max(0, center_col - col_radius), min(width, center_col + col_radius + 1)
            ):
                if radius_m:
                    pixel_x, pixel_y = xy(transform, row_index, col_index, offset="center")
                    if (
                        math.hypot(float(pixel_x) - float(point.x), float(pixel_y) - float(point.y))
                        > radius_m
                    ):
                        continue
                pixels.add((int(row_index), int(col_index)))
    if not pixels:
        raise ValueError("No observer pixels were available to ground on the canopy surface.")
    return pixels


def build_observer_grounded_canopy_surface_from_base(
    *,
    endpoint_dem_path: Path,
    water_mask_path: Path,
    aligned_canopy_path: Path,
    base_surface_path: Path,
    output_surface_path: Path,
    observers_projected: gpd.GeoDataFrame,
    nodata_policy: str = "error",
    minimum_canopy_height_m: float = 0.0,
    observer_clearance_radius_m: float = 0.0,
    overwrite: bool = False,
    compress: str | None = "deflate",
) -> CanopySurfaceResult:
    """Validate and publish an observer-neutral clipped canopy surface.

    Grounding is deferred to an isolated surface for each LOS call. The
    historical function name remains for callers; no batch pixel is cleared.
    """

    nodata_policy = normalize_canopy_nodata_policy(nodata_policy)
    for path, label in (
        (water_mask_path, "water mask"),
        (aligned_canopy_path, "aligned canopy raster"),
        (base_surface_path, "canonical canopy base surface"),
    ):
        validate_strict_grid(Path(path), Path(endpoint_dem_path), label=label)
    threshold = max(0.0, float(minimum_canopy_height_m))
    payload = {
        "algorithm_version": OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION,
        "endpoint_dem": _input_signature(Path(endpoint_dem_path)),
        "water_mask": _input_signature(Path(water_mask_path)),
        "aligned_canopy": _input_signature(Path(aligned_canopy_path)),
        "base_surface": _input_signature(Path(base_surface_path)),
        "nodata_policy": nodata_policy,
        "minimum_canopy_height_m": threshold,
        "observer_clearance_radius_m": max(0.0, float(observer_clearance_radius_m)),
        "observer_crs": str(observers_projected.crs),
        "observer_coordinates": _observer_design_signature(observers_projected),
    }
    fingerprint = _cache_fingerprint(payload)
    if Path(output_surface_path).exists() and not overwrite:
        validate_strict_grid(
            Path(output_surface_path),
            Path(endpoint_dem_path),
            label="observer-grounded canopy surface",
        )
        if _raster_cache_matches(Path(output_surface_path), fingerprint):
            with rasterio.open(output_surface_path) as existing:
                tags = existing.tags()
            return CanopySurfaceResult(
                surface_path=Path(output_surface_path),
                aligned_canopy_path=Path(aligned_canopy_path),
                observer_pixel_count=int(tags.get("observer_grounded_pixel_count", 0)),
                land_pixel_count=int(tags.get("land_pixel_count", 0)),
                missing_land_canopy_pixel_count=int(tags.get("missing_land_canopy_pixel_count", 0)),
                positive_canopy_pixel_count=int(tags.get("positive_canopy_pixel_count", 0)),
                maximum_canopy_height_m=float(tags.get("maximum_canopy_height_m", 0.0)),
            )

    with (
        rasterio.open(endpoint_dem_path) as endpoint_source,
        rasterio.open(water_mask_path) as water_source,
        rasterio.open(aligned_canopy_path) as canopy_source,
        rasterio.open(base_surface_path) as base_source,
    ):
        endpoint = endpoint_source.read(1, masked=True).filled(np.nan).astype("float32")
        water = water_source.read(1) == 1
        canopy = canopy_source.read(1, masked=True).filled(np.nan).astype("float32")
        surface = base_source.read(1, masked=True).filled(np.nan).astype("float32")
        profile = endpoint_source.profile.copy()
        raster_crs = endpoint_source.crs
        transform = endpoint_source.transform

    endpoint_valid = np.isfinite(endpoint)
    land = endpoint_valid & ~water
    canopy_valid = np.isfinite(canopy)
    missing_land_canopy = land & ~canopy_valid
    land_count = int(np.count_nonzero(land))
    missing_count = int(np.count_nonzero(missing_land_canopy))
    if missing_count and nodata_policy == "error":
        denominator = max(1, land_count)
        raise ValueError(
            "Canopy-height raster has missing values over DEM-defined land in the batch. "
            f"missing_land_pixels={missing_count:,} land_pixels={land_count:,} "
            f"fraction={missing_count / denominator:.6f}. Set "
            "viewshed.canopy_nodata_policy='zero' only when treating missing CHM as "
            "confirmed zero canopy is scientifically justified."
        )

    canopy_height = np.where(
        canopy_valid,
        np.maximum(canopy, 0.0),
        0.0,
    ).astype("float32")
    if threshold:
        canopy_height[canopy_height < threshold] = 0.0
    canopy_height[water] = 0.0
    observer_pixels = _observer_clearance_pixels(
        observers_projected,
        transform=transform,
        raster_crs=raster_crs,
        shape=surface.shape,
        clearance_radius_m=observer_clearance_radius_m,
    )
    for row_index, col_index in observer_pixels:
        if not endpoint_valid[row_index, col_index]:
            raise ValueError(
                "Observer pixel has no valid endpoint DEM elevation: "
                f"row={row_index} col={col_index} endpoint_dem={endpoint_dem_path}"
            )
        # Validate observer ground without changing the obstruction surface.

    positive_canopy = land & (canopy_height > 0.0)
    positive_count = int(np.count_nonzero(positive_canopy))
    max_height = float(np.max(canopy_height[land])) if np.any(land) else 0.0
    output_surface_path = Path(output_surface_path)
    output_surface_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_raster_path(output_surface_path)
    tmp_path.unlink(missing_ok=True)
    try:
        profile.update(
            dtype="float32",
            count=1,
            nodata=np.nan,
            compress=compress or "none",
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(tmp_path, "w", **profile) as destination:
            destination.write(surface, 1)
            destination.update_tags(
                algorithm_version=OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION,
                cache_fingerprint=fingerprint,
                terrain_surface_model="canopy",
                observer_base_surface="endpoint_dtm",
                target_base_surface="water_flattened_endpoint_dem",
                intervening_obstacle_surface="canonical_endpoint_dem_plus_chm",
                observer_grounded_pixel_count="0",
                observer_grounding="isolated_per_los_call",
                land_pixel_count=str(land_count),
                missing_land_canopy_pixel_count=str(missing_count),
                positive_canopy_pixel_count=str(positive_count),
                maximum_canopy_height_m=str(max_height),
            )
        tmp_path.replace(output_surface_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    validate_strict_grid(
        output_surface_path,
        Path(endpoint_dem_path),
        label="observer-grounded canopy surface",
    )
    return CanopySurfaceResult(
        surface_path=output_surface_path,
        aligned_canopy_path=Path(aligned_canopy_path),
        observer_pixel_count=0,
        land_pixel_count=land_count,
        missing_land_canopy_pixel_count=missing_count,
        positive_canopy_pixel_count=positive_count,
        maximum_canopy_height_m=max_height,
    )


def build_canopy_obstacle_surface(
    *,
    endpoint_dem_path: Path,
    water_mask_path: Path,
    canopy_height_path: Path,
    aligned_canopy_path: Path,
    output_surface_path: Path,
    observers_projected: gpd.GeoDataFrame,
    resampling: str = "max",
    nodata_policy: str = "error",
    minimum_canopy_height_m: float = 0.0,
    observer_clearance_radius_m: float = 0.0,
    overwrite: bool = False,
    compress: str | None = "deflate",
) -> CanopySurfaceResult:
    """Build a private single-observer canopy surface; batch grounding is forbidden."""

    if len(observers_projected) != 1:
        raise ValueError(
            "Canopy grounding requires exactly one observer; use a neutral batch surface"
        )
    nodata_policy = normalize_canopy_nodata_policy(nodata_policy)
    aligned_canopy_path = align_canopy_height_to_endpoint_dem(
        canopy_height_path,
        endpoint_dem_path,
        aligned_canopy_path,
        resampling=resampling,
        overwrite=overwrite,
        compress=compress,
    )
    validate_strict_grid(water_mask_path, endpoint_dem_path, label="water mask")

    surface_payload = {
        "algorithm_version": CANOPY_SURFACE_ALGORITHM_VERSION,
        "endpoint_dem": _input_signature(endpoint_dem_path),
        "water_mask": _input_signature(water_mask_path),
        "source_canopy": _input_signature(canopy_height_path),
        "aligned_canopy": _input_signature(aligned_canopy_path),
        "resampling": str(resampling),
        "nodata_policy": nodata_policy,
        "minimum_canopy_height_m": max(0.0, float(minimum_canopy_height_m)),
        "observer_clearance_radius_m": max(0.0, float(observer_clearance_radius_m)),
        "observer_crs": str(observers_projected.crs),
        "observer_coordinates": _observer_design_signature(observers_projected),
    }
    surface_fingerprint = _cache_fingerprint(surface_payload)

    if output_surface_path.exists() and not overwrite:
        validate_strict_grid(
            output_surface_path,
            endpoint_dem_path,
            label="canopy obstacle surface",
        )
        if _raster_cache_matches(output_surface_path, surface_fingerprint):
            with rasterio.open(output_surface_path) as existing:
                tags = existing.tags()
            return CanopySurfaceResult(
                surface_path=output_surface_path,
                aligned_canopy_path=aligned_canopy_path,
                observer_pixel_count=int(tags.get("observer_grounded_pixel_count", 0)),
                land_pixel_count=int(tags.get("land_pixel_count", 0)),
                missing_land_canopy_pixel_count=int(tags.get("missing_land_canopy_pixel_count", 0)),
                positive_canopy_pixel_count=int(tags.get("positive_canopy_pixel_count", 0)),
                maximum_canopy_height_m=float(tags.get("maximum_canopy_height_m", 0.0)),
            )
        LOGGER.info("Rebuilding stale canopy obstacle surface: %s", output_surface_path)
        output_surface_path.unlink()

    with (
        rasterio.open(endpoint_dem_path) as endpoint_source,
        rasterio.open(water_mask_path) as water_source,
        rasterio.open(aligned_canopy_path) as canopy_source,
    ):
        endpoint = endpoint_source.read(1, masked=True).filled(np.nan).astype("float32")
        water = water_source.read(1) == 1
        canopy = canopy_source.read(1, masked=True).filled(np.nan).astype("float32")
        profile = endpoint_source.profile.copy()
        raster_crs = endpoint_source.crs
        transform = endpoint_source.transform

    endpoint_valid = np.isfinite(endpoint)
    land = endpoint_valid & ~water
    canopy_valid = np.isfinite(canopy)
    missing_land_canopy = land & ~canopy_valid
    land_count = int(np.count_nonzero(land))
    missing_count = int(np.count_nonzero(missing_land_canopy))
    if missing_count and nodata_policy == "error":
        denominator = max(1, land_count)
        raise ValueError(
            "Canopy-height raster has missing values over DEM-defined land in the batch. "
            f"missing_land_pixels={missing_count:,} land_pixels={land_count:,} "
            f"fraction={missing_count / denominator:.6f}. Set "
            "viewshed.canopy_nodata_policy='zero' only when treating missing CHM as "
            "confirmed zero canopy is scientifically justified."
        )

    threshold = max(0.0, float(minimum_canopy_height_m))
    canopy_height = np.where(canopy_valid, np.maximum(canopy, 0.0), 0.0).astype("float32")
    if threshold:
        canopy_height[canopy_height < threshold] = 0.0
    canopy_height[water] = 0.0

    surface = endpoint.copy()
    surface[land] = endpoint[land] + canopy_height[land]
    observer_pixels = _observer_clearance_pixels(
        observers_projected,
        transform=transform,
        raster_crs=raster_crs,
        shape=surface.shape,
        clearance_radius_m=observer_clearance_radius_m,
    )
    for row_index, col_index in observer_pixels:
        if not endpoint_valid[row_index, col_index]:
            raise ValueError(
                "Observer pixel has no valid endpoint DEM elevation: "
                f"row={row_index} col={col_index} endpoint_dem={endpoint_dem_path}"
            )
        surface[row_index, col_index] = endpoint[row_index, col_index]

    positive_canopy = land & (canopy_height > 0.0)
    positive_count = int(np.count_nonzero(positive_canopy))
    max_height = float(np.max(canopy_height[land])) if np.any(land) else 0.0

    output_surface_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_raster_path(output_surface_path)
    tmp_path.unlink(missing_ok=True)
    try:
        profile.update(
            dtype="float32",
            count=1,
            nodata=np.nan,
            compress=compress or "none",
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(tmp_path, "w", **profile) as destination:
            destination.write(surface.astype("float32", copy=False), 1)
            destination.update_tags(
                algorithm_version=CANOPY_SURFACE_ALGORITHM_VERSION,
                cache_fingerprint=surface_fingerprint,
                terrain_surface_model="canopy",
                observer_base_surface="endpoint_dtm",
                target_base_surface="water_flattened_endpoint_dem",
                intervening_obstacle_surface="endpoint_dem_plus_chm",
                landcover_used="false",
                source_endpoint_dem_path=str(Path(endpoint_dem_path).resolve()),
                source_canopy_height_path=str(Path(canopy_height_path).resolve()),
                aligned_canopy_height_path=str(Path(aligned_canopy_path).resolve()),
                canopy_resampling=str(resampling),
                canopy_nodata_policy=nodata_policy,
                minimum_canopy_height_m=str(threshold),
                observer_clearance_radius_m=str(max(0.0, observer_clearance_radius_m)),
                observer_grounded_pixel_count=str(len(observer_pixels)),
                land_pixel_count=str(land_count),
                missing_land_canopy_pixel_count=str(missing_count),
                positive_canopy_pixel_count=str(positive_count),
                maximum_canopy_height_m=str(max_height),
            )
        tmp_path.replace(output_surface_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    validate_strict_grid(
        output_surface_path,
        endpoint_dem_path,
        label="canopy obstacle surface",
    )
    LOGGER.info(
        "Canopy obstacle surface ready path=%s observer_pixels=%d "
        "positive_canopy_pixels=%d missing_land_canopy_pixels=%d max_canopy_height_m=%.2f",
        output_surface_path,
        len(observer_pixels),
        positive_count,
        missing_count,
        max_height,
    )
    return CanopySurfaceResult(
        surface_path=output_surface_path,
        aligned_canopy_path=aligned_canopy_path,
        observer_pixel_count=len(observer_pixels),
        land_pixel_count=land_count,
        missing_land_canopy_pixel_count=missing_count,
        positive_canopy_pixel_count=positive_count,
        maximum_canopy_height_m=max_height,
    )


def isolate_observer_canopy_surface(
    *,
    base_surface_path: Path,
    endpoint_dem_path: Path,
    output_path: Path,
    observer_x: float,
    observer_y: float,
    clearance_radius_m: float = 0.0,
) -> Path:
    """Copy a neutral obstruction grid and ground only this observer's pixel(s).

    The optional clearance radius is an explicit physical assumption. Neither
    the shared base nor other observers' pixels are changed.
    """
    validate_strict_grid(base_surface_path, endpoint_dem_path, label="canopy base")
    with rasterio.open(base_surface_path) as base, rasterio.open(endpoint_dem_path) as ground:
        observers = gpd.GeoDataFrame(
            geometry=gpd.points_from_xy([observer_x], [observer_y]), crs=base.crs
        )
        pixels = _observer_clearance_pixels(
            observers,
            transform=base.transform,
            raster_crs=base.crs,
            shape=(base.height, base.width),
            clearance_radius_m=clearance_radius_m,
        )
        with rasterio.open(output_path, "w", **base.profile) as output:
            for _, window in base.block_windows(1):
                data = base.read(1, window=window)
                for row, col in pixels:
                    if (
                        window.row_off <= row < window.row_off + window.height
                        and window.col_off <= col < window.col_off + window.width
                    ):
                        value = ground.read(
                            1, window=rasterio.windows.Window(col, row, 1, 1), masked=True
                        )
                        if np.ma.is_masked(value[0, 0]) or not np.isfinite(value[0, 0]):
                            raise ValueError("Observer ground elevation is unavailable")
                        data[row - int(window.row_off), col - int(window.col_off)] = value[0, 0]
                output.write(data, 1, window=window)
            output.update_tags(
                algorithm_version=OBSERVER_GROUNDED_CANOPY_ALGORITHM_VERSION,
                observer_grounded_pixel_count=str(len(pixels)),
                observer_clearance_radius_m=str(clearance_radius_m),
            )
    return output_path


def isolate_observer_canopy_vrt(
    *,
    base_surface_path: Path,
    endpoint_dem_path: Path,
    output_path: Path,
    observer_x: float,
    observer_y: float,
    clearance_radius_m: float = 0.0,
) -> Path:
    """Overlay a small private clearance patch without copying the whole grid."""
    from xml.etree import ElementTree as ET

    from osgeo import gdal

    validate_strict_grid(base_surface_path, endpoint_dem_path, label="canopy base")
    with rasterio.open(base_surface_path) as base, rasterio.open(endpoint_dem_path) as ground:
        observers = gpd.GeoDataFrame(
            geometry=gpd.points_from_xy([observer_x], [observer_y]), crs=base.crs
        )
        pixels = _observer_clearance_pixels(
            observers,
            transform=base.transform,
            raster_crs=base.crs,
            shape=(base.height, base.width),
            clearance_radius_m=clearance_radius_m,
        )
        rows, cols = zip(*pixels, strict=True)
        row_min, col_min = min(rows), min(cols)
        patch_height, patch_width = max(rows) - row_min + 1, max(cols) - col_min + 1
        window = rasterio.windows.Window(col_min, row_min, patch_width, patch_height)
        patch = base.read(1, window=window)
        ground_values = ground.read(1, window=window, masked=True)
        for row, col in pixels:
            value = ground_values[row - row_min, col - col_min]
            if np.ma.is_masked(value) or not np.isfinite(value):
                raise ValueError("Observer ground elevation is unavailable")
            patch[row - row_min, col - col_min] = value
        patch_path = output_path.with_suffix(".patch.tif")
        with rasterio.open(
            patch_path,
            "w",
            driver="GTiff",
            height=patch_height,
            width=patch_width,
            count=1,
            dtype=base.dtypes[0],
            crs=base.crs,
            transform=base.window_transform(window),
            nodata=base.nodata,
        ) as target:
            target.write(patch, 1)
        tree = ET.Element("VRTDataset", rasterXSize=str(base.width), rasterYSize=str(base.height))
        ET.SubElement(tree, "SRS").text = base.crs.to_wkt()
        ET.SubElement(tree, "GeoTransform").text = ",".join(map(str, base.transform.to_gdal()))
        dataset = gdal.Open(str(base_surface_path))
        dtype = gdal.GetDataTypeName(dataset.GetRasterBand(1).DataType)
        dataset = None
        band = ET.SubElement(tree, "VRTRasterBand", dataType=dtype, band="1")
        if base.nodata is not None:
            ET.SubElement(band, "NoDataValue").text = str(base.nodata)
        for path, width, height, xoff, yoff in (
            (base_surface_path, base.width, base.height, 0, 0),
            (patch_path, patch_width, patch_height, col_min, row_min),
        ):
            source = ET.SubElement(band, "SimpleSource")
            ET.SubElement(source, "SourceFilename", relativeToVRT="0").text = str(path.resolve())
            ET.SubElement(source, "SourceBand").text = "1"
            ET.SubElement(
                source, "SrcRect", xOff="0", yOff="0", xSize=str(width), ySize=str(height)
            )
            ET.SubElement(
                source,
                "DstRect",
                xOff=str(xoff),
                yOff=str(yoff),
                xSize=str(width),
                ySize=str(height),
            )
        ET.ElementTree(tree).write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path
