from __future__ import annotations

import csv
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit._internal.config.paths import project_root

from ...config import (
    DEFAULT_VEGETATION_CLASS_LOOKUP_PATH,
    bbox_from_config,
    canopy_config_with_defaults,
    landcover_config_with_defaults,
)

CRS_WGS84 = "EPSG:4326"
REPO_ROOT = project_root()
VIEWSHED_ROOT = REPO_ROOT
AWS_ALLOWED_EXTENSIONS = ".tif,.tiff,.json,.geojson"
DEFAULT_VEGETATION_BBOX_BUFFER_DEG = 0.02
DEFAULT_WORLDCOVER_S3_URI = "s3://esa-worldcover/v200/2021/map"
LOGGER = logging.getLogger(__name__)


from .assets import (
    ETH_TILE_INDEX_URL,
    aligned_raster_is_valid,
    bboxes_intersect,
    build_or_reuse_vrt,
    configure_unsigned_aws,
    dem_path_for_resolution,
    dem_path_from_config,
    list_s3_objects_unsigned,
    load_raw_config,
    profile_timer,
    raster_band_stats,
    raster_covers_bbox,
    raster_profile_summary,
    read_tile_manifest,
    reproject_raster,
    resolve_path,
    valid_raster,
    validate_raster_matches_reference,
    warp_to_reference_grid,
    write_metadata_json,
    write_tile_manifest,
)
from .sources import (
    CanopyHeightResult,
    _canopy_config,
    _print_canopy_summary,
    build_canopy_mosaic,
    discover_eth_canopy_tiles,
    download_eth_canopy_tiles,
    ensure_canopy_nodata,
    existing_canopy_30m_is_valid,
    load_project_bbox,
    validate_canopy_resampling,
    write_obstruction_raster,
)


def _canopy_metadata_matches_policy(metadata_path: Path, *, resampling: str) -> bool:
    if not metadata_path.exists():
        return False
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return str(metadata.get("resampling", "")).strip().lower() == resampling


