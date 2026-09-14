# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

import hashlib
import logging
from typing import Any, Sequence

import numpy as np
import pandas as pd
from pyproj import Transformer
from rasterio.transform import rowcol
from shapely import wkb
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

from ...config import (
    DEFAULT_VEGETATION_PATH_INPUTS,
    DEFAULT_VEGETATION_PATH_OUTPUTS,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_INPUTS = dict(DEFAULT_VEGETATION_PATH_INPUTS)
DEFAULT_COLUMNS = {
    "source_id": "source_h3",
    "target_id": "target_h3",
    "target_id_output": "target_h3",
    "clear_sky_weight": "weight_terrain",
    "distance_km": "distance_km",
    "source_weight_column": "land_source_vegetation_weight",
    "source_lon": "source_lon",
    "source_lat": "source_lat",
    "target_lon": "target_lon",
    "target_lat": "target_lat",
}
DEFAULT_OUTPUTS = dict(DEFAULT_VEGETATION_PATH_OUTPUTS)


from .path_config import VegetationPathConfig


def _project_geometry(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    return shapely_transform(transformer.transform, geom)


def _project_geometry_lookup(geoms: dict[str, BaseGeometry], raster_crs) -> dict[str, BaseGeometry]:
    if str(raster_crs).upper() in {"EPSG:4326", "OGC:CRS84"}:
        return geoms
    transformer = Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True)
    return {cell: _project_geometry(geom, transformer) for cell, geom in geoms.items()}


def _is_wgs84_crs(crs: Any) -> bool:
    return str(crs).upper() in {"EPSG:4326", "OGC:CRS84"}


def _geom(value: Any) -> BaseGeometry:
    if isinstance(value, BaseGeometry):
        return value
    if isinstance(value, bytes):
        return wkb.loads(value)
    if isinstance(value, str):
        try:
            return wkb.loads(bytes.fromhex(value))
        except Exception:
            return cell_to_polygon(value)
    raise TypeError(f"Cannot decode geometry value: {type(value)!r}")


def stable_seed(global_seed: int, *parts: Any) -> int:
    text = "|".join([str(global_seed), *[str(p) for p in parts]])
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**32)


def sample_points_in_polygon(
    polygon: BaseGeometry,
    n_points: int,
    *,
    strategy: str = "deterministic_random",
    include_centroid: bool = True,
    seed: int = 42,
    enforce_within_polygon: bool = True,
    max_attempts: int = 1000,
) -> list[Point]:
    if n_points <= 0:
        return []
    poly = polygon if polygon.is_valid else polygon.buffer(0)
    points: list[Point] = []
    if include_centroid:
        centroid = poly.centroid
        points.append(
            centroid
            if (not enforce_within_polygon or poly.contains(centroid))
            else poly.representative_point()
        )
    if len(points) >= n_points:
        return points[:n_points]
    if strategy != "deterministic_random":
        raise ValueError(f"Unsupported point sampling strategy: {strategy}")
    rng = np.random.default_rng(seed)
    minx, miny, maxx, maxy = poly.bounds
    attempts = 0
    while len(points) < n_points and attempts < max_attempts:
        attempts += 1
        point = Point(float(rng.uniform(minx, maxx)), float(rng.uniform(miny, maxy)))
        if not enforce_within_polygon or poly.contains(point):
            points.append(point)
    while len(points) < n_points:
        points.append(poly.representative_point())
    return points[:n_points]


def pair_points(
    source_points: Sequence[Point], target_points: Sequence[Point], pair_mode: str
) -> list[tuple[Point, Point]]:
    if pair_mode == "paired":
        return list(zip(source_points, target_points))
    if pair_mode == "cross_product":
        return [(s, t) for s in source_points for t in target_points]
    raise ValueError(f"Unsupported pair_mode: {pair_mode}")


