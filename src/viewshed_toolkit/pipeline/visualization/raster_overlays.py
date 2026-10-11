"""Offline display assets retaining exact caller-qualified native raster values."""

import base64
import hashlib
import json
import struct
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from affine import Affine
from rasterio.crs import CRS
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

RASTER_OVERLAY_CONTRACT = "native_30m_inspection_display_preview_v1"


def _png(rgba: NDArray[Any]) -> str:
    height, width, _ = rgba.shape

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            struct.pack("!I", len(value))
            + kind
            + value
            + struct.pack("!I", zlib.crc32(kind + value))
        )

    raw = b"".join(b"\0" + row.tobytes() for row in rgba)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def export_raster_overlay_asset(
    source: str,
    arrays: Mapping[str, NDArray[Any]],
    *,
    transform: Affine,
    crs: CRS | str,
    output_path: Path,
    provenance: Mapping[str, Any],
    preview_pixels: int = 512,
) -> dict[str, Any]:
    """Write one local classic-script asset; no network, sampling or LOS calculation.

    Caller supplies aligned dem/dem_original_delta/chm_raw/chm_lower/building/joint/flags grids.
    The sparse original-minus-model DEM delta exposes water flattening without
    duplicating another full terrain replay.
    Floats retain exact float32 native values, including supported height floors.
    Flags: canopy unknown=1, building unknown=2, joint unknown=4, building
    present=8, missing-height overlap=16, qualified water=32, water conflict=64,
    target water center=128. Zero floors under unknown support are not observations.
    Native inspection currently supports only EPSG:32610 north-up square grids.
    Display PNGs are nearest-neighbor reprojections, explicitly separate from values.
    """
    required = {"dem", "dem_original_delta", "chm_raw", "chm_lower", "building", "joint", "flags"}
    if set(arrays) != required or CRS.from_user_input(crs).to_epsg() != 32610:
        raise ValueError("Exact aligned layer set and EPSG:32610 required")
    if (
        not source
        or not 16 <= preview_pixels <= 1024
        or not np.isfinite(tuple(transform)).all()
        or transform.a != 30
        or transform.e != -transform.a
        or transform.b != 0
        or transform.d != 0
    ):
        raise ValueError("Finite north-up 30 m square grid and bounded display size required")
    shape = arrays["dem"].shape
    if len(shape) != 2 or not all(shape) or any(a.shape != shape for a in arrays.values()):
        raise ValueError("Nonempty aligned two-dimensional arrays required")
    for name, values in arrays.items():
        if name in {"flags", "chm_raw"}:
            if values.dtype != np.uint8:
                raise ValueError("Raw CHM and flags must preserve uint8 values")
        elif values.dtype != np.float32 or not np.isfinite(values).all():
            raise ValueError("Finite exact float32 DEM and supported lower surfaces required")
        if name in {"chm_lower", "building", "joint"} and (values < 0).any():
            raise ValueError("Above-ground lower surfaces must be nonnegative")
    height, width = shape
    bounds = array_bounds(height, width, transform)
    dst_transform, pw, ph = calculate_default_transform(
        crs,
        "EPSG:4326",
        width,
        height,
        *bounds,
        dst_width=preview_pixels,
        dst_height=max(16, round(preview_pixels * height / width)),
    )
    dst_bounds = list(array_bounds(ph, pw, dst_transform))
    flags = arrays["flags"]
    layer_inputs = {
        "dem_original": (
            arrays["dem"] + arrays["dem_original_delta"],
            np.zeros(shape, dtype=bool),
            "DEM before water flattening",
            "m; declared DEM reference; original projected terrain values",
        ),
        "dem": (
            arrays["dem"],
            np.zeros(shape, dtype=bool),
            "Terrain elevation",
            "m; declared DEM reference, water flattened to 0",
        ),
        "chm_raw": (
            arrays["chm_raw"].astype("float32"),
            arrays["chm_raw"] == 255,
            "CHM: raw maximum height",
            "m above ground; ETH2020;255 is no data",
        ),
        "building": (
            arrays["building"],
            (flags & 2) != 0,
            "Building raster prediction floor",
            "m above ground; pre-water-qualification GBA raster prediction; unknown upper explicit",
        ),
        "joint": (
            arrays["joint"],
            np.zeros(shape, dtype=bool),
            "Joint above-ground lower surface",
            "m above ground; max qualified canopy/building; unknown upper separate",
        ),
        "unknown": (
            ((flags & 4) != 0).astype("float32"),
            np.zeros(shape, dtype=bool),
            "Joint unknown upper support",
            "0 resolved /1 unknown; not inferred height zero",
        ),
        "water": (
            ((flags & 32) != 0).astype("float32"),
            np.zeros(shape, dtype=bool),
            "Qualified full-water pixels",
            "0 not qualified /1 fully supported water; distinct from target-center mask",
        ),
    }
    layer_inputs["targets"] = (
        ((flags & 128) != 0).astype("float32"),
        np.zeros(shape, dtype=bool),
        "Retained target-water raster samples",
        "0 excluded /1 retained sample; exact current comparison census",
    )
    previews = {}
    for key, (values, missing, label, units) in layer_inputs.items():
        data = np.where(missing, np.nan, values).astype("float32")
        preview = np.full((ph, pw), np.nan, dtype="float32")
        reproject(
            data,
            preview,
            src_transform=transform,
            src_crs=crs,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
        )
        valid = data[np.isfinite(data)]
        low = float(valid.min()) if valid.size else 0.0
        high = float(valid.max()) if valid.size else 1.0
        q = np.clip((np.nan_to_num(preview, nan=low) - low) / max(high - low, 1), 0, 1)
        rgba = np.zeros((*preview.shape, 4), dtype="uint8")
        rgba[..., 0] = 235 - (q * 205).astype("uint8")
        rgba[..., 1] = 245 - (q * 95).astype("uint8")
        rgba[..., 2] = 220 - (q * 170).astype("uint8")
        rgba[..., 3] = 255
        rgba[~np.isfinite(preview)] = [210, 60, 170, 200]
        if key in {"unknown", "water", "targets"}:
            rgba[preview == 0, 3] = 0
            rgba[preview == 1] = [140, 79, 181, 220] if key == "unknown" else [8, 126, 164, 220]
        # Outside the reprojected native grid is transparent, not no-data support.
        coverage = np.zeros((ph, pw), dtype="uint8")
        reproject(
            np.ones(shape, dtype="uint8"),
            coverage,
            src_transform=transform,
            src_crs=crs,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.nearest,
        )
        rgba[coverage == 0, 3] = 0
        previews[key] = dict(
            label=label,
            units=units,
            min=low,
            max=high,
            image=_png(rgba),
            bounds=dst_bounds,
            width=pw,
            height=ph,
            display_only="Nearest display reprojection; native values remain 30m",
        )
    encoded = {}
    for name, values in arrays.items():
        raw = (
            values.astype("<f4", copy=False).tobytes()
            if name not in {"chm_raw", "flags"}
            else values.tobytes()
        )
        encoded[name] = dict(
            dtype="uint8" if name in {"chm_raw", "flags"} else "float32le",
            zlib_base64=base64.b64encode(zlib.compress(raw, 9)).decode(),
            sha256=hashlib.sha256(raw).hexdigest(),
            native_bytes=len(raw),
        )
    asset = dict(
        contract=RASTER_OVERLAY_CONTRACT,
        source=source,
        crs="EPSG:32610",
        transform=list(transform)[:6],
        width=width,
        height=height,
        native_pixel_m=float(transform.a),
        arrays=encoded,
        previews=previews,
        provenance=dict(provenance),
        flag_bits=dict(
            canopy_unknown=1,
            building_unknown=2,
            joint_unknown=4,
            building_present=8,
            missing_height_overlap=16,
            qualified_water=32,
            water_conflict=64,
            target_water_center=128,
        ),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "globalThis.VIEWSHED_RASTERS["
        + json.dumps(source)
        + "]="
        + json.dumps(asset, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
        + ";\n"
    )
    output_path.write_text(text)
    return dict(
        source=source,
        path=output_path.name,
        bytes=output_path.stat().st_size,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        crs=asset["crs"],
        width=width,
        height=height,
        native_pixel_m=asset["native_pixel_m"],
        previews={k: {n: v for n, v in p.items() if n != "image"} for k, p in previews.items()},
        provenance=dict(provenance),
    )
