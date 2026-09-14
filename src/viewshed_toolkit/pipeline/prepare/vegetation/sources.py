from __future__ import annotations

import json
import logging
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from viewshed_toolkit._internal.config.paths import project_root

from ...config import bbox_from_config, canopy_config_with_defaults

CRS_WGS84 = "EPSG:4326"
REPO_ROOT = project_root()
VIEWSHED_ROOT = REPO_ROOT
AWS_ALLOWED_EXTENSIONS = ".tif,.tiff,.json,.geojson"
DEFAULT_VEGETATION_BBOX_BUFFER_DEG = 0.02
DEFAULT_WORLDCOVER_S3_URI = "s3://esa-worldcover/v200/2021/map"
LOGGER = logging.getLogger(__name__)


from .assets import (
    _ETH_TILE_RE,
    ETH_DOWNLOAD_BASE_URL,
    ETH_TILE_INDEX_URL,
    ETH_TILE_SIZE_DEG,
    aligned_raster_is_valid,
    bboxes_intersect,
    build_or_reuse_vrt,
    clip_cogs_to_bbox,
    profile_timer,
    raster_covers_bbox,
    read_tile_manifest,
    resolve_path,
    valid_raster,
    write_tile_manifest,
)


@dataclass(frozen=True)
class CanopyTile:
    tile_id: str
    chm_url: str
    std_url: str | None
    local_chm_path: Path
    local_std_path: Path | None
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class CanopyHeightResult:
    output_path: Path
    output_projected_path: Path | None
    output_aligned_to_dem_path: Path | None
    metadata_path: Path
    selected_assets: list[str]
    bbox_wgs84: tuple[float, float, float, float]
    raw_tile_paths: list[Path]
    mosaic_path: Path | None = None
    obstruction_path: Path | None = None
    std_output_path: Path | None = None
    std_aligned_to_dem_path: Path | None = None
    output_10m_path: Path | None = None
    output_30m_path: Path | None = None


def validate_canopy_resampling(resampling: str) -> str:
    normalized = (resampling or "max").lower()
    if normalized not in {"bilinear", "average", "max"}:
        raise ValueError(
            "Canopy resampling must be one of 'bilinear', 'average', or 'max'; "
            f"got {resampling!r}."
        )
    return normalized


def load_project_bbox(raw: dict[str, Any]) -> tuple[float, float, float, float]:
    canopy = raw.get("canopy", {})
    buffer_deg = float(
        canopy.get(
            "bbox_buffer_deg",
            raw.get("vegetation_data", {}).get(
                "bbox_buffer_deg",
                DEFAULT_VEGETATION_BBOX_BUFFER_DEG,
            ),
        )
    )
    return bbox_from_config(raw, buffer_deg)


def existing_canopy_30m_is_valid(
    chm_30m_path: Path,
    dem_30m_path: Path,
    bbox_wgs84: tuple[float, float, float, float],
) -> bool:
    return raster_covers_bbox(dem_30m_path, bbox_wgs84) and aligned_raster_is_valid(
        chm_30m_path,
        dem_30m_path,
        acceptable_dtypes={"float32"},
        require_nodata=True,
    )


def _format_eth_tile_id(lat_south: int, lon_west: int) -> str:
    ns = "N" if lat_south >= 0 else "S"
    ew = "E" if lon_west >= 0 else "W"
    return f"{ns}{abs(lat_south):02d}{ew}{abs(lon_west):03d}"


def _parse_eth_tile_id(tile_id: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?P<ns>[NS])(?P<lat>\d{2})(?P<ew>[EW])(?P<lon>\d{3})", tile_id)
    if not match:
        raise ValueError(f"Invalid ETH canopy tile id: {tile_id}")
    lat = int(match.group("lat")) * (1 if match.group("ns") == "N" else -1)
    lon = int(match.group("lon")) * (1 if match.group("ew") == "E" else -1)
    return lat, lon


def eth_tile_bounds(tile_id: str) -> tuple[float, float, float, float]:
    lat_south, lon_west = _parse_eth_tile_id(tile_id)
    return (
        float(lon_west),
        float(lat_south),
        float(lon_west + ETH_TILE_SIZE_DEG),
        float(lat_south + ETH_TILE_SIZE_DEG),
    )


def eth_chm_filename(tile_id: str) -> str:
    return f"ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map.tif"


def eth_std_filename(tile_id: str) -> str:
    return f"ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map_SD.tif"


def eth_download_canopy_url(filename: str) -> str:
    return f"{ETH_DOWNLOAD_BASE_URL}{filename}"


def _tile_id_from_filename(name: str) -> str | None:
    match = _ETH_TILE_RE.search(Path(name).name)
    return match.group("tile") if match else None