def cached_cell_points(
    cache: dict[tuple[str, str], list[Point]],
    *,
    role: str,
    cell_id: str,
    geometry: BaseGeometry,
    n_points: int,
    sampling: dict[str, Any],
    global_seed: int,
    max_attempts: int,
) -> list[Point]:
    key = (role, str(cell_id))
    if key not in cache:
        strategy_key = f"{role}_point_strategy"
        cache[key] = sample_points_in_polygon(
            geometry,
            n_points,
            strategy=sampling.get(strategy_key, "deterministic_random"),
            include_centroid=bool(sampling.get("include_centroid", True)),
            seed=stable_seed(global_seed, role, cell_id),
            enforce_within_polygon=bool(sampling.get("enforce_within_polygon", True)),
            max_attempts=max_attempts,
        )
    return cache[key]


def bresenham_line(row0: int, col0: int, row1: int, col1: int) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    dr = abs(row1 - row0)
    dc = abs(col1 - col0)
    sr = 1 if row0 < row1 else -1
    sc = 1 if col0 < col1 else -1
    err = dc - dr
    row, col = row0, col0
    while True:
        points.append((row, col))
        if row == row1 and col == col1:
            break
        err2 = 2 * err
        if err2 > -dr:
            err -= dr
            col += sc
        if err2 < dc:
            err += dc
            row += sr
    return points


def trace_raster_line(
    point_a: Point,
    point_b: Point,
    transform,
    raster_shape: tuple[int, int],
    *,
    include_endpoint_pixels: bool = True,
) -> list[tuple[int, int]]:
    row0, col0 = rowcol(transform, point_a.x, point_a.y)
    row1, col1 = rowcol(transform, point_b.x, point_b.y)
    cells = bresenham_line(int(row0), int(col0), int(row1), int(col1))
    height, width = raster_shape
    cells = [(r, c) for r, c in cells if 0 <= r < height and 0 <= c < width]
    if not include_endpoint_pixels and len(cells) > 2:
        cells = cells[1:-1]
    return cells


def _meters_between(a: Point, b: Point) -> float:
    if not (-180 <= a.x <= 180 and -90 <= a.y <= 90 and -180 <= b.x <= 180 and -90 <= b.y <= 90):
        return float(np.hypot(b.x - a.x, b.y - a.y))
    try:
        from pyproj import Geod

        geod = Geod(ellps="WGS84")
        _, _, dist = geod.inv(a.x, a.y, b.x, b.y)
        return float(dist)
    except Exception:
        lat = np.deg2rad((a.y + b.y) / 2.0)
        dx = (b.x - a.x) * 111_320.0 * np.cos(lat)
        dy = (b.y - a.y) * 110_540.0
        return float(np.hypot(dx, dy))


def _skip_cells(
    cells: list[tuple[int, int]],
    source: Point,
    target: Point,
    cfg: VegetationPathConfig,
) -> list[tuple[int, int]]:
    ray_cfg = cfg.raw.get("ray_tracing", {})
    policy = ray_cfg.get("source_near_field_policy", "skip_first_distance_m")
    if policy == "none" or not cells:
        return cells
    if policy == "skip_first_n_pixels":
        return cells[int(ray_cfg.get("skip_first_n_pixels", 0)) :]
    if policy == "skip_first_distance_m":
        total_m = _meters_between(source, target)
        if total_m <= 0:
            return cells
        skip_m = float(ray_cfg.get("skip_first_distance_m", 60))
        skip_n = int(np.floor((skip_m / total_m) * max(len(cells) - 1, 1)))
        return cells[min(skip_n, len(cells)) :]
    raise ValueError(f"Unsupported source_near_field_policy: {policy}")


