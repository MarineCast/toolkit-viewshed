from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from viewshed_toolkit._internal.config.data import load_data_config
from viewshed_toolkit._internal.config.paths import project_root, resolve_config_path

from ...config import dem_path_template_from_config, normalize_viewshed_config

CRS_WGS84 = "EPSG:4326"
REPO_ROOT = project_root()
VIEWSHED_ROOT = REPO_ROOT
AWS_ALLOWED_EXTENSIONS = ".tif,.tiff,.json,.geojson"
DEFAULT_VEGETATION_BBOX_BUFFER_DEG = 0.02
DEFAULT_WORLDCOVER_S3_URI = "s3://esa-worldcover/v200/2021/map"
LOGGER = logging.getLogger(__name__)


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(os.getpid()).memory_info().rss) / (1024 * 1024)
    except Exception:
        pass
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if rss > 10_000_000:
            return rss / (1024 * 1024)
        return rss / 1024
    except Exception:
        return None


@contextmanager
def profile_timer(label: str, **context: Any):
    detail = " ".join(f"{key}={value}" for key, value in context.items() if value is not None)
    LOGGER.info("Starting %s%s", label, f" {detail}" if detail else "")
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        rss = _rss_mb()
        if rss is None:
            LOGGER.info("Finished %s in %.1f seconds", label, elapsed)
        else:
            LOGGER.info("Finished %s in %.1f seconds rss_mb=%.1f", label, elapsed, rss)


def configure_unsigned_aws(region: str | None = None) -> dict[str, str]:
    env = {
        "AWS_NO_SIGN_REQUEST": "YES",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": AWS_ALLOWED_EXTENSIONS,
    }
    if region:
        env["AWS_REGION"] = region
    for key, value in env.items():
        os.environ.setdefault(key, value)
    return env


def _resolve_config_path(config_path: str | Path) -> Path:
    return resolve_config_path(config_path)