def deterministic_eth_tile_ids_for_bbox(
    bbox: tuple[float, float, float, float],
) -> list[str]:
    min_lon, min_lat, max_lon, max_lat = bbox
    lon_start = math.floor(min_lon / ETH_TILE_SIZE_DEG) * ETH_TILE_SIZE_DEG
    lon_end = math.floor((max_lon - 1e-12) / ETH_TILE_SIZE_DEG) * ETH_TILE_SIZE_DEG
    lat_south_start = math.floor(min_lat / ETH_TILE_SIZE_DEG) * ETH_TILE_SIZE_DEG
    lat_south_end = math.floor((max_lat - 1e-12) / ETH_TILE_SIZE_DEG) * ETH_TILE_SIZE_DEG

    tile_ids: list[str] = []
    for lat_south in range(lat_south_start, lat_south_end + ETH_TILE_SIZE_DEG, ETH_TILE_SIZE_DEG):
        for lon_west in range(lon_start, lon_end + ETH_TILE_SIZE_DEG, ETH_TILE_SIZE_DEG):
            tile_id = _format_eth_tile_id(lat_south, lon_west)
            if bboxes_intersect(eth_tile_bounds(tile_id), bbox):
                tile_ids.append(tile_id)
    return sorted(tile_ids)


def _read_tile_index_html(tile_index_url: str) -> str:
    with urllib.request.urlopen(tile_index_url, timeout=60) as response:
        return response.read().decode("utf-8")


def _extract_tile_index_records(html: str) -> list[dict[str, Any]]:
    marker = "_add("
    start = html.find(marker)
    if start < 0:
        return []
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(html[start + len(marker) :])
    records = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        tile_name = props.get("tile_name") or props.get("location")
        tile_id = _tile_id_from_filename(str(tile_name))
        if not tile_id:
            continue
        bounds = feature.get("bbox") or eth_tile_bounds(tile_id)
        chm_url = props.get("href_canopy_height") or eth_download_canopy_url(
            eth_chm_filename(tile_id)
        )
        std_html = props.get("html_std") or ""
        std_match = re.search(r"href=([^>]+)", str(std_html))
        std_url = (
            std_match.group(1) if std_match else eth_download_canopy_url(eth_std_filename(tile_id))
        )
        records.append(
            {
                "tile_id": tile_id,
                "chm_url": chm_url,
                "std_url": std_url,
                "bounds": tuple(float(v) for v in bounds),
            }
        )
    return records