def score_ray(
    source: Point,
    target: Point,
    *,
    transform,
    raster_shape: tuple[int, int],
    transmission: np.ndarray,
    obstruction: np.ndarray,
    chm_obstruction: np.ndarray,
    landcover: np.ndarray,
    cfg: VegetationPathConfig,
) -> dict[str, float | int | None]:
    cells = trace_raster_line(
        source,
        target,
        transform,
        raster_shape,
        include_endpoint_pixels=bool(
            cfg.raw.get("ray_tracing", {}).get("include_endpoint_pixels", True)
        ),
    )
    cells = _skip_cells(cells, source, target, cfg)
    if not cells:
        return _empty_ray_score(cfg)
    path_length_m = float(source.distance(target))
    segment_length_m = path_length_m / max(len(cells) - 1, 1)
    rows = np.array([r for r, _ in cells], dtype=int)
    cols = np.array([c for _, c in cells], dtype=int)
    trans_vals = transmission[rows, cols].astype("float64")
    obs_vals = obstruction[rows, cols].astype("float64")
    chm_vals = chm_obstruction[rows, cols].astype("float64")
    lc_vals = landcover[rows, cols]
    lc_cfg = cfg.raw.get("landcover", {})
    water_codes = {int(v) for v in lc_cfg.get("water_codes", [80])}
    ignore_codes = {int(v) for v in lc_cfg.get("ignore_codes_for_vegetation_path", [80])}
    landcover_nodata = cfg.nodata.get("landcover", 0.0)
    trans_nodata = cfg.nodata.get("surface_transmission", -9999.0)
    obs_nodata = cfg.nodata.get("surface_obstruction", -9999.0)
    chm_nodata = cfg.nodata.get("chm_obstruction", -9999.0)
    valid_land = (
        (lc_vals != landcover_nodata)
        & ~np.isin(lc_vals, list(ignore_codes))
        & (trans_vals != trans_nodata)
        & np.isfinite(trans_vals)
    )
    water = np.isin(lc_vals, list(water_codes))
    if not np.any(valid_land):
        if (
            cfg.raw.get("ray_tracing", {}).get("no_land_pixels_policy", "transmission_1")
            == "transmission_1"
        ):
            ray_value = 1.0
        else:
            ray_value = None
        return {
            "path_vegetation_transmission": ray_value,
            "path_vegetation_weight_mean": ray_value,
            "path_vegetation_weight_length_decay": ray_value,
            "weight_landcover": ray_value,
            "weight_chm": ray_value,
            "path_length_m": path_length_m,
            "vegetated_path_length_m": 0.0,
            "n_total_pixels_crossed": int(len(cells)),
            "n_valid_land_pixels_crossed": 0,
            "n_water_pixels_crossed": int(np.count_nonzero(water)),
            "mean_surface_obstruction_along_land": None,
            "max_surface_obstruction_along_land": None,
            "mean_surface_transmission_along_land": None,
        }
    method = cfg.raw.get("path_transmission", {}).get(
        "method", "mean_transmission_over_land_pixels"
    )
    land_trans = trans_vals[valid_land]
    land_obs = obs_vals[valid_land]
    land_lc_obs = _path_landcover_obstruction(lc_vals[valid_land], cfg)
    land_chm_obs = chm_vals[valid_land]
    valid_lc_obs = land_lc_obs[np.isfinite(land_lc_obs)]
    valid_chm_obs = land_chm_obs[(land_chm_obs != chm_nodata) & np.isfinite(land_chm_obs)]
    valid_obs = land_obs[(land_obs != obs_nodata) & np.isfinite(land_obs)]
    path_vegetation_weight_mean = float(np.mean(land_trans))
    beta = float(cfg.raw.get("path_transmission", {}).get("beta_per_meter", 0.002))
    path_vegetation_weight_length_decay = length_decay_transmission(
        valid_obs,
        segment_length_m=segment_length_m,
        beta_per_meter=beta,
    )
    weight_landcover_mean = float(np.mean(1.0 - valid_lc_obs)) if valid_lc_obs.size else 1.0
    weight_chm_mean = float(np.mean(1.0 - valid_chm_obs)) if valid_chm_obs.size else 1.0
    weight_landcover_length_decay = length_decay_transmission(
        valid_lc_obs,
        segment_length_m=segment_length_m,
        beta_per_meter=beta,
    )
    weight_chm_length_decay = length_decay_transmission(
        valid_chm_obs,
        segment_length_m=segment_length_m,
        beta_per_meter=beta,
    )
    if method == "mean_transmission_over_land_pixels":
        ray_value = path_vegetation_weight_mean
    elif method == "p10_transmission_over_land_pixels":
        ray_value = float(np.percentile(land_trans, 10))
    elif method == "min_transmission_over_land_pixels":
        ray_value = float(np.min(land_trans))
    else:
        ray_value = path_vegetation_weight_length_decay
    clamp_min = float(cfg.raw.get("path_transmission", {}).get("clamp_min", 0.0))
    clamp_max = float(cfg.raw.get("path_transmission", {}).get("clamp_max", 1.0))
    ray_value = float(np.clip(ray_value, clamp_min, clamp_max))
    path_vegetation_weight_mean = float(np.clip(path_vegetation_weight_mean, clamp_min, clamp_max))
    path_vegetation_weight_length_decay = float(
        np.clip(path_vegetation_weight_length_decay, clamp_min, clamp_max)
    )
    selected = selected_path_weight_name(cfg)
    weight_landcover = (
        weight_landcover_length_decay if selected == "length_decay" else weight_landcover_mean
    )
    weight_chm = weight_chm_length_decay if selected == "length_decay" else weight_chm_mean
    return {
        "path_vegetation_transmission": ray_value,
        "path_vegetation_weight_mean": path_vegetation_weight_mean,
        "path_vegetation_weight_length_decay": path_vegetation_weight_length_decay,
        "weight_landcover": float(np.clip(weight_landcover, clamp_min, clamp_max)),
        "weight_chm": float(np.clip(weight_chm, clamp_min, clamp_max)),
        "path_length_m": path_length_m,
        "vegetated_path_length_m": float(np.count_nonzero(valid_land) * segment_length_m),
        "n_total_pixels_crossed": int(len(cells)),
        "n_valid_land_pixels_crossed": int(np.count_nonzero(valid_land)),
        "n_water_pixels_crossed": int(np.count_nonzero(water)),
        "mean_surface_obstruction_along_land": (
            float(np.mean(valid_obs)) if valid_obs.size else None
        ),
        "max_surface_obstruction_along_land": (
            float(np.max(valid_obs)) if valid_obs.size else None
        ),
        "mean_surface_transmission_along_land": float(np.mean(land_trans)),
    }