def load_raw_config(config_path: str | Path) -> tuple[dict[str, Any], Path]:
    resolved = _resolve_config_path(config_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Config file not found: {resolved}")
    raw = load_data_config(resolved, domains="HUMAN_LAYER")
    is_normalized_runtime = any(
        key in raw
        for key in (
            "region",
            "h3",
            "batch",
            "vegetation_weights",
            "vegetation_path_weights",
        )
    )
    if "viewshed" in raw and not is_normalized_runtime:
        viewshed_raw = raw["viewshed"]
        if not isinstance(viewshed_raw, dict):
            raise ValueError("Config section 'viewshed' must be a mapping.")
        raw = viewshed_raw
    return normalize_viewshed_config(raw), resolved.parent


def resolve_path(value: str | Path, config_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if path.parts and path.parts[0] in {"config", "data", "notebooks", "src"}:
        return (REPO_ROOT / path).resolve()
    return (config_dir / path).resolve()


def valid_raster(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        import rasterio  # type: ignore

        with rasterio.open(path) as src:
            bounds = src.bounds
            _ = src.profile
            return bool(src.width > 0 and src.height > 0 and all(math.isfinite(v) for v in bounds))
    except Exception:
        return False


def raster_covers_bbox(
    path: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    expected_crs: str | None = CRS_WGS84,
    tolerance: float = 1e-8,
) -> bool:
    if not valid_raster(path):
        return False
    try:
        import rasterio  # type: ignore

        with rasterio.open(path) as src:
            if expected_crs and str(src.crs).upper() != str(expected_crs).upper():
                return False
            left, bottom, right, top = tuple(float(v) for v in src.bounds)
            min_lon, min_lat, max_lon, max_lat = bbox_wgs84
            return (
                left <= min_lon + tolerance
                and bottom <= min_lat + tolerance
                and right >= max_lon - tolerance
                and top >= max_lat - tolerance
            )
    except Exception:
        return False


def validate_raster_matches_reference(
    path: Path,
    reference_path: Path,
    *,
    acceptable_dtypes: set[str] | None = None,
    require_nodata: bool = False,
    tolerance: float = 1e-8,
) -> None:
    if not valid_raster(path):
        raise ValueError(f"Raster is missing or unreadable: {path}")
    if not valid_raster(reference_path):
        raise ValueError(f"Reference raster is missing or unreadable: {reference_path}")
    import numpy as np  # type: ignore
    import rasterio  # type: ignore

    with rasterio.open(path) as src, rasterio.open(reference_path) as ref:
        problems = []
        if src.crs != ref.crs:
            problems.append(f"crs {src.crs} != {ref.crs}")
        if src.transform != ref.transform:
            problems.append(f"transform {src.transform} != {ref.transform}")
        if src.width != ref.width or src.height != ref.height:
            problems.append(f"shape {(src.width, src.height)} != {(ref.width, ref.height)}")
        if not np.allclose(tuple(src.bounds), tuple(ref.bounds), rtol=0, atol=tolerance):
            problems.append(f"bounds {tuple(src.bounds)} != {tuple(ref.bounds)}")
        if acceptable_dtypes and src.dtypes and src.dtypes[0] not in acceptable_dtypes:
            problems.append(f"dtype {src.dtypes[0]} not in {sorted(acceptable_dtypes)}")
        if require_nodata and src.nodata is None:
            problems.append("nodata is not set")
        if problems:
            raise ValueError(
                f"Raster does not match reference grid: {path}; " + "; ".join(problems)
            )


def aligned_raster_is_valid(
    path: Path,
    reference_path: Path,
    *,
    acceptable_dtypes: set[str] | None = None,
    require_nodata: bool = False,
) -> bool:
    try:
        validate_raster_matches_reference(
            path,
            reference_path,
            acceptable_dtypes=acceptable_dtypes,
            require_nodata=require_nodata,
        )
        return True
    except Exception as exc:
        LOGGER.info("Existing raster cannot be reused: %s reason=%s", path, exc)
        return False


def resampling_enum(name: str):
    from rasterio.warp import Resampling  # type: ignore

    normalized = (name or "nearest").lower()
    if not hasattr(Resampling, normalized):
        raise ValueError(f"Unsupported resampling method: {name}")
    return getattr(Resampling, normalized)


def _gtiff_tiling_options(width: int, height: int, block_size: int = 512) -> dict[str, Any]:
    if width < 16 or height < 16:
        return {"tiled": False}
    return {"tiled": True, "blockxsize": block_size, "blockysize": block_size}


def read_tile_manifest(
    path: Path,
    *,
    bbox_wgs84: tuple[float, float, float, float],
    source: str,
) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        LOGGER.info("Ignoring unreadable tile manifest: %s reason=%s", path, exc)
        return None
    if tuple(float(v) for v in payload.get("bbox_wgs84", [])) != tuple(
        float(v) for v in bbox_wgs84
    ):
        return None
    if str(payload.get("source")) != str(source):
        return None
    records = payload.get("selected_tiles")
    return records if isinstance(records, list) else None


def write_tile_manifest(
    path: Path,
    *,
    bbox_wgs84: tuple[float, float, float, float],
    source: str,
    selected_tiles: Sequence[dict[str, Any]],
    notes: str = "",
) -> Path:
    payload = {
        "bbox_wgs84": list(bbox_wgs84),
        "source": source,
        "selected_tiles": list(selected_tiles),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    return path


def build_or_reuse_vrt(
    source_paths_or_urls: Sequence[str | Path], vrt_path: Path, *, overwrite: bool = False
) -> Path:
    if vrt_path.exists() and not overwrite:
        LOGGER.info("Reusing VRT: %s", vrt_path)
        return vrt_path
    if not source_paths_or_urls:
        raise ValueError("No source rasters were supplied for VRT creation.")
    gdalbuildvrt = shutil.which("gdalbuildvrt")
    if not gdalbuildvrt:
        raise RuntimeError("gdalbuildvrt was not found on PATH; cannot build raster VRT.")
    configure_unsigned_aws()
    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    if vrt_path.exists():
        vrt_path.unlink()
    gdal_sources = []
    for path in source_paths_or_urls:
        text = str(path)
        if text.startswith("s3://"):
            bucket, key = parse_s3_uri(text)
            text = f"/vsis3/{bucket}/{key}"
        gdal_sources.append(text)
    with profile_timer("build_vrt", output=vrt_path, sources=len(source_paths_or_urls)):
        subprocess.run(
            [gdalbuildvrt, "-overwrite", str(vrt_path), *gdal_sources],
            check=True,
        )
    return vrt_path


def warp_to_reference_grid(
    src_path: Path,
    reference_path: Path,
    out_path: Path,
    *,
    resampling: str,
    dtype: str | None = None,
    nodata: float | int | None = None,
    overwrite: bool = False,
) -> Path:
    acceptable = {dtype} if dtype else None
    if not overwrite and aligned_raster_is_valid(
        out_path,
        reference_path,
        acceptable_dtypes=acceptable,
        require_nodata=nodata is not None,
    ):
        LOGGER.info("Reusing existing DEM-aligned raster: %s", out_path)
        return out_path
    import rasterio  # type: ignore
    from rasterio.warp import reproject  # type: ignore

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    with rasterio.open(src_path) as src, rasterio.open(reference_path) as ref:
        out_dtype = dtype or src.dtypes[0]
        out_nodata = nodata if nodata is not None else src.nodata
        profile = ref.profile.copy()
        profile.update(
            driver="GTiff",
            count=src.count,
            dtype=out_dtype,
            nodata=out_nodata,
            compress="deflate",
            BIGTIFF="IF_SAFER",
            **_gtiff_tiling_options(ref.width, ref.height),
        )
        with profile_timer(
            "warp_to_reference_grid",
            source=src_path,
            reference=reference_path,
            output=out_path,
            resampling=resampling,
        ):
            with rasterio.open(out_path, "w", **profile) as dst:
                for band_index in range(1, src.count + 1):
                    reproject(
                        rasterio.band(src, band_index),
                        rasterio.band(dst, band_index),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        src_nodata=src.nodata,
                        dst_transform=ref.transform,
                        dst_crs=ref.crs,
                        dst_nodata=out_nodata,
                        resampling=resampling_enum(resampling),
                    )
    validate_raster_matches_reference(
        out_path,
        reference_path,
        acceptable_dtypes=acceptable,
        require_nodata=nodata is not None,
    )
    return out_path


class _ProgressDataset:
    def __init__(self, dataset: Any, progress: Any) -> None:
        self._dataset = dataset
        self._progress = progress
        self._counted = False

    def read(self, *args: Any, **kwargs: Any) -> Any:
        data = self._dataset.read(*args, **kwargs)
        if not self._counted:
            self._progress.update(1)
            self._counted = True
        return data

    def close(self) -> None:
        self._dataset.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._dataset, name)


def _make_progress_bar(total: int, desc: str) -> Any | None:
    try:
        from tqdm.auto import tqdm  # type: ignore
    except Exception:
        return None
    return tqdm(total=total, desc=desc, unit="file")


def clip_cogs_to_bbox(
    cog_paths_or_urls: list[str],
    bbox_wgs84: tuple[float, float, float, float],
    out_path: Path,
    dst_crs: str | None = CRS_WGS84,
    dst_resolution: float | None = None,
    resampling: str = "nearest",
    nodata: float | int | None = None,
    overwrite: bool = False,
    show_progress: bool = False,
    progress_desc: str = "Source rasters",
) -> Path:
    if valid_raster(out_path) and not overwrite:
        return out_path
    if not cog_paths_or_urls:
        raise ValueError("No source rasters were selected for clipping.")
    import rasterio  # type: ignore
    from rasterio.merge import merge  # type: ignore

    configure_unsigned_aws()
    if out_path.exists():
        out_path.unlink()
    from rasterio.vrt import WarpedVRT  # type: ignore

    progress = _make_progress_bar(len(cog_paths_or_urls), progress_desc) if show_progress else None
    base_datasets = [rasterio.open(path) for path in cog_paths_or_urls]
    datasets = []
    merge_datasets = []
    try:
        target_crs = dst_crs or base_datasets[0].crs
        for dataset in base_datasets:
            if dst_crs and dataset.crs and str(dataset.crs) != str(dst_crs):
                datasets.append(
                    WarpedVRT(
                        dataset, crs=dst_crs, resampling=resampling_enum(resampling), nodata=nodata
                    )
                )
            else:
                datasets.append(dataset)
        merge_datasets = (
            [_ProgressDataset(dataset, progress) for dataset in datasets]
            if progress is not None
            else datasets
        )
        mosaic, transform = merge(
            merge_datasets,
            bounds=bbox_wgs84 if str(target_crs).upper() == CRS_WGS84 else None,
            dst_path=None,
            resampling=resampling_enum(resampling),
            nodata=nodata,
            target_aligned_pixels=True,
            res=dst_resolution,
        )
        profile = datasets[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            transform=transform,
            count=mosaic.shape[0],
            crs=target_crs,
            compress="deflate",
            tiled=True,
            blockxsize=256,
            blockysize=256,
            BIGTIFF="IF_SAFER",
        )
        if nodata is not None:
            profile["nodata"] = nodata
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(mosaic)
    except Exception:
        if out_path.exists():
            out_path.unlink()
        raise
    finally:
        if progress is not None:
            progress.close()
        for dataset in datasets:
            if dataset not in base_datasets:
                dataset.close()
        for dataset in base_datasets:
            dataset.close()
    return out_path


def reproject_raster(
    src_path: Path,
    out_path: Path,
    dst_crs: str,
    resolution_m: float | None,
    resampling: str,
    overwrite: bool = False,
) -> Path:
    if valid_raster(out_path) and not overwrite:
        return out_path
    import rasterio  # type: ignore
    from rasterio.warp import calculate_default_transform, reproject  # type: ignore

    if out_path.exists():
        out_path.unlink()
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds, resolution=resolution_m
        )
        profile = src.profile.copy()
        profile.update(
            crs=dst_crs,
            transform=transform,
            width=width,
            height=height,
            compress="deflate",
            BIGTIFF="IF_SAFER",
            **_gtiff_tiling_options(width, height),
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            for band_index in range(1, src.count + 1):
                reproject(
                    rasterio.band(src, band_index),
                    rasterio.band(dst, band_index),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=resampling_enum(resampling),
                    src_nodata=src.nodata,
                    dst_nodata=profile.get("nodata"),
                )
    return out_path


def raster_profile_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        import rasterio  # type: ignore

        with rasterio.open(path) as src:
            return {
                "path": str(path),
                "exists": True,
                "crs": str(src.crs),
                "bounds": list(src.bounds),
                "resolution": list(src.res),
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtype": src.dtypes[0] if src.dtypes else None,
                "nodata": src.nodata,
            }
    except Exception as exc:
        return {"path": str(path), "exists": path.exists(), "error": str(exc)}


def raster_band_stats(path: Path, categorical: bool = False) -> dict[str, Any]:
    try:
        import numpy as np  # type: ignore
        import rasterio  # type: ignore

        with rasterio.open(path) as src:
            valid_count = 0
            unique_values: set[int] = set()
            min_value: float | None = None
            max_value: float | None = None
            sample_chunks: list[Any] = []
            sample_limit = 1_000_000
            sample_count = 0
            for _, window in src.block_windows(1):
                arr = src.read(1, window=window, masked=True)
                data = arr.compressed()
                if data.size == 0:
                    continue
                valid_count += int(data.size)
                if categorical:
                    unique_values.update(int(v) for v in np.unique(data))
                    if len(unique_values) > 200:
                        unique_values = set(sorted(unique_values)[:200])
                else:
                    block_min = float(np.min(data))
                    block_max = float(np.max(data))
                    min_value = block_min if min_value is None else min(min_value, block_min)
                    max_value = block_max if max_value is None else max(max_value, block_max)
                    if sample_count < sample_limit:
                        remaining = sample_limit - sample_count
                        if data.size > remaining:
                            step = max(1, int(math.ceil(data.size / remaining)))
                            sampled = data[::step][:remaining]
                        else:
                            sampled = data
                        sample_chunks.append(sampled)
                        sample_count += int(sampled.size)
            if valid_count == 0:
                return {"valid_pixel_count": 0}
            if categorical:
                return {
                    "valid_pixel_count": valid_count,
                    "unique_values": sorted(unique_values)[:200],
                }
            sample = (
                np.concatenate(sample_chunks) if sample_chunks else np.array([], dtype="float32")
            )
            stats = {"valid_pixel_count": valid_count, "min": min_value, "max": max_value}
            if sample.size:
                stats.update(
                    {
                        "p50": float(np.percentile(sample, 50)),
                        "p95": float(np.percentile(sample, 95)),
                        "p99": float(np.percentile(sample, 99)),
                        "percentiles_approx": valid_count > sample.size,
                    }
                )
            return stats
    except Exception as exc:
        return {"error": str(exc)}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


def write_metadata_json(path: Path, metadata: dict[str, Any]) -> Path:
    payload = dict(metadata)
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    return path


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Expected s3:// URI, got: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def s3_url(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def list_s3_objects_unsigned(
    s3_uri: str, suffixes: tuple[str, ...] | None = None, max_keys: int | None = None
) -> list[dict[str, Any]]:
    bucket, prefix = parse_s3_uri(s3_uri)
    token = None
    objects: list[dict[str, Any]] = []
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        url = f"https://{bucket}.s3.amazonaws.com/?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as response:
            root = ET.fromstring(response.read())
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for item in root.findall("s3:Contents", ns):
            key = item.findtext("s3:Key", default="", namespaces=ns)
            if suffixes and not key.lower().endswith(suffixes):
                continue
            objects.append(
                {
                    "key": key,
                    "url": s3_url(bucket, key),
                    "size": int(item.findtext("s3:Size", default="0", namespaces=ns) or 0),
                }
            )
            if max_keys is not None and len(objects) >= max_keys:
                return objects
        truncated = root.findtext("s3:IsTruncated", default="false", namespaces=ns) == "true"
        if not truncated:
            break
        token = root.findtext("s3:NextContinuationToken", default=None, namespaces=ns)
        if not token:
            break
    return objects


def bboxes_intersect(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def dem_path_from_config(raw: dict[str, Any], config_dir: Path) -> Path:
    configured = raw.get("paths", {}).get("regional_dem_path")
    if configured:
        return resolve_path(configured, config_dir)
    resolution = int(raw.get("viewshed", {}).get("dem_resolution_m", 30))
    return resolve_path(dem_path_template_from_config(raw, resolution), config_dir)


def dem_path_for_resolution(raw: dict[str, Any], config_dir: Path, resolution_m: int) -> Path:
    configured = raw.get("paths", {}).get(f"dem_{resolution_m}m_path")
    if configured:
        return resolve_path(configured, config_dir)
    return resolve_path(dem_path_template_from_config(raw, resolution_m), config_dir)


# -----------------------------------------------------------------------------
# ETH Global Canopy Height preparation
# -----------------------------------------------------------------------------

ETH_TILE_INDEX_URL = "https://langnico.github.io/globalcanopyheight/assets/tile_index.html"
ETH_DOWNLOAD_BASE_URL = (
    "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files="
)
ETH_TILE_SIZE_DEG = 3
_ETH_TILE_RE = re.compile(
    r"ETH_GlobalCanopyHeight_10m_2020_(?P<tile>[NS]\d{2}[EW]\d{3})_Map(?P<std>_SD)?\.tif"
)