def _fallback_eth_tile_records(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    return [
        {
            "tile_id": tile_id,
            "chm_url": eth_download_canopy_url(eth_chm_filename(tile_id)),
            "std_url": eth_download_canopy_url(eth_std_filename(tile_id)),
            "bounds": eth_tile_bounds(tile_id),
        }
        for tile_id in deterministic_eth_tile_ids_for_bbox(bbox)
    ]


def _canopy_tiles_from_records(
    records: Sequence[dict[str, Any]], raw_dir: Path, *, download_std: bool
) -> list[CanopyTile]:
    selected = []
    for rec in records:
        bounds = tuple(float(v) for v in rec["bounds"])
        tile_id = str(rec["tile_id"])
        local_chm_path = raw_dir / eth_chm_filename(tile_id)
        local_std_path = raw_dir / eth_std_filename(tile_id) if download_std else None
        selected.append(
            CanopyTile(
                tile_id=tile_id,
                chm_url=str(rec["chm_url"]),
                std_url=(str(rec.get("std_url")) if rec.get("std_url") and download_std else None),
                local_chm_path=local_chm_path,
                local_std_path=local_std_path,
                bounds=bounds,
            )
        )
    return sorted(selected, key=lambda tile: tile.tile_id)


def discover_eth_canopy_tiles(
    bbox: tuple[float, float, float, float],
    config: dict[str, Any],
    *,
    config_dir: Path,
    dry_run: bool = False,
    overwrite_manifest: bool = False,
) -> list[CanopyTile]:
    config = canopy_config_with_defaults(config)
    raw_dir = resolve_path(config["raw_dir"], config_dir)
    download_std = bool(config.get("download_std_layer", False))
    tile_index_url = str(config.get("tile_index_url", ETH_TILE_INDEX_URL))
    manifest_path = resolve_path(
        config["tile_manifest_path"],
        config_dir,
    )
    source = f"eth_global_canopy_height_2020:{tile_index_url}:std={download_std}"

    if not overwrite_manifest:
        cached = read_tile_manifest(manifest_path, bbox_wgs84=bbox, source=source)
        if cached is not None:
            LOGGER.info("Reusing CHM tile manifest: %s", manifest_path)
            return _canopy_tiles_from_records(cached, raw_dir, download_std=download_std)

    local_records = _fallback_eth_tile_records(bbox)
    local_tiles = _canopy_tiles_from_records(local_records, raw_dir, download_std=download_std)
    required_local_paths = [tile.local_chm_path for tile in local_tiles]
    if download_std:
        required_local_paths.extend(
            tile.local_std_path for tile in local_tiles if tile.local_std_path is not None
        )
    if required_local_paths and all(valid_raster(path) for path in required_local_paths):
        LOGGER.info("Using %d complete local CHM tiles without a network lookup", len(local_tiles))
        if not dry_run:
            write_tile_manifest(
                manifest_path,
                bbox_wgs84=bbox,
                source=source,
                selected_tiles=local_records,
                notes="Deterministic ETH tile selection verified from complete local rasters.",
            )
        return local_tiles

    records: list[dict[str, Any]]
    with profile_timer("discover_chm_tiles", bbox=bbox):
        try:
            records = _extract_tile_index_records(_read_tile_index_html(tile_index_url))
            if not records:
                raise RuntimeError("ETH tile index did not contain parseable tile records.")
        except Exception as exc:
            print(
                f"WARNING: ETH canopy tile index read failed; using deterministic 3-degree fallback: {exc}"
            )
            records = _fallback_eth_tile_records(bbox)

    selected_records = []
    for rec in records:
        bounds = tuple(float(v) for v in rec["bounds"])
        if not bboxes_intersect(bounds, bbox):
            continue
        selected_records.append(
            {
                "tile_id": str(rec["tile_id"]),
                "chm_url": str(rec["chm_url"]),
                "std_url": (
                    str(rec.get("std_url")) if rec.get("std_url") and download_std else None
                ),
                "bounds": bounds,
            }
        )

    selected = _canopy_tiles_from_records(selected_records, raw_dir, download_std=download_std)
    if not selected and not dry_run:
        raise RuntimeError(f"No ETH canopy tiles selected for bbox {bbox}")
    if not dry_run:
        write_tile_manifest(
            manifest_path,
            bbox_wgs84=bbox,
            source=source,
            selected_tiles=selected_records,
            notes="ETH Global Canopy Height selected tiles for configured bbox.",
        )
    LOGGER.info("Selected %d CHM tiles", len(selected))
    return sorted(selected, key=lambda tile: tile.tile_id)


def _download_canopy_url(
    url: str, out_path: Path, *, overwrite: bool = False, chunk_size: int = 1024 * 1024
) -> Path:
    if out_path.exists() and out_path.stat().st_size > 0 and not overwrite:
        if valid_raster(out_path):
            print(f"Cache hit -> {out_path}")
            return out_path
        print(f"WARNING: cached tile is unreadable; redownloading -> {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    if part_path.exists():
        part_path.unlink()

    print(f"Download -> {out_path.name}")
    with urllib.request.urlopen(url, timeout=60) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"HTTP {status} downloading {url}")
        total_header = (
            response.headers.get("Content-Length") if hasattr(response, "headers") else None
        )
        total = int(total_header) if total_header and total_header.isdigit() else None
        try:
            from tqdm.auto import tqdm  # type: ignore
        except Exception:
            tqdm = None
        progress = (
            tqdm(total=total, unit="B", unit_scale=True, desc=out_path.name)
            if tqdm is not None
            else None
        )
        try:
            with part_path.open("wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    if progress is not None:
                        progress.update(len(chunk))
        finally:
            if progress is not None:
                progress.close()
    part_path.replace(out_path)
    return out_path


def download_eth_canopy_tiles(
    tiles: Sequence[CanopyTile], raw_dir: Path | None = None, overwrite: bool = False
) -> list[Path]:
    paths = []
    already_local = 0
    downloaded = 0
    for tile in tiles:
        existed = (
            tile.local_chm_path.exists()
            and tile.local_chm_path.stat().st_size > 0
            and valid_raster(tile.local_chm_path)
        )
        paths.append(_download_canopy_url(tile.chm_url, tile.local_chm_path, overwrite=overwrite))
        already_local += int(existed and not overwrite)
        downloaded += int(not (existed and not overwrite))
        if tile.std_url and tile.local_std_path:
            _download_canopy_url(tile.std_url, tile.local_std_path, overwrite=overwrite)
    LOGGER.info(
        "CHM tile download summary selected=%d already_local=%d downloaded=%d failed=0",
        len(tiles),
        already_local,
        downloaded,
    )
    return paths


def build_canopy_mosaic(
    tile_paths: Sequence[Path], output_vrt_or_tif: Path, *, overwrite: bool = False
) -> Path:
    if output_vrt_or_tif.exists() and not overwrite:
        return output_vrt_or_tif
    if not tile_paths:
        raise ValueError("No canopy tile paths supplied.")
    output_vrt_or_tif.parent.mkdir(parents=True, exist_ok=True)
    if output_vrt_or_tif.suffix.lower() == ".vrt":
        return build_or_reuse_vrt(tile_paths, output_vrt_or_tif, overwrite=overwrite)
    clip_cogs_to_bbox(
        [str(path) for path in tile_paths],
        (-180, -90, 180, 90),
        output_vrt_or_tif,
        dst_crs="EPSG:4326",
        overwrite=overwrite,
    )
    return output_vrt_or_tif


def ensure_canopy_nodata(path: Path, nodata: int | float | None = 255) -> None:
    if nodata is None or not path.exists():
        return
    import rasterio  # type: ignore

    with rasterio.open(path, "r+") as dst:
        dst.nodata = nodata


def write_obstruction_raster(
    canopy_path: Path,
    output_path: Path,
    *,
    thresholds_m: dict[str, float],
    values: dict[str, float],
    overwrite: bool = False,
) -> Path:
    if valid_raster(output_path) and not overwrite:
        return output_path
    import numpy as np  # type: ignore
    import rasterio  # type: ignore

    low = float(thresholds_m.get("low", 2))
    medium = float(thresholds_m.get("medium", 5))
    high = float(thresholds_m.get("high", 15))
    none_v = float(values.get("none", 0.0))
    low_v = float(values.get("low", 0.25))
    medium_v = float(values.get("medium", 0.65))
    high_v = float(values.get("high", 1.0))
    nodata = -9999.0

    with rasterio.open(canopy_path) as src:
        profile = src.profile.copy()
        profile.update(
            dtype="float32",
            nodata=nodata,
            compress="deflate",
            tiled=True,
            BIGTIFF="IF_SAFER",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        with profile_timer("derive_chm_obstruction", source=canopy_path, output=output_path):
            with rasterio.open(output_path, "w", **profile) as dst:
                for _, window in src.block_windows(1):
                    canopy = src.read(1, window=window, masked=True)
                    obstruction = np.full(canopy.shape, none_v, dtype="float32")
                    obstruction[(canopy >= low) & (canopy < medium)] = low_v
                    obstruction[(canopy >= medium) & (canopy < high)] = medium_v
                    obstruction[canopy >= high] = high_v
                    obstruction = np.where(canopy.mask, nodata, obstruction).astype("float32")
                    dst.write(obstruction, 1, window=window)
    return output_path


def _canopy_config(raw: dict[str, Any]) -> dict[str, Any]:
    vegetation_data = raw.get("vegetation_data", {}) or {}
    nested = vegetation_data.get("canopy", {}) if isinstance(vegetation_data, dict) else {}
    top_level = raw.get("canopy", {}) or {}
    if not isinstance(nested, dict) or not isinstance(top_level, dict):
        raise ValueError("Canopy configuration sections must be mappings.")
    cfg = {**nested, **top_level}
    if not cfg:
        raise ValueError("Missing required vegetation_data.canopy config section.")
    cfg.setdefault(
        "resampling",
        (raw.get("viewshed", {}) or {}).get("canopy_resampling", "max"),
    )
    if cfg.get("enabled", True) is False:
        raise ValueError("canopy.enabled is false")
    provider = cfg.get("provider", "eth_global_canopy_height_2020")
    if provider != "eth_global_canopy_height_2020":
        raise ValueError(f"Unsupported canopy.provider: {provider}")
    return cfg


def _print_canopy_summary(result: CanopyHeightResult, stats: dict[str, Any] | None = None) -> None:
    print("ETH canopy height preparation summary")
    print(f"  bbox_wgs84: {result.bbox_wgs84}")
    print(f"  selected tile count: {len(result.selected_assets)}")
    for asset in result.selected_assets[:25]:
        print(f"    - {asset}")
    if len(result.selected_assets) > 25:
        print(f"    ... {len(result.selected_assets) - 25} more")
    print(f"  mosaic_path: {result.mosaic_path}")
    print(f"  output_path: {result.output_path}")
    print(f"  output_projected_path: {result.output_projected_path}")
    print(f"  output_aligned_to_dem_path: {result.output_aligned_to_dem_path}")
    print(f"  obstruction_path: {result.obstruction_path}")
    print(f"  std_output_path: {result.std_output_path}")
    print(f"  std_aligned_to_dem_path: {result.std_aligned_to_dem_path}")
    if stats:
        print(f"  stats: {stats}")