def length_decay_transmission(
    obstruction_scores: Any,
    *,
    segment_length_m: float,
    beta_per_meter: float,
) -> float:
    scores = np.asarray(obstruction_scores, dtype="float64")
    scores = scores[np.isfinite(scores)]
    if scores.size == 0:
        return 1.0
    return float(np.exp(-float(beta_per_meter) * np.sum(scores * float(segment_length_m))))


def _path_landcover_obstruction(
    landcover_values: np.ndarray,
    cfg: VegetationPathConfig,
) -> np.ndarray:
    classes = {
        int(code): values for code, values in dict(cfg.raw.get("landcover_classes", {})).items()
    }
    out = np.full(landcover_values.shape, np.nan, dtype="float64")
    for code, values in classes.items():
        out[landcover_values == code] = float(values.get("obstruction_floor", 0.0))
    return np.clip(out, 0.0, 1.0)


def _empty_ray_score(cfg: VegetationPathConfig) -> dict[str, float | int | None]:
    ray_value = (
        1.0
        if cfg.raw.get("ray_tracing", {}).get("no_land_pixels_policy", "transmission_1")
        == "transmission_1"
        else None
    )
    return {
        "path_vegetation_transmission": ray_value,
        "path_vegetation_weight_mean": ray_value,
        "path_vegetation_weight_length_decay": ray_value,
        "weight_landcover": ray_value,
        "weight_chm": ray_value,
        "path_length_m": 0.0,
        "vegetated_path_length_m": 0.0,
        "n_total_pixels_crossed": 0,
        "n_valid_land_pixels_crossed": 0,
        "n_water_pixels_crossed": 0,
        "mean_surface_obstruction_along_land": None,
        "max_surface_obstruction_along_land": None,
        "mean_surface_transmission_along_land": None,
    }


