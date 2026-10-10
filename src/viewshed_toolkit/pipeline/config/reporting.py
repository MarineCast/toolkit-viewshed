"""Consume the owner-pinned registry without inferring reporting from a bbox."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import h3
from pyproj import Geod
from shapely.geometry import Polygon, box, mapping
from shapely.geometry.polygon import orient

from viewshed_toolkit._internal.geo.geometry import safe_polygonal_union
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

_SUPPORT_CACHE: dict[tuple[str, ...], ReportingSupport] = {}


def support_polygons_for_reporting_geometry(
    reporting_water: Any, crs: str, los_m: float, margin_m: float
) -> tuple[Any, Any, Any]:
    """Derive observer/path support from supplied qualified reporting water.

    The input is already materialized WGS84 geometry. No envelope or coastline
    complement supplies a fallback, and its boundary is not reconstructed here.
    """
    import geopandas as gpd
    from pyproj import CRS

    if (
        reporting_water is None
        or reporting_water.is_empty
        or not reporting_water.is_valid
        or reporting_water.geom_type not in {"Polygon", "MultiPolygon"}
    ):
        raise ValueError("Reporting water must be valid nonempty polygonal geometry")
    if not all(math.isfinite(value) and value >= 0 for value in (los_m, margin_m)):
        raise ValueError("Reporting LOS and AOI margin must be finite and nonnegative")
    projected_crs = CRS.from_user_input(crs)
    if not projected_crs.is_projected or any(
        axis.unit_name != "metre" for axis in projected_crs.axis_info
    ):
        raise ValueError("Reporting support requires a projected CRS in metres")
    west, south, east, north = reporting_water.bounds
    if not (-180 <= west < east <= 180 and -90 < south < north < 90):
        raise ValueError("Reporting water geometry must be WGS84 longitude/latitude")
    projected = gpd.GeoSeries([reporting_water], crs="EPSG:4326").to_crs(crs).iloc[0]
    source_wgs, native_wgs = gpd.GeoSeries(
        [projected.buffer(los_m), projected.buffer(los_m + margin_m)], crs=crs
    ).to_crs(4326)
    return reporting_water, source_wgs, native_wgs


def validate_reporting_membership_ids(
    cell_ids: list[Any], *, resolution: int, count: int, sha256: str
) -> tuple[str, ...]:
    """Check decoded IDs under the owner's canonical newline membership identity.

    Decoding the artifact is deliberately separate: the format must be pinned
    by its owner. No null removal, case conversion or duplicate repair is allowed.
    """
    import h3

    if any(
        not isinstance(cell, str)
        or cell != cell.lower()
        or not h3.is_valid_cell(cell)
        or h3.int_to_str(h3.str_to_int(cell)) != cell
        or h3.get_resolution(cell) != resolution
        for cell in cell_ids
    ):
        raise ValueError(
            "Reporting membership requires valid lowercase H3 IDs at declared resolution"
        )
    if len(cell_ids) != count or len(set(cell_ids)) != count:
        raise ValueError("Reporting membership count or uniqueness mismatch")
    if cell_ids != sorted(cell_ids):
        raise ValueError("Reporting membership IDs must be sorted")
    payload = "".join(f"{cell}\n" for cell in cell_ids).encode("ascii")
    if hashlib.sha256(payload).hexdigest() != sha256:
        raise ValueError("Reporting membership canonical SHA256 mismatch")
    return tuple(cell_ids)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reporting_cells_for_geometry(geometry: Any, resolution: int) -> tuple[str, ...]:
    """Enumerate overlap, retaining strictly positive ellipsoidal polygon area."""
    geod = Geod(ellps="WGS84")
    candidates = h3.h3shape_to_cells_experimental(
        h3.geo_to_h3shape(mapping(geometry)), resolution, contain="overlap"
    )
    result = []
    for cell in candidates:
        overlap = cell_to_polygon(cell).intersection(geometry)
        polygons = [overlap] if overlap.geom_type == "Polygon" else getattr(overlap, "geoms", [])
        area = sum(
            abs(geod.geometry_area_perimeter(orient(part, sign=1))[0])
            for part in polygons
            if part.geom_type == "Polygon" and not part.is_empty
        )
        if area > 0:
            result.append(cell)
    return tuple(sorted(result))


def read_polygon(path: Path) -> Any:
    frame = (
        gpd.read_parquet(path)
        if path.suffix.lower() in {".parquet", ".geoparquet"}
        else gpd.read_file(path)
    )
    if frame.crs is None or frame.empty:
        raise ValueError(f"Declared geometry must have CRS and polygon features: {path}")
    if any(
        geom is None
        or geom.is_empty
        or not geom.is_valid
        or geom.geom_type not in {"Polygon", "MultiPolygon"}
        for geom in frame.geometry
    ):
        raise ValueError(f"Declared geometry is invalid or not polygonal: {path}")
    return safe_polygonal_union(frame.to_crs(4326))


@dataclass(frozen=True)
class ReportingSupport:
    mask_path: Path
    membership_path: Path
    cells: tuple[str, ...]
    reporting_water: Any
    source_extent: Any
    native_extent: Any
    identity: dict[str, Any]


def load_reporting_support(raw: dict[str, Any], crs: str) -> ReportingSupport:
    study = raw["marinecast_study"]
    registry = study["grid_registry"]
    if registry["status"] != "validated":
        raise ValueError("Reporting artifacts require a validated registry")
    root = Path(study["resolved_data_root"]).resolve()
    base = Path(study["study_config_directory"]).resolve()
    entries = [
        item
        for item in registry["memberships"]
        if item["resolution"] == 7 and item["role"] == "water_reporting"
    ]
    if len(entries) != 1:
        raise ValueError("Exactly one R7 water_reporting membership artifact is required")
    entry = entries[0]
    relative = Path(entry["relative_path"])
    if relative.is_absolute():
        raise ValueError("Membership artifact path must be relative to selected study config")
    member_path = (base / relative).resolve()
    if not member_path.is_relative_to(root):
        raise ValueError("Reporting membership path escapes configured Data root")
    if member_path.stat().st_size != entry["count"] * 16:
        raise ValueError("Membership artifact byte length does not match canonical R7 count")
    payload = member_path.read_bytes()
    if payload and (not payload.endswith(b"\n") or b"\r" in payload):
        raise ValueError("Membership artifact requires LF lines and terminal newline")
    try:
        ids = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Membership artifact must be ASCII") from exc
    cells = validate_reporting_membership_ids(
        ids, resolution=7, count=entry["count"], sha256=entry["sha256"]
    )
    mask_value = raw["paths"].get("reporting_water_polygon_path")
    if not mask_value:
        raise ValueError("Shared execution requires explicit paths.reporting_water_polygon_path")
    mask_path = Path(mask_value).resolve()
    if not mask_path.is_relative_to(root):
        raise ValueError("Reporting mask path escapes configured Data root")
    mask_hash = file_sha256(mask_path)
    if mask_hash != registry["mask_sha256"]:
        raise ValueError("Reporting mask does not match shared mask_sha256 (raw file bytes)")
    buffers = study["producer_buffers"]
    validation_hash = hashlib.sha256(
        json.dumps(study, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()
    key = (
        str(mask_path),
        mask_hash,
        str(member_path),
        hashlib.sha256(payload).hexdigest(),
        crs,
        registry["mask_revision"],
        json.dumps(buffers, sort_keys=True),
        validation_hash,
    )
    cached = _SUPPORT_CACHE.get(key)
    if cached is not None:
        if not box(*study["reporting_bbox_wgs84"]).covers(cached.reporting_water):
            raise ValueError("Reporting mask lies outside declared acquisition envelope")
        if cached.identity["study_validation_sha256"] != validation_hash:
            raise ValueError("Cached reporting support has stale study validation provenance")
        return cached
    water = read_polygon(mask_path)
    if file_sha256(mask_path) != mask_hash:
        raise ValueError("Reporting mask changed while being read")
    if reporting_cells_for_geometry(water, 7) != cells:
        raise ValueError("Reporting membership does not equal positive-area mask intersections")
    if not box(*study["reporting_bbox_wgs84"]).covers(water):
        raise ValueError("Reporting mask lies outside declared acquisition envelope")
    reporting, source, native = support_polygons_for_reporting_geometry(
        water, crs, buffers["line_of_sight_m"], buffers["aoi_margin_m"]
    )
    identity = {
        "interface_version": 1,
        "study_validation_sha256": validation_hash,
        "mask_revision": registry["mask_revision"],
        "mask_sha256": mask_hash,
        "mask_hash_policy": "sha256_exact_file_bytes",
        "membership": copy.deepcopy(entry),
        "membership_file_sha256": hashlib.sha256(payload).hexdigest(),
        "role": "water_reporting",
        "h3_version": h3.__version__,
        "area_engine": "pyproj.Geod WGS84 ellipsoid; positive polygon area",
        "producer_buffers": copy.deepcopy(buffers),
        "source_extent_wkb_sha256": hashlib.sha256(source.wkb).hexdigest(),
        "native_extent_wkb_sha256": hashlib.sha256(native.wkb).hexdigest(),
        "projected_crs": crs,
    }
    support = ReportingSupport(mask_path, member_path, cells, reporting, source, native, identity)
    _SUPPORT_CACHE[key] = support
    return support


def validate_native_path_coverage(raw: dict[str, Any], crs: str, *, canopy: bool) -> dict[str, Any]:
    """Read-only raster/source qualification before shared physical execution.

    Source dates and references must be explicit native raster metadata; unknown
    historical inputs cannot acquire qualification from a configuration date.
    """
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask

    support = load_reporting_support(raw, crs)
    native_water_path = Path(raw["paths"]["water_polygon_path"])
    native_water = read_polygon(native_water_path)
    if not native_water.covers(support.reporting_water):
        raise ValueError("Native source-water geometry does not cover reporting water")
    land_path = Path(raw["paths"]["land_polygon_path"])
    mapped_land = read_polygon(land_path)
    if not mapped_land.union(native_water).covers(support.native_extent):
        raise ValueError("Native land/water geometry does not cover complete path extent")
    land = mapped_land.intersection(support.native_extent)
    paths = {"dem": Path(raw["paths"]["regional_dem_path"])}
    if canopy:
        paths["chm"] = Path(raw["paths"]["canopy_height_path"])
    result: dict[str, Any] = {"reporting": support.identity, "native_rasters": {}}
    for name, path in paths.items():
        with rasterio.open(path) as raster:
            if raster.crs is None:
                raise ValueError(f"Native {name} raster has no CRS")
            native = gpd.GeoSeries([support.native_extent], crs=4326).to_crs(raster.crs).iloc[0]
            footprint = Polygon(
                [
                    raster.transform * point
                    for point in (
                        (0, 0),
                        (raster.width, 0),
                        (raster.width, raster.height),
                        (0, raster.height),
                    )
                ]
            )
            if footprint.is_empty or not footprint.is_valid:
                raise ValueError(f"Native {name} raster has an invalid affine footprint")
            uncovered = native.difference(footprint)
            if not footprint.covers(native):
                raise ValueError(
                    f"Native {name} raster does not cover complete paths plus AOI margin; "
                    f"uncovered area in raster CRS squared units={uncovered.area}"
                )
            tags = raster.tags()
            if not tags.get("source_date"):
                raise ValueError(f"Native {name} raster lacks explicit source_date qualification")
            date.fromisoformat(tags["source_date"])
            reference = tags.get("vertical_reference" if name == "dem" else "height_reference")
            if not reference or reference.lower() in {"unknown", "unverified"}:
                raise ValueError(f"Native {name} raster lacks qualified vertical/height reference")
            if name == "chm" and reference != "above_ground":
                raise ValueError("Canopy height_reference must be above_ground")
            units = tags.get("vertical_units" if name == "dem" else "height_units")
            if units != "m":
                raise ValueError(f"Native {name} heights must declare metre units")
            projected_land = gpd.GeoSeries([land], crs=4326).to_crs(raster.crs).iloc[0]
            valid_land_pixels = 0
            missing_land_pixels = 0
            for _, window in raster.block_windows(1):
                window_transform = raster.window_transform(window)
                block_footprint = Polygon(
                    [
                        window_transform * point
                        for point in (
                            (0, 0),
                            (window.width, 0),
                            (window.width, window.height),
                            (0, window.height),
                        )
                    ]
                )
                if projected_land.is_empty or not projected_land.intersects(block_footprint):
                    continue
                values = raster.read(1, window=window, masked=True)
                active = geometry_mask(
                    [mapping(projected_land)],
                    out_shape=values.shape,
                    transform=raster.window_transform(window),
                    invert=True,
                    all_touched=True,
                )
                valid = ~np.ma.getmaskarray(values) & np.isfinite(values.data)
                if (
                    name == "chm"
                    and raw.get("datasets", {}).get("chm", {}).get("provider")
                    == "global_canopy_height"
                ):
                    valid &= values.data != 255
                valid_land_pixels += int(np.count_nonzero(active & valid))
                missing_land_pixels += int(np.count_nonzero(active & ~valid))
            if missing_land_pixels:
                raise ValueError(
                    f"Native {name} source coverage has missing land pixels; qualification required"
                )
            result["native_rasters"][name] = {
                "raw_sha256": file_sha256(path),
                "crs": raster.crs.to_string(),
                "source_date": tags["source_date"],
                "reference": reference,
                "valid_land_pixels": valid_land_pixels,
                "missing_land_pixels": missing_land_pixels,
                "required_land_pixels": valid_land_pixels + missing_land_pixels,
                "native_path_coverage": "complete",
                "affine_footprint_wkb_sha256": hashlib.sha256(footprint.wkb).hexdigest(),
                "uncovered_path_area_raster_crs_squared_units": float(uncovered.area),
            }
    result["native_water_sha256"] = file_sha256(native_water_path)
    result["native_land_sha256"] = file_sha256(land_path)
    return result