def _cleanup_intermediate_paths(
    paths: Sequence[Path | None],
    *,
    preserve: Sequence[Path | None] = (),
    stop_at: Path | None = None,
) -> None:
    preserved = {p.resolve() for p in preserve if p is not None}
    for path in paths:
        if path is None:
            continue
        resolved = path.resolve()
        if resolved in preserved:
            continue
        if path.exists() and path.is_file():
            path.unlink()
            parent = path.parent
            stop = stop_at.resolve() if stop_at else None
            while (
                parent.exists() and parent.is_dir() and (stop is None or parent.resolve() != stop)
            ):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        elif path.exists() and path.is_dir():
            stop = stop_at.resolve() if stop_at else None
            try:
                path.rmdir()
            except OSError:
                continue
            parent = path.parent
            while (
                parent.exists() and parent.is_dir() and (stop is None or parent.resolve() != stop)
            ):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def download_canopy_height_for_config(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> CanopyHeightResult:
    logging.basicConfig(level=logging.INFO)
    raw, config_dir = load_raw_config(config_path)
    cfg = _canopy_config(raw)
    cfg = canopy_config_with_defaults(cfg)
    model_bbox = bbox_from_config(raw)
    bbox = load_project_bbox(raw)
    resampling = validate_canopy_resampling(str(cfg.get("resampling", "max")))
    nodata = cfg.get("nodata", 255)

    raw_dir = resolve_path(cfg["raw_dir"], config_dir)
    processed_dir = resolve_path(cfg["processed_dir"], config_dir)
    final_dir = resolve_path(cfg["final_dir"], config_dir)
    mosaic_path = processed_dir / str(
        cfg.get("mosaic_filename", "eth_global_canopy_height_2020_10m_mosaic.vrt")
    )
    output_path = processed_dir / str(
        cfg.get("clipped_filename", "eth_global_canopy_height_2020_10m_bbox.tif")
    )
    output_aligned_path = processed_dir / str(
        cfg.get("aligned_filename", "eth_global_canopy_height_2020_10m_aligned_to_dem.tif")
    )
    obstruction_path = processed_dir / str(
        cfg.get(
            "obstruction_filename",
            "eth_global_canopy_height_2020_10m_obstruction_aligned_to_dem.tif",
        )
    )
    metadata_path = processed_dir / str(
        cfg.get("metadata_filename", "eth_global_canopy_height_2020_10m_metadata.json")
    )
    std_output_path = processed_dir / str(
        cfg.get("std_clipped_filename", "eth_global_canopy_height_2020_10m_sd_bbox.tif")
    )
    std_aligned_path = processed_dir / str(
        cfg.get(
            "std_aligned_filename",
            "eth_global_canopy_height_2020_10m_sd_aligned_to_dem.tif",
        )
    )
    output_10m_path = final_dir / str(cfg.get("chm_10m_filename", "CHM_10M.tif"))
    output_30m_path = final_dir / str(cfg.get("chm_30m_filename", "CHM_30M.tif"))
    obstruction_30m_path = final_dir / str(
        cfg.get("obstruction_30m_filename", "CHM_OBSTRUCTION_30M.tif")
    )
    dem_path = dem_path_from_config(raw, config_dir)
    dem_10m_path = dem_path_for_resolution(raw, config_dir, 10)
    dem_30m_path = dem_path_for_resolution(raw, config_dir, 30)

    metadata_matches_policy = _canopy_metadata_matches_policy(metadata_path, resampling=resampling)
    if (
        not overwrite
        and not bool(cfg.get("overwrite", False))
        # The buffered bbox controls raw tile discovery. The durable CHM is
        # aligned to the DEM, whose required coverage is the modeling bbox.
        # Requiring that smaller DEM to cover the raw-data buffer makes this
        # reuse gate fail on every otherwise valid run.
        and existing_canopy_30m_is_valid(
            output_30m_path,
            dem_30m_path,
            model_bbox,
        )
        and metadata_matches_policy
    ):
        final = CanopyHeightResult(
            output_30m_path,
            None,
            output_30m_path,
            metadata_path,
            [],
            bbox,
            [],
            mosaic_path=mosaic_path,
            obstruction_path=(obstruction_30m_path if obstruction_30m_path.exists() else None),
            std_output_path=std_output_path if std_output_path.exists() else None,
            std_aligned_to_dem_path=(std_aligned_path if std_aligned_path.exists() else None),
            output_10m_path=output_10m_path if output_10m_path.exists() else None,
            output_30m_path=output_30m_path,
        )
        if dry_run:
            print("Dry run only; existing CHM_30M.tif can be reused and no files will be written.")
            _print_canopy_summary(final)
            return final
        stats = raster_band_stats(output_30m_path, categorical=False)
        # The existing metadata is part of the durable source lineage. Reuse
        # must not replace its selected assets and checksums with empty lists.
        ensure_canopy_nodata(output_30m_path, nodata)
        print(f"Reusing existing CHM_30M.tif -> {output_30m_path}")
        _print_canopy_summary(final, stats)
        if bool(cfg.get("cleanup_intermediate_dirs", True)):
            _cleanup_intermediate_paths(
                [
                    mosaic_path,
                    output_path,
                    output_aligned_path,
                    obstruction_path,
                    std_output_path,
                    std_aligned_path,
                    processed_dir / "eth_global_canopy_height_2020_10m_sd_mosaic.vrt",
                ],
                preserve=[
                    metadata_path,
                    output_10m_path,
                    output_30m_path,
                    obstruction_30m_path,
                ],
                stop_at=resolve_path("data", config_dir),
            )
        return final

    tiles = discover_eth_canopy_tiles(
        bbox,
        cfg,
        config_dir=config_dir,
        dry_run=dry_run,
        overwrite_manifest=bool(overwrite or cfg.get("refresh_manifest", False)),
    )
    selected_urls = [tile.chm_url for tile in tiles]
    result = CanopyHeightResult(
        output_30m_path,
        None,
        output_30m_path,
        metadata_path,
        selected_urls,
        bbox,
        [tile.local_chm_path for tile in tiles],
        mosaic_path=mosaic_path,
        output_10m_path=output_10m_path,
        output_30m_path=output_30m_path,
    )
    if dry_run:
        print("Dry run only; no ETH canopy rasters will be downloaded or written.")
        _print_canopy_summary(result)
        return result

    final_overwrite = bool(overwrite or cfg.get("overwrite", False) or not metadata_matches_policy)
    download_overwrite = bool(cfg.get("overwrite_downloads", False))
    raw_paths = download_eth_canopy_tiles(tiles, raw_dir, overwrite=download_overwrite)
    build_canopy_mosaic(raw_paths, mosaic_path, overwrite=final_overwrite)

    aligned_written = None
    obstruction_written = None
    if dem_10m_path.exists():
        warp_to_reference_grid(
            mosaic_path,
            dem_10m_path,
            output_10m_path,
            resampling=resampling,
            dtype="float32",
            nodata=nodata,
            overwrite=final_overwrite,
        )
        ensure_canopy_nodata(output_10m_path, nodata)
    else:
        print(f"WARNING: DEM_10M not found; skipped CHM_10M alignment: {dem_10m_path}")
    if dem_30m_path.exists():
        warp_to_reference_grid(
            mosaic_path,
            dem_30m_path,
            output_30m_path,
            resampling=resampling,
            dtype="float32",
            nodata=nodata,
            overwrite=final_overwrite,
        )
        ensure_canopy_nodata(output_30m_path, nodata)
        validate_raster_matches_reference(
            output_30m_path,
            dem_30m_path,
            acceptable_dtypes={"float32"},
            require_nodata=True,
        )
        aligned_written = output_30m_path
        if output_aligned_path != output_30m_path:
            output_aligned_path = output_30m_path
        veg_cfg = raw.get("vegetation", {})
        write_obstruction_raster(
            output_30m_path,
            obstruction_30m_path,
            thresholds_m=veg_cfg.get("obstruction_thresholds_m", {}),
            values=veg_cfg.get("obstruction_values", {}),
            overwrite=final_overwrite,
        )
        obstruction_written = obstruction_30m_path
    else:
        print(f"WARNING: DEM_30M not found; skipped CHM_30M alignment: {dem_30m_path}")

    std_written = None
    std_aligned_written = None
    if bool(cfg.get("download_std_layer", False)):
        std_paths = [tile.local_std_path for tile in tiles if tile.local_std_path]
        if std_paths:
            std_vrt_path = processed_dir / "eth_global_canopy_height_2020_10m_sd_mosaic.vrt"
            build_or_reuse_vrt(std_paths, std_vrt_path, overwrite=final_overwrite)
            std_written = std_vrt_path
            if dem_path.exists():
                warp_to_reference_grid(
                    std_vrt_path,
                    dem_path,
                    std_aligned_path,
                    resampling=resampling,
                    dtype="float32",
                    nodata=nodata,
                    overwrite=final_overwrite,
                )
                ensure_canopy_nodata(std_aligned_path, nodata)
                std_aligned_written = std_aligned_path

    stats = (
        raster_band_stats(output_30m_path, categorical=False) if output_30m_path.exists() else None
    )
    metadata = {
        "source_name": "ETH Global Canopy Height 2020",
        "provider": cfg.get("provider"),
        "product_year": cfg.get("product_year", 2020),
        "resolution_m": cfg.get("resolution_m", 10),
        "tile_index_url": cfg.get("tile_index_url", ETH_TILE_INDEX_URL),
        "bbox_wgs84": bbox,
        "selected_tiles": [tile.tile_id for tile in tiles],
        "selected_assets": selected_urls,
        "raw_tile_paths": raw_paths,
        "raw_tile_checksums": {str(path): checksum_path(path) for path in raw_paths},
        "download_std_layer": bool(cfg.get("download_std_layer", False)),
        "nodata": nodata,
        "resampling": resampling,
        "output_paths": {
            "mosaic": mosaic_path,
            "clip": None,
            "chm_10m": output_10m_path if output_10m_path.exists() else None,
            "chm_30m": output_30m_path if output_30m_path.exists() else None,
            "aligned_to_dem": aligned_written,
            "obstruction": obstruction_written,
            "std_clip": std_written,
            "std_aligned_to_dem": std_aligned_written,
        },
        "config_path": str(config_path),
        "raster_profile": raster_profile_summary(output_30m_path),
        "raster_stats": stats,
    }
    write_metadata_json(metadata_path, metadata)
    final = CanopyHeightResult(
        output_30m_path,
        None,
        aligned_written,
        metadata_path,
        selected_urls,
        bbox,
        raw_paths,
        mosaic_path=mosaic_path,
        obstruction_path=obstruction_written,
        std_output_path=std_written,
        std_aligned_to_dem_path=std_aligned_written,
        output_10m_path=output_10m_path if output_10m_path.exists() else None,
        output_30m_path=output_30m_path if output_30m_path.exists() else None,
    )
    _print_canopy_summary(final, stats)
    if bool(cfg.get("cleanup_intermediate_dirs", True)):
        _cleanup_intermediate_paths(
            [
                mosaic_path,
                output_path,
                output_aligned_path,
                obstruction_path,
                std_output_path,
                std_aligned_path,
                processed_dir / "eth_global_canopy_height_2020_10m_sd_mosaic.vrt",
            ],
            preserve=[
                metadata_path,
                output_10m_path,
                output_30m_path,
                obstruction_30m_path,
            ],
            stop_at=resolve_path("data", config_dir),
        )
    return final


# -----------------------------------------------------------------------------
# ESA WorldCover landcover preparation
# -----------------------------------------------------------------------------

ESA_CLASS_ROWS = [
    (10, "Tree cover", "tree", "evergreen_or_mixed_unknown", "high"),
    (20, "Shrubland", "shrub", "seasonal_medium", "medium"),
    (30, "Grassland", "grass", "low", "low"),
    (40, "Cropland", "crop", "seasonal_medium", "medium"),
    (50, "Built-up", "built", "built_not_vegetation", "separate_building_model"),
    (60, "Bare / sparse vegetation", "bare", "low", "low"),
    (70, "Snow and ice", "snow", "low", "low"),
    (80, "Permanent water bodies", "water", "none", "none"),
    (90, "Herbaceous wetland", "wetland", "seasonal_medium", "medium"),
    (95, "Mangroves", "mangrove", "evergreen_high", "high"),
    (100, "Moss and lichen", "moss_lichen", "low", "low"),
]
ESA_CLASS_VALUES = {row[0] for row in ESA_CLASS_ROWS}
_TILE_RE = re.compile(r"(?P<ns>[NS])(?P<lat>\d{2})(?P<ew>[EW])(?P<lon>\d{3})", re.IGNORECASE)


@dataclass(frozen=True)
class LandcoverResult:
    output_path: Path
    output_projected_path: Path | None
    output_aligned_to_dem_path: Path | None
    class_lookup_path: Path
    metadata_path: Path
    selected_assets: list[str]
    bbox_wgs84: tuple[float, float, float, float]
    output_10m_path: Path | None = None
    output_30m_path: Path | None = None


def validate_landcover_resampling(resampling: str) -> str:
    if (resampling or "nearest").lower() != "nearest":
        raise ValueError("ESA WorldCover is categorical; landcover resampling must be nearest.")
    return "nearest"


def write_esa_class_lookup(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["value", "class_name", "vegetation_group", "leaf_behavior", "vegetation_relevance"]
        )
        writer.writerows(ESA_CLASS_ROWS)
    return path


def existing_landcover_30m_is_valid(
    landcover_30m_path: Path, dem_30m_path: Path, bbox_wgs84: tuple[float, float, float, float]
) -> bool:
    return raster_covers_bbox(dem_30m_path, bbox_wgs84) and aligned_raster_is_valid(
        landcover_30m_path,
        dem_30m_path,
        acceptable_dtypes={"uint8", "uint16"},
        require_nodata=True,
    )


def _tile_bbox_from_name(key: str) -> tuple[float, float, float, float] | None:
    match = _TILE_RE.search(Path(key).name)
    if not match:
        return None
    lat = int(match.group("lat")) * (1 if match.group("ns").upper() == "N" else -1)
    lon = int(match.group("lon")) * (1 if match.group("ew").upper() == "E" else -1)
    # ESA WorldCover map COGs are 3x3 degree tiles named by their south-west
    # corner. For this bbox, N45/N48 tiles are needed; treating the latitude as
    # a north edge incorrectly shifts selection to N48/N51 and leaves the
    # southern landcover raster as nodata.
    return (lon, lat, lon + 3, lat + 3)


def _cache_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(records).to_parquet(path, index=False)
    except Exception:
        path.write_text(json.dumps(records, indent=2))


def _read_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        import pandas as pd  # type: ignore

        return pd.read_parquet(path).to_dict("records")
    except Exception:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None


def discover_worldcover_assets(
    s3_uri: str,
    bbox_wgs84: tuple[float, float, float, float],
    cache_path: Path,
    overwrite_cache: bool = False,
    manifest_path: Path | None = None,
    write_manifest: bool = True,
) -> list[dict[str, Any]]:
    if manifest_path is not None and not overwrite_cache:
        cached_selected = read_tile_manifest(manifest_path, bbox_wgs84=bbox_wgs84, source=s3_uri)
        if cached_selected is not None:
            LOGGER.info("Reusing landcover tile manifest: %s", manifest_path)
            return cached_selected

    records = None if overwrite_cache else _read_cache(cache_path)
    if records is None:
        with profile_timer("discover_landcover_tiles", source=s3_uri):
            objects = list_s3_objects_unsigned(s3_uri, suffixes=(".tif", ".tiff"))
            records = []
            for obj in objects:
                bounds = _tile_bbox_from_name(obj["key"])
                records.append(
                    {
                        **obj,
                        "s3_uri": s3_uri,
                        "min_lon": bounds[0] if bounds else None,
                        "min_lat": bounds[1] if bounds else None,
                        "max_lon": bounds[2] if bounds else None,
                        "max_lat": bounds[3] if bounds else None,
                    }
                )
            _cache_records(cache_path, records)
    selected = []
    for rec in records:
        bounds = (rec.get("min_lon"), rec.get("min_lat"), rec.get("max_lon"), rec.get("max_lat"))
        if any(v is None for v in bounds):
            continue
        if bboxes_intersect(tuple(float(v) for v in bounds), bbox_wgs84):
            selected.append(rec)
    if manifest_path is not None and write_manifest:
        write_tile_manifest(
            manifest_path,
            bbox_wgs84=bbox_wgs84,
            source=s3_uri,
            selected_tiles=selected,
            notes="ESA WorldCover selected tiles for configured bbox.",
        )
    LOGGER.info("Selected %d landcover tiles", len(selected))
    return selected


def _landcover_asset_url(record: dict[str, Any]) -> str:
    url = str(record.get("url") or "")
    if url.startswith("s3://"):
        parsed = urllib.parse.urlparse(url)
        return f"https://{parsed.netloc}.s3.amazonaws.com/{parsed.path.lstrip('/')}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    key = str(record.get("key") or "")
    if key:
        bucket = urllib.parse.urlparse(str(record.get("s3_uri") or "")).netloc
        if not bucket:
            # ESA WorldCover config currently uses a fixed bucket. Keep this
            # fallback narrow instead of guessing for arbitrary S3 sources.
            bucket = "esa-worldcover"
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    raise ValueError(f"Cannot determine download URL for landcover record: {record}")


def _landcover_local_path(record: dict[str, Any], raw_dir: Path) -> Path:
    key_or_url = str(record.get("key") or record.get("url") or "")
    name = Path(urllib.parse.urlparse(key_or_url).path).name or Path(key_or_url).name
    if not name:
        raise ValueError(f"Cannot determine local filename for landcover record: {record}")
    return raw_dir / name


def _download_landcover_url(
    url: str, out_path: Path, *, overwrite: bool = False, chunk_size: int = 1024 * 1024
) -> Path:
    if out_path.exists() and out_path.stat().st_size > 0 and not overwrite:
        if valid_raster(out_path):
            print(f"Cache hit -> {out_path}")
            return out_path
        print(f"WARNING: cached landcover tile is unreadable; redownloading -> {out_path}")
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


def download_worldcover_assets(
    records: Sequence[dict[str, Any]], raw_dir: Path, *, overwrite: bool = False
) -> list[Path]:
    paths: list[Path] = []
    already_local = 0
    downloaded = 0
    for record in records:
        local_path = _landcover_local_path(record, raw_dir)
        existed = local_path.exists() and local_path.stat().st_size > 0 and valid_raster(local_path)
        paths.append(
            _download_landcover_url(_landcover_asset_url(record), local_path, overwrite=overwrite)
        )
        already_local += int(existed and not overwrite)
        downloaded += int(not (existed and not overwrite))
    LOGGER.info(
        "Landcover tile download summary selected=%d already_local=%d downloaded=%d failed=0",
        len(records),
        already_local,
        downloaded,
    )
    return paths


def _print_landcover_summary(result: LandcoverResult, stats: dict[str, Any] | None = None) -> None:
    print("Landcover preparation summary")
    print(f"  bbox_wgs84: {result.bbox_wgs84}")
    print(f"  selected tile count: {len(result.selected_assets)}")
    for asset in result.selected_assets[:25]:
        print(f"    - {asset}")
    if len(result.selected_assets) > 25:
        print(f"    ... {len(result.selected_assets) - 25} more")
    print(f"  output_path: {result.output_path}")
    print(f"  output_projected_path: {result.output_projected_path}")
    print(f"  output_aligned_to_dem_path: {result.output_aligned_to_dem_path}")
    print(f"  output_10m_path: {result.output_10m_path}")
    print(f"  output_30m_path: {result.output_30m_path}")
    print(f"  class_lookup_path: {result.class_lookup_path}")
    if stats:
        print(f"  stats: {stats}")


def download_landcover_for_config(
    config_path: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    method: str | None = None,
) -> LandcoverResult:
    logging.basicConfig(level=logging.INFO)
    raw, config_dir = load_raw_config(config_path)
    veg = raw.get("vegetation_data", {})
    cfg = landcover_config_with_defaults(veg.get("landcover", {}))
    if cfg.get("enabled", True) is False:
        raise ValueError("vegetation_data.landcover.enabled is false")
    chosen_method = (method or cfg.get("method") or "aws").lower()
    if chosen_method != "aws":
        raise ValueError("Landcover method must be 'aws'.")
    resampling = validate_landcover_resampling(str(cfg.get("resampling", "nearest")))
    bbox = bbox_from_config(
        raw, float(veg.get("bbox_buffer_deg", DEFAULT_VEGETATION_BBOX_BUFFER_DEG))
    )
    temp_dir = resolve_path(cfg["temp_dir"], config_dir)
    raw_dir = resolve_path(cfg["raw_dir"], config_dir)
    final_dir = resolve_path(cfg["final_dir"], config_dir)
    output_path = resolve_path(cfg["output_path"], config_dir)
    output_projected_path = (
        resolve_path(cfg["output_projected_path"], config_dir)
        if cfg.get("output_projected_path")
        else None
    )
    output_aligned_path = final_dir / str(cfg.get("land_cover_30m_filename", "LAND_COVER_30M.tif"))
    output_10m_path = final_dir / str(cfg.get("land_cover_10m_filename", "LAND_COVER_10M.tif"))
    output_30m_path = output_aligned_path
    metadata_path = resolve_path(cfg["metadata_path"], config_dir)
    class_lookup_path = resolve_path(
        veg.get(
            "class_lookup_path",
            DEFAULT_VEGETATION_CLASS_LOOKUP_PATH,
        ),
        config_dir,
    )
    cache_dir = resolve_path(cfg["cache_dir"], config_dir)
    cache_path = cache_dir / "esa_worldcover_v200_tile_index_cache.parquet"
    manifest_path = resolve_path(cfg["tile_manifest_path"], config_dir)
    vrt_path = temp_dir / "esa_worldcover_v200_landcover.vrt"
    dem_10m_path = dem_path_for_resolution(raw, config_dir, 10)
    dem_30m_path = dem_path_for_resolution(raw, config_dir, 30)

    if not overwrite and existing_landcover_30m_is_valid(output_30m_path, dem_30m_path, bbox):
        stats = raster_band_stats(output_30m_path, categorical=True)
        found = set(stats.get("unique_values", [])) if "unique_values" in stats else set()
        unexpected = sorted(found - ESA_CLASS_VALUES)
        if unexpected:
            print(f"WARNING: Unexpected ESA WorldCover class values found: {unexpected}")
        result = LandcoverResult(
            output_30m_path,
            output_projected_path,
            output_30m_path,
            class_lookup_path,
            metadata_path,
            [],
            bbox,
            output_10m_path=output_10m_path if output_10m_path.exists() else None,
            output_30m_path=output_30m_path,
        )
        if dry_run:
            print(
                "Dry run only; existing LAND_COVER_30M.tif can be reused and no files will be written."
            )
            _print_landcover_summary(result, stats)
            return result
        write_esa_class_lookup(class_lookup_path)
        metadata = {
            "source_name": cfg.get("source", "esa_worldcover_v200"),
            "source_uri": cfg.get("s3_uri", DEFAULT_WORLDCOVER_S3_URI),
            "method": chosen_method,
            "bbox_wgs84": bbox,
            "selected_assets": [],
            "output_paths": {
                "clip": output_path,
                "projected": (
                    output_projected_path
                    if output_projected_path and output_projected_path.exists()
                    else None
                ),
                "land_cover_10m": output_10m_path if output_10m_path.exists() else None,
                "land_cover_30m": output_30m_path,
            },
            "class_lookup_path": class_lookup_path,
            "class_values_found": sorted(found),
            "unexpected_class_values": unexpected,
            "config_path": str(config_path),
            "unsigned_aws_access": True,
            "reused_existing_output": True,
            "raster_profile": raster_profile_summary(output_30m_path),
            "raster_stats": stats,
        }
        write_metadata_json(metadata_path, metadata)
        print(f"Reusing existing LAND_COVER_30M.tif -> {output_30m_path}")
        _print_landcover_summary(result, stats)
        if bool(cfg.get("cleanup_intermediate_dirs", True)):
            _cleanup_intermediate_paths(
                [
                    vrt_path,
                    output_path,
                    cache_path,
                    manifest_path,
                    temp_dir,
                    cache_dir,
                ],
                preserve=[
                    output_projected_path,
                    output_10m_path,
                    output_30m_path,
                    metadata_path,
                    class_lookup_path,
                ],
                stop_at=resolve_path("data", config_dir),
            )
        return result

    selected: list[dict[str, Any]] = []
    discovery_error = None
    configure_unsigned_aws()
    try:
        selected = discover_worldcover_assets(
            cfg.get("s3_uri", DEFAULT_WORLDCOVER_S3_URI),
            bbox,
            cache_path,
            overwrite_cache=bool(overwrite or cfg.get("refresh_manifest", False)),
            manifest_path=manifest_path,
            write_manifest=not dry_run,
        )
    except Exception as exc:
        discovery_error = str(exc)
        print(f"WARNING: ESA WorldCover S3 discovery failed: {exc}")
        if not dry_run:
            raise

    selected_urls = [rec.get("url") or rec.get("key") for rec in selected]
    result = LandcoverResult(
        output_30m_path,
        output_projected_path,
        output_30m_path,
        class_lookup_path,
        metadata_path,
        selected_urls,
        bbox,
        output_10m_path=output_10m_path,
        output_30m_path=output_30m_path,
    )
    if dry_run:
        print(
            f"Dry run only; no landcover rasters will be written. Discovery error: {discovery_error}"
            if discovery_error
            else "Dry run only; no landcover rasters will be written."
        )
        _print_landcover_summary(result)
        return result

    if not selected_urls:
        raise RuntimeError("No ESA WorldCover tiles selected for bbox.")
    write_esa_class_lookup(class_lookup_path)
    download_overwrite = bool(cfg.get("overwrite_downloads", False))
    local_tile_paths = download_worldcover_assets(selected, raw_dir, overwrite=download_overwrite)
    build_or_reuse_vrt(local_tile_paths, vrt_path, overwrite=overwrite)

    projected_written = None
    projected_crs = raw.get("viewshed", {}).get("crs_projected") or raw.get("region", {}).get(
        "crs_projected"
    )
    if output_projected_path and projected_crs:
        reproject_raster(
            vrt_path,
            output_projected_path,
            str(projected_crs),
            float(cfg.get("target_resolution_m", 10)),
            resampling,
            overwrite=overwrite,
        )
        projected_written = output_projected_path

    aligned_written = None
    if dem_10m_path.exists():
        warp_to_reference_grid(
            vrt_path,
            dem_10m_path,
            output_10m_path,
            resampling=resampling,
            dtype="uint8",
            nodata=0,
            overwrite=overwrite,
        )
    else:
        print(f"WARNING: DEM_10M not found; skipped LAND_COVER_10M alignment: {dem_10m_path}")
    if dem_30m_path.exists():
        warp_to_reference_grid(
            vrt_path,
            dem_30m_path,
            output_30m_path,
            resampling=resampling,
            dtype="uint8",
            nodata=0,
            overwrite=overwrite,
        )
        validate_raster_matches_reference(
            output_30m_path,
            dem_30m_path,
            acceptable_dtypes={"uint8", "uint16"},
            require_nodata=True,
        )
        aligned_written = output_30m_path
    else:
        print(f"WARNING: DEM_30M not found; skipped LAND_COVER_30M alignment: {dem_30m_path}")

    stats = raster_band_stats(output_30m_path, categorical=True) if output_30m_path.exists() else {}
    found = set(stats.get("unique_values", [])) if "unique_values" in stats else set()
    unexpected = sorted(found - ESA_CLASS_VALUES)
    if unexpected:
        print(f"WARNING: Unexpected ESA WorldCover class values found: {unexpected}")
    metadata = {
        "source_name": cfg.get("source", "esa_worldcover_v200"),
        "source_uri": (
            cfg.get("s3_uri", DEFAULT_WORLDCOVER_S3_URI)
            if chosen_method == "aws"
            else cfg.get("gee_asset")
        ),
        "method": chosen_method,
        "bbox_wgs84": bbox,
        "selected_assets": selected_urls,
        "raw_tile_paths": local_tile_paths,
        "output_paths": {
            "vrt": vrt_path,
            "clip": None,
            "projected": projected_written,
            "land_cover_10m": output_10m_path if output_10m_path.exists() else None,
            "land_cover_30m": aligned_written,
        },
        "class_lookup_path": class_lookup_path,
        "class_values_found": sorted(found),
        "unexpected_class_values": unexpected,
        "config_path": str(config_path),
        "unsigned_aws_access": chosen_method == "aws",
        "raster_profile": raster_profile_summary(output_30m_path),
        "raster_stats": stats,
    }
    write_metadata_json(metadata_path, metadata)
    _print_landcover_summary(result, stats)
    if bool(cfg.get("cleanup_intermediate_dirs", True)):
        _cleanup_intermediate_paths(
            [
                vrt_path,
                output_path,
                cache_path,
                manifest_path,
                temp_dir,
                cache_dir,
            ],
            preserve=[
                output_projected_path,
                output_10m_path,
                output_30m_path,
                metadata_path,
                class_lookup_path,
            ],
            stop_at=resolve_path("data", config_dir),
        )
    legacy_dir = resolve_path("data/vegetation/landcover", config_dir)
    if bool(cfg.get("cleanup_legacy_dir", True)):
        _cleanup_intermediate_paths(
            [
                legacy_dir / "esa_worldcover_v200_landcover.vrt",
                legacy_dir / Path(str(output_path.name)),
            ],
            stop_at=resolve_path("data", config_dir),
        )
    return LandcoverResult(
        output_30m_path,
        projected_written,
        aligned_written,
        class_lookup_path,
        metadata_path,
        selected_urls,
        bbox,
        output_10m_path=output_10m_path if output_10m_path.exists() else None,
        output_30m_path=output_30m_path if output_30m_path.exists() else None,
    )