def aggregate_ray_scores(scores: Sequence[dict[str, Any]], requested_rays: int) -> dict[str, Any]:
    vals = np.array(
        [
            s["path_vegetation_transmission"]
            for s in scores
            if s.get("path_vegetation_transmission") is not None
        ],
        dtype="float64",
    )
    mean_weight_vals = np.array(
        [
            s["path_vegetation_weight_mean"]
            for s in scores
            if s.get("path_vegetation_weight_mean") is not None
        ],
        dtype="float64",
    )
    length_decay_vals = np.array(
        [
            s["path_vegetation_weight_length_decay"]
            for s in scores
            if s.get("path_vegetation_weight_length_decay") is not None
        ],
        dtype="float64",
    )
    mean_obs_vals = np.array(
        [
            s["mean_surface_obstruction_along_land"]
            for s in scores
            if s.get("mean_surface_obstruction_along_land") is not None
        ],
        dtype="float64",
    )
    max_obs_vals = np.array(
        [
            s["max_surface_obstruction_along_land"]
            for s in scores
            if s.get("max_surface_obstruction_along_land") is not None
        ],
        dtype="float64",
    )
    mean_trans_vals = np.array(
        [
            s["mean_surface_transmission_along_land"]
            for s in scores
            if s.get("mean_surface_transmission_along_land") is not None
        ],
        dtype="float64",
    )
    weight_landcover_vals = np.array(
        [s["weight_landcover"] for s in scores if s.get("weight_landcover") is not None],
        dtype="float64",
    )
    weight_chm_vals = np.array(
        [s["weight_chm"] for s in scores if s.get("weight_chm") is not None],
        dtype="float64",
    )
    out: dict[str, Any] = {
        "n_requested_rays": int(requested_rays),
        "n_completed_rays": int(len(scores)),
        "n_valid_vegetation_rays": int(vals.size),
        "valid_vegetation_ray_fraction": (float(vals.size) / float(len(scores)) if scores else 0.0),
        "mean_land_pixels_crossed": (
            float(np.mean([s["n_valid_land_pixels_crossed"] for s in scores])) if scores else 0.0
        ),
        "mean_water_pixels_crossed": (
            float(np.mean([s["n_water_pixels_crossed"] for s in scores])) if scores else 0.0
        ),
        "mean_total_pixels_crossed": (
            float(np.mean([s["n_total_pixels_crossed"] for s in scores])) if scores else 0.0
        ),
        "path_length_m": (
            float(np.mean([s.get("path_length_m", 0.0) for s in scores])) if scores else 0.0
        ),
        "vegetated_path_length_m": (
            float(np.mean([s.get("vegetated_path_length_m", 0.0) for s in scores]))
            if scores
            else 0.0
        ),
        "path_vegetation_weight_mean": (
            float(np.mean(mean_weight_vals)) if mean_weight_vals.size else None
        ),
        "path_vegetation_weight_length_decay": (
            float(np.mean(length_decay_vals)) if length_decay_vals.size else None
        ),
        "weight_landcover": (
            float(np.mean(weight_landcover_vals)) if weight_landcover_vals.size else None
        ),
        "weight_chm": float(np.mean(weight_chm_vals)) if weight_chm_vals.size else None,
        "mean_surface_obstruction_along_land": (
            float(np.mean(mean_obs_vals)) if mean_obs_vals.size else None
        ),
        "max_surface_obstruction_along_land": (
            float(np.max(max_obs_vals)) if max_obs_vals.size else None
        ),
        "mean_surface_transmission_along_land": (
            float(np.mean(mean_trans_vals)) if mean_trans_vals.size else None
        ),
    }
    for name, fn in {
        "mean": np.mean,
        "median": np.median,
        "min": np.min,
        "max": np.max,
        "std": np.std,
    }.items():
        out[f"path_vegetation_transmission_{name}"] = float(fn(vals)) if vals.size else None
    for pct in [10, 25, 75]:
        out[f"path_vegetation_transmission_p{pct}"] = (
            float(np.percentile(vals, pct)) if vals.size else None
        )
    return out


def selected_path_weight_name(cfg: VegetationPathConfig) -> str:
    return str(
        cfg.raw.get("path_transmission", {}).get(
            "selected_path_weight",
            cfg.raw.get("selected_path_weight", "mean"),
        )
    )


def selected_path_weight_from_row(
    row: dict[str, Any] | pd.Series, cfg: VegetationPathConfig
) -> float:
    selected = selected_path_weight_name(cfg)
    col = (
        "path_vegetation_weight_length_decay"
        if selected == "length_decay"
        else "path_vegetation_weight_mean"
    )
    value = row.get(col)
    if value is None or pd.isna(value):
        return 0.0
    return float(np.clip(float(value), 0.0, 1.0))
