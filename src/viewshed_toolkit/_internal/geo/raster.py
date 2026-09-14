"""Raster helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import Window, from_bounds

GEOTIFF_BLOCK_SIZE = 256


def normalize_compress(compress: str | None) -> str | None:
    if compress is None:
        return None
    value = str(compress).strip().lower()
    if value in {"", "none", "false", "no"}:
        return None
    return value


def update_geotiff_profile(
    profile: dict,
    *,
    compress: str | None = "deflate",
    block_size: int = GEOTIFF_BLOCK_SIZE,
    **updates: object,
) -> dict:
    out = profile.copy()
    out.update(updates)

    width = int(out.get("width", block_size))
    height = int(out.get("height", block_size))
    block_size = int(block_size)

    if width >= 16 and height >= 16:
        out["tiled"] = True
        out["blockxsize"] = min(block_size, max(16, (width // 16) * 16))
        out["blockysize"] = min(block_size, max(16, (height // 16) * 16))
    else:
        out.pop("tiled", None)
        out.pop("blockxsize", None)
        out.pop("blockysize", None)

    compress_value = normalize_compress(compress)
    if compress_value:
        out["compress"] = compress_value
        dtype_value = str(out.get("dtype", "")).lower()
        if "float" in dtype_value:
            out.setdefault("predictor", 3)
        elif dtype_value:
            out.setdefault("predictor", 2)
    else:
        out.pop("compress", None)
        out.pop("predictor", None)

    return out


def valid_raster(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with rasterio.open(path) as src:
            _ = src.profile
            _ = src.bounds
        return True
    except Exception:
        return False


def reuse_or_remove_raster(path: Path, overwrite: bool) -> bool:
    if not path.exists() or overwrite:
        return False
    if valid_raster(path):
        return True
    path.unlink()
    return False


def remove_partial(path: Path) -> None:
    if path.exists():
        path.unlink()


def validate_raster_grid_alignment(
    raster_a: Path,
    raster_b: Path,
    *,
    label_a: str = "raster_a",
    label_b: str = "raster_b",
    require_same_shape: bool = True,
    require_same_transform: bool = True,
) -> None:
    """Validate that two rasters are aligned on the same grid."""
    with rasterio.open(raster_a) as src_a, rasterio.open(raster_b) as src_b:
        if src_a.crs != src_b.crs:
            raise ValueError(
                f"Raster CRS mismatch: {label_a}={raster_a} crs={src_a.crs}; "
                f"{label_b}={raster_b} crs={src_b.crs}"
            )
        if require_same_shape and (src_a.width != src_b.width or src_a.height != src_b.height):
            raise ValueError(
                f"Raster shape mismatch: {label_a}={raster_a} shape=({src_a.height}, {src_a.width}); "
                f"{label_b}={raster_b} shape=({src_b.height}, {src_b.width})"
            )
        if require_same_transform and not src_a.transform.almost_equals(src_b.transform):
            raise ValueError(
                f"Raster transform mismatch: {label_a}={raster_a} transform={src_a.transform}; "
                f"{label_b}={raster_b} transform={src_b.transform}"
            )


def write_array_like(
    reference_raster_path: Path,
    out_path: Path,
    array: np.ndarray,
    dtype: str,
    nodata: float | int | None = None,
    overwrite: bool = True,
    compress: str | None = "deflate",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    if reuse_or_remove_raster(out_path, overwrite):
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(reference_raster_path) as src:
            profile = update_geotiff_profile(
                src.profile,
                compress=compress,
                block_size=block_size,
                dtype=dtype,
                count=1,
                nodata=nodata,
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(array.astype(dtype, copy=False), 1)
    except Exception:
        remove_partial(out_path)
        raise
    return out_path


def reproject_raster(
    src_path: Path,
    dst_path: Path,
    dst_crs: str,
    resolution_m: float,
    overwrite: bool = False,
    compress: str | None = "none",
    block_size: int = GEOTIFF_BLOCK_SIZE,
    resampling: Resampling = Resampling.bilinear,
) -> Path:
    if reuse_or_remove_raster(dst_path, overwrite):
        return dst_path

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(src_path) as src:
            transform, width, height = calculate_default_transform(
                src.crs,
                dst_crs,
                src.width,
                src.height,
                *src.bounds,
                resolution=resolution_m,
            )
            profile = update_geotiff_profile(
                src.profile,
                compress=compress,
                block_size=block_size,
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height,
            )

            with rasterio.open(dst_path, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=resampling,
                )
    except Exception:
        remove_partial(dst_path)
        raise
    return dst_path


def clip_raster_to_bounds(
    src_path: Path,
    dst_path: Path,
    bounds: tuple[float, float, float, float],
    *,
    overwrite: bool = False,
    compress: str | None = "none",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    if reuse_or_remove_raster(dst_path, overwrite):
        return dst_path

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(src_path) as src:
            requested_left, requested_bottom, requested_right, requested_top = map(float, bounds)
            left = max(requested_left, float(src.bounds.left))
            bottom = max(requested_bottom, float(src.bounds.bottom))
            right = min(requested_right, float(src.bounds.right))
            top = min(requested_top, float(src.bounds.top))
            if left >= right or bottom >= top:
                raise ValueError(
                    "Requested clip bounds do not overlap the source raster: "
                    f"requested={bounds} source_bounds={tuple(src.bounds)} "
                    f"source={src_path}"
                )
            # Intersect before constructing the window. Rasterio clips an
            # out-of-range read window but does not adjust the transform from
            # that original window, which silently shifts partially overlapping
            # rasters and can place valid observer points outside the result.
            window = from_bounds(left, bottom, right, top, src.transform)
            window = window.round_offsets().round_lengths()
            window = window.intersection(Window(0, 0, src.width, src.height))
            data = src.read(1, window=window)
            transform = src.window_transform(window)
            profile = update_geotiff_profile(
                src.profile,
                compress=compress,
                block_size=block_size,
                height=data.shape[0],
                width=data.shape[1],
                transform=transform,
            )
            with rasterio.open(dst_path, "w", **profile) as dst:
                dst.write(data, 1)
    except Exception:
        remove_partial(dst_path)
        raise
    return dst_path


def rasterize_geometries_to_match(
    geometries: Any,
    reference_raster_path: Path,
    out_path: Path,
    *,
    fill: int = 0,
    default_value: int = 1,
    dtype: str = "uint8",
    overwrite: bool = False,
    compress: str | None = "deflate",
    block_size: int = GEOTIFF_BLOCK_SIZE,
) -> Path:
    if reuse_or_remove_raster(out_path, overwrite):
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(reference_raster_path) as src:
            arr = rasterize(
                [
                    (geom, default_value)
                    for geom in geometries
                    if geom is not None and not geom.is_empty
                ],
                out_shape=(src.height, src.width),
                transform=src.transform,
                fill=fill,
                dtype=dtype,
            )
            profile = update_geotiff_profile(
                src.profile,
                compress=compress,
                block_size=block_size,
                dtype=dtype,
                count=1,
                nodata=fill,
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(arr, 1)
    except Exception:
        remove_partial(out_path)
        raise
    return out_path
