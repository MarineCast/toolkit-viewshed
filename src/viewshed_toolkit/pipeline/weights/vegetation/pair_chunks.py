# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

import json
import logging
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from viewshed_toolkit._internal.geo.geometry import safe_polygonal_union
from viewshed_toolkit._internal.geo.h3 import cell_to_polygon

from ...config import (
    DEFAULT_VEGETATION_PATH_INPUTS,
    DEFAULT_VEGETATION_PATH_OUTPUTS,
    get_h3_settings,
    metadata_sidecar_candidates,
    resolve_path,
)
from ...prepare.area.geometry import (
    h3_geometry_artifact_path,
    h3_geometry_metadata,
    load_h3_geometry_lookup,
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


from .candidate_pairs import materialize_candidate_pairs
from .path_config import (
    VegetationPathConfig,
    VegetationRasterData,
    load_vegetation_rasters_full,
)
from .ray_sampling import (
    _geom,
    _is_wgs84_crs,
    _project_geometry,
    _project_geometry_lookup,
    aggregate_ray_scores,
    cached_cell_points,
    pair_points,
    score_ray,
    selected_path_weight_from_row,
)


def iter_candidate_pair_chunks(
    cfg: VegetationPathConfig,
    *,
    chunk_size: int,
    limit: int | None = None,
    count_totals: dict[str, int] | None = None,
) -> Iterable[pd.DataFrame]:
    artifact = materialize_candidate_pairs(cfg)
    if count_totals is not None:
        count_totals.update(artifact.filter_counts)
    yielded = 0
    parquet = pq.ParquetFile(artifact.path)
    for batch in parquet.iter_batches(batch_size=chunk_size):
        if limit is not None:
            remaining = int(limit) - yielded
            if remaining <= 0:
                return
            if batch.num_rows > remaining:
                batch = batch.slice(0, remaining)
        chunk = batch.to_pandas()
        if chunk.empty:
            continue
        yielded += len(chunk)
        yield chunk
        if limit is not None and yielded >= int(limit):
            return


def _load_source_geometries(cfg: VegetationPathConfig) -> dict[str, BaseGeometry]:
    gdf = gpd.read_parquet(cfg.inputs["source_cells"])
    id_col = "h3_cell" if "h3_cell" in gdf.columns else cfg.columns["source_id"]
    columns = [id_col, "geometry"]
    return {
        str(cell_id): _geom(geometry)
        for cell_id, geometry in gdf[columns].itertuples(index=False, name=None)
    }


def _load_water_union(cfg: VegetationPathConfig, raster_crs=None) -> BaseGeometry | None:
    path = cfg.inputs.get("water_polygon")
    if path is None or not path.exists():
        return None
    water = gpd.read_parquet(path)
    if water.empty:
        return None
    if water.crs is None:
        raise ValueError(f"Water polygon has no CRS metadata: {path}")
    geom = safe_polygonal_union(
        water,
        target_crs=raster_crs if raster_crs is not None else "EPSG:4326",
    )
    return geom if geom is not None and not geom.is_empty else None


def _load_cached_h3_geometries(
    cfg: VegetationPathConfig,
    column: str,
    raster_crs,
) -> dict[str, BaseGeometry] | None:
    """Load canonical H3 geometry once instead of rebuilding it per chunk."""

    output_value = (cfg.raw_config.get("paths", {}) or {}).get("output_dir")
    if output_value is None:
        return None
    output_dir = resolve_path(output_value, cfg.config_dir)
    h3 = get_h3_settings(cfg.raw_config)
    resolution = int(h3.get("target_resolution", h3.get("source_resolution", 6)))
    path = h3_geometry_artifact_path(output_dir, resolution)
    if not path.exists():
        return None
    metadata = h3_geometry_metadata(path)
    artifact_crs = metadata.get("geometry_projected_crs")
    if column.endswith("_projected") and CRS.from_user_input(artifact_crs) != CRS.from_user_input(
        raster_crs
    ):
        if column == "water_geometry_projected":
            return None
        wgs84 = load_h3_geometry_lookup(path, "geometry_wgs84")
        return _project_geometry_lookup(
            wgs84,
            raster_crs,
        )
    return load_h3_geometry_lookup(path, column)


def _target_geometry(
    target_id: str,
    *,
    geometry_value: Any = None,
    target_geoms: dict[str, BaseGeometry] | None,
    water_union: BaseGeometry | None = None,
    derived_target_geoms: dict[str, tuple[BaseGeometry, str]] | None = None,
) -> tuple[BaseGeometry, str]:
    if target_geoms and target_id in target_geoms:
        return target_geoms[target_id], "visible_water_pixels"
    if derived_target_geoms is not None and target_id in derived_target_geoms:
        return derived_target_geoms[target_id]
    if geometry_value is not None and not pd.isna(geometry_value):
        full_geom = _geom(geometry_value)
    else:
        full_geom = cell_to_polygon(target_id)
    if water_union is not None:
        water_geom = full_geom.intersection(water_union)
        if water_geom is not None and not water_geom.is_empty:
            result = (water_geom, "water_intersection")
            if derived_target_geoms is not None:
                derived_target_geoms[target_id] = result
            return result
    result = (full_geom, "full_h3_fallback")
    if derived_target_geoms is not None:
        derived_target_geoms[target_id] = result
    return result


def _load_target_geometries(
    cfg: VegetationPathConfig,
) -> dict[str, BaseGeometry] | None:
    path = cfg.inputs.get("water_cells")
    if path is None:
        return None
    gdf = gpd.read_parquet(path)
    id_col = (
        cfg.columns["target_id"]
        if cfg.columns["target_id"] in gdf.columns
        else cfg.columns["target_id_output"]
    )
    columns = [id_col, "geometry"]
    return {
        str(cell_id): _geom(geometry)
        for cell_id, geometry in gdf[columns].itertuples(index=False, name=None)
    }


def _load_source_weights(cfg: VegetationPathConfig) -> dict[str, float]:
    if not bool(cfg.raw.get("combine", {}).get("include_source_cell_weight", True)):
        return {}
    path = cfg.inputs.get("source_cell_weights")
    if path is None or not path.exists():
        return {}
    df = pd.read_parquet(path)
    id_col = "h3_cell" if "h3_cell" in df.columns else cfg.columns["source_id"]
    weight_col = cfg.columns.get("source_weight_column", "land_source_vegetation_weight")
    if weight_col not in df.columns:
        return {}
    return {
        str(cell_id): float(weight)
        for cell_id, weight in df[[id_col, weight_col]].dropna().itertuples(index=False, name=None)
    }


def _geometry_for_id(
    cell_id: str,
    geoms: dict[str, BaseGeometry] | None,
    raster_crs,
) -> BaseGeometry:
    if geoms and cell_id in geoms:
        return geoms[cell_id]
    geom = cell_to_polygon(cell_id)
    if raster_crs is not None and not _is_wgs84_crs(raster_crs):
        transformer = Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True)
        geom = _project_geometry(geom, transformer)
    return geom


def _chunk_pair_bounds(
    pairs: pd.DataFrame,
    cfg: VegetationPathConfig,
    *,
    source_geoms: dict[str, BaseGeometry],
    target_geoms: dict[str, BaseGeometry] | None,
    raster_crs,
    pad_pixels: int = 2,
) -> tuple[float, float, float, float]:
    bounds: list[tuple[float, float, float, float]] = []
    source_col = cfg.columns["source_id"]
    target_col = cfg.columns["target_id"]
    for source_id in pairs[source_col].astype(str).dropna().unique():
        bounds.append(_geometry_for_id(str(source_id), source_geoms, raster_crs).bounds)
    for target_id in pairs[target_col].astype(str).dropna().unique():
        bounds.append(_geometry_for_id(str(target_id), target_geoms, raster_crs).bounds)
    if not bounds:
        raise ValueError("Cannot derive vegetation raster window for empty pair chunk.")
    minx = min(b[0] for b in bounds)
    miny = min(b[1] for b in bounds)
    maxx = max(b[2] for b in bounds)
    maxy = max(b[3] for b in bounds)
    pad = max(0, int(pad_pixels)) * float(cfg.resolution_m)
    return (minx - pad, miny - pad, maxx + pad, maxy + pad)


def _clear_sky_weight(
    clear_sky_weight: Any,
    terrain_visible_clear_sky: Any,
) -> float:
    if clear_sky_weight is not None and pd.notna(clear_sky_weight):
        return float(clear_sky_weight)
    if terrain_visible_clear_sky is not None:
        return 1.0 if bool(terrain_visible_clear_sky) else 0.0
    return 1.0


def process_pairs(
    cfg: VegetationPathConfig,
    pairs: pd.DataFrame,
    *,
    progress: Any | None = None,
    derived_source_geoms: dict[str, BaseGeometry] | None = None,
    missing_source_ids: set[str] | None = None,
    source_geoms: dict[str, BaseGeometry] | None = None,
    target_geoms: dict[str, BaseGeometry] | None = None,
    source_weights: dict[str, float] | None = None,
    raster_crs=None,
    raster_data: VegetationRasterData | None = None,
    water_union: BaseGeometry | None = None,
    point_cache: dict[tuple[str, str], list[Point]] | None = None,
    derived_target_geoms: dict[str, tuple[BaseGeometry, str]] | None = None,
    chunk_id: int | None = None,
    max_debug_rows: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    source_geoms = source_geoms if source_geoms is not None else _load_source_geometries(cfg)
    target_geoms = target_geoms if target_geoms is not None else _load_target_geometries(cfg)
    source_weights = source_weights if source_weights is not None else _load_source_weights(cfg)
    point_cache = point_cache if point_cache is not None else {}
    distance_cache: dict[tuple[str, str], float] = {}
    derived_source_geoms = derived_source_geoms if derived_source_geoms is not None else {}
    derived_target_geoms = derived_target_geoms if derived_target_geoms is not None else {}
    missing_source_ids = missing_source_ids if missing_source_ids is not None else set()
    sampling = cfg.raw.get("sampling", {})
    n_points = int(sampling.get("n_points_per_pair", 15))
    pair_mode = sampling.get("pair_mode", "paired")
    bool(sampling.get("include_centroid", True))
    global_seed = int(sampling.get("random_seed", 42))
    max_attempts = int(sampling.get("max_sampling_attempts_per_polygon", 1000))
    requested_rays = n_points if pair_mode == "paired" else n_points * n_points
    if pair_mode == "cross_product" and requested_rays > 100:
        LOGGER.warning("cross_product mode will compute %d rays per pair.", requested_rays)

    debug_rows = []
    debug_cfg = cfg.raw.get("debug", {})
    max_debug_pairs = int(debug_cfg.get("max_pairs", 100))
    include_debug_geom = bool(debug_cfg.get("include_geometry", True))
    write_debug = cfg.outputs.get("debug_rays") is not None
    rows = []
    if raster_data is None:
        raster_data = load_vegetation_rasters_full(cfg)
    if raster_crs is None:
        raster_crs = raster_data.raster_crs
    if water_union is None:
        water_union = _load_water_union(cfg)
    transmission = raster_data.transmission
    obstruction = raster_data.obstruction
    chm_obstruction = raster_data.chm_obstruction
    landcover = raster_data.landcover
    raster_shape = raster_data.raster_shape
    transform = raster_data.transform
    to_raster = (
        None
        if raster_crs is None or _is_wgs84_crs(raster_crs)
        else Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True)
    )
    debug_to_wgs84 = (
        None
        if raster_crs is None or _is_wgs84_crs(raster_crs)
        else Transformer.from_crs(raster_crs, "EPSG:4326", always_xy=True)
    )

    source_id_col = cfg.columns["source_id"]
    target_id_col = cfg.columns["target_id"]
    target_id_output_col = cfg.columns["target_id_output"]
    clear_sky_weight_col = cfg.columns["clear_sky_weight"]
    source_lon_col = cfg.columns.get("source_lon", "source_lon")
    source_lat_col = cfg.columns.get("source_lat", "source_lat")
    target_lon_col = cfg.columns.get("target_lon", "target_lon")
    target_lat_col = cfg.columns.get("target_lat", "target_lat")
    columns = list(pairs.columns)
    col_idx = {name: idx for idx, name in enumerate(columns)}

    def _value(row_values: tuple[Any, ...], column: str) -> Any:
        idx = col_idx.get(column)
        return row_values[idx] if idx is not None else None

    for idx, row_values in enumerate(pairs.itertuples(index=False, name=None)):
        source_id = str(_value(row_values, source_id_col))
        target_id = str(_value(row_values, target_id_col))
        src_lon = _value(row_values, source_lon_col)
        src_lat = _value(row_values, source_lat_col)
        tgt_lon = _value(row_values, target_lon_col)
        tgt_lat = _value(row_values, target_lat_col)
        has_point_coords = all(pd.notna(v) for v in [src_lon, src_lat, tgt_lon, tgt_lat])
        if has_point_coords:
            source_geom = Point(float(src_lon), float(src_lat))
            target_geom = Point(float(tgt_lon), float(tgt_lat))
            if to_raster is not None:
                source_geom = _project_geometry(source_geom, to_raster)
                target_geom = _project_geometry(target_geom, to_raster)
        else:
            if source_id in source_geoms:
                source_geom = source_geoms[source_id]
            elif source_id in derived_source_geoms:
                source_geom = derived_source_geoms[source_id]
            else:
                if (
                    cfg.raw.get("geometry", {}).get("missing_source_policy", "derive_h3_polygon")
                    != "derive_h3_polygon"
                ):
                    raise ValueError(f"Missing source geometry for {source_id}")
                if not missing_source_ids:
                    LOGGER.warning(
                        "Some clear-sky source cells are not present in source_cells; deriving full H3 polygons. "
                        "This usually means LAND_AREA.parquet and clear-sky partitions were built at different H3 resolutions. "
                        "Suppressing per-cell warnings for the rest of this run."
                    )
                missing_source_ids.add(source_id)
                source_geom = cell_to_polygon(source_id)
                if to_raster is not None:
                    source_geom = _project_geometry(source_geom, to_raster)
                derived_source_geoms[source_id] = source_geom
            target_geom, target_geometry_source = _target_geometry(
                target_id,
                geometry_value=_value(row_values, "geometry"),
                target_geoms=target_geoms,
                water_union=water_union,
                derived_target_geoms=derived_target_geoms,
            )
            if to_raster is not None and target_geometry_source != "visible_water_pixels":
                target_geom = _project_geometry(target_geom, to_raster)
        if has_point_coords:
            target_geometry_source = "full_h3_fallback"
        source_points = cached_cell_points(
            point_cache,
            role="source",
            cell_id=source_id,
            geometry=source_geom,
            n_points=n_points,
            sampling=sampling,
            global_seed=global_seed,
            max_attempts=max_attempts,
        )
        target_points = cached_cell_points(
            point_cache,
            role="target",
            cell_id=target_id,
            geometry=target_geom,
            n_points=n_points,
            sampling=sampling,
            global_seed=global_seed,
            max_attempts=max_attempts,
        )
        rays = pair_points(source_points, target_points, pair_mode)
        scores = [
            score_ray(
                src,
                tgt,
                transform=transform,
                raster_shape=raster_shape,
                transmission=transmission,
                obstruction=obstruction,
                chm_obstruction=chm_obstruction,
                landcover=landcover,
                cfg=cfg,
            )
            for src, tgt in rays
        ]
        distance_km = _h3_centroid_distance_km_cached(
            source_id,
            target_id,
            distance_cache,
        )
        out = {
            source_id_col: source_id,
            target_id_output_col: target_id,
            "source_h3": source_id,
            "target_h3": target_id,
            "distance_km": distance_km,
            "clear_sky_visibility_weight": _clear_sky_weight(
                _value(row_values, clear_sky_weight_col),
                _value(row_values, "terrain_visible_clear_sky"),
            ),
        }
        out.update(aggregate_ray_scores(scores, requested_rays))
        path_trans = selected_path_weight_from_row(out, cfg)
        out["path_vegetation_weight_selected"] = path_trans
        out["target_geometry_source"] = target_geometry_source
        out["vegetation_path_adjusted_weight"] = path_trans
        out["bare_earth_composite_weight"] = out["clear_sky_visibility_weight"]
        out["base_visibility_weight"] = out["clear_sky_visibility_weight"]
        weight_terrain = _value(row_values, "weight_terrain")
        terrain_weight = _value(row_values, "terrain_weight")
        terrain_support = _value(row_values, "terrain_visibility_support")
        weight_distance = _value(row_values, "weight_distance")
        if weight_terrain is not None:
            out["weight_terrain"] = weight_terrain
        elif terrain_weight is not None:
            out["weight_terrain"] = terrain_weight
        elif terrain_support is not None:
            out["weight_terrain"] = terrain_support
        if weight_distance is not None:
            out["weight_distance"] = weight_distance
        out["mean_obstruction_along_path"] = out.get("mean_surface_obstruction_along_land")
        out["max_obstruction_along_path"] = out.get("max_surface_obstruction_along_land")
        out["mean_canopy_height_along_path"] = np.nan
        out["max_canopy_height_along_path"] = np.nan
        out["forest_fraction_along_path"] = np.nan
        out["vegetation_transmission_weight"] = path_trans
        source_weight = _source_weight(source_id, source_weights, cfg)
        out["source_vegetation_weight"] = source_weight
        out["source_cell_vegetation_weight"] = source_weight
        out["combined_vegetation_weight"] = float(source_weight) * float(path_trans)
        out["vegetation_adjusted_weight"] = out["combined_vegetation_weight"]
        out["vegetation_and_source_adjusted_weight"] = out["combined_vegetation_weight"]
        out["run_version"] = cfg.run_version
        out["config_hash"] = cfg.config_hash
        if chunk_id is not None:
            out["chunk_id"] = int(chunk_id)
        chunk_parent_h3 = _value(row_values, "chunk_parent_h3")
        if chunk_parent_h3 is not None and pd.notna(chunk_parent_h3):
            out["chunk_parent_h3"] = str(chunk_parent_h3)
        rows.append(out)
        if (
            write_debug
            and idx < max_debug_pairs
            and (max_debug_rows is None or len(debug_rows) < max_debug_rows)
        ):
            for ray_idx, ((src, tgt), score) in enumerate(zip(rays, scores)):
                debug_src = (
                    Point(*debug_to_wgs84.transform(src.x, src.y))
                    if debug_to_wgs84 is not None
                    else src
                )
                debug_tgt = (
                    Point(*debug_to_wgs84.transform(tgt.x, tgt.y))
                    if debug_to_wgs84 is not None
                    else tgt
                )
                debug = {
                    cfg.columns["source_id"]: source_id,
                    cfg.columns["target_id_output"]: target_id,
                    "ray_index": ray_idx,
                    "source_lon": debug_src.x,
                    "source_lat": debug_src.y,
                    "target_lon": debug_tgt.x,
                    "target_lat": debug_tgt.y,
                    "debug_geometry_crs": "EPSG:4326",
                    **score,
                }
                if include_debug_geom:
                    debug["geometry"] = LineString([debug_src, debug_tgt])
                debug_rows.append(debug)
                if max_debug_rows is not None and len(debug_rows) >= max_debug_rows:
                    break
        if progress is not None:
            progress.update(1)
    pair_df = pd.DataFrame(rows)
    debug_df = None
    if debug_rows:
        debug_df = (
            gpd.GeoDataFrame(debug_rows, geometry="geometry", crs="EPSG:4326")
            if "geometry" in debug_rows[0]
            else pd.DataFrame(debug_rows)
        )
    return pair_df, debug_df


def _chunk_metadata_path(chunk_path: Path) -> Path:
    return metadata_sidecar_candidates(chunk_path)[0]


def _metadata_compatible(meta_path: Path, cfg: VegetationPathConfig) -> bool:
    meta_path = Path(meta_path)
    candidates = [meta_path]
    if meta_path.name.endswith("_metadata.json"):
        chunk_name = meta_path.name[: -len("_metadata.json")] + ".parquet"
        candidates.extend(metadata_sidecar_candidates(meta_path.with_name(chunk_name)))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text())
        except Exception:
            return False
        expected_pair_path, expected_pair_mtime = _pair_input_signature(cfg)
        return (
            payload.get("config_hash") == cfg.config_hash
            and payload.get("run_version") == cfg.run_version
            and payload.get("pair_input_path") == expected_pair_path
            and str(payload.get("pair_input_mtime_ns")) == str(expected_pair_mtime)
        )
    return False


def _pair_input_signature(cfg: VegetationPathConfig) -> tuple[str, int | None]:
    path = cfg.inputs["clear_sky_pairs"]
    mtime = path.stat().st_mtime_ns if path.exists() else None
    return str(path), mtime


def _chunk_output_row_count(chunk_path: Path) -> int:
    for meta_path in metadata_sidecar_candidates(chunk_path):
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
            value = payload.get("output_row_count")
            if value is not None:
                return int(value)
        except Exception:
            pass
    return int(pq.ParquetFile(chunk_path).metadata.num_rows)


def _vegetation_pair_chunk_for_storage(pair_df: pd.DataFrame) -> pd.DataFrame:
    out = pair_df[
        [
            c
            for c in ["source_h3", "target_h3", "weight_landcover", "weight_chm"]
            if c in pair_df.columns
        ]
    ].copy()
    for weight_col in ("weight_landcover", "weight_chm"):
        if weight_col in out.columns:
            out[weight_col] = (
                pd.to_numeric(out[weight_col], errors="coerce")
                .fillna(1.0)
                .clip(0.0, 1.0)
                .astype("float32")
            )
    return out


def _source_weight(source_id: str, weights: dict[str, float], cfg: VegetationPathConfig) -> float:
    if not bool(cfg.raw.get("combine", {}).get("include_source_cell_weight", True)):
        return 1.0
    if source_id in weights:
        return float(weights[source_id])
    policy = cfg.raw.get("combine", {}).get("source_weight_missing_policy", "use_1")
    if policy == "use_1":
        return 1.0
    if policy == "use_0":
        return 0.0
    raise ValueError(f"Missing source vegetation weight for {source_id}")


def _h3_centroid_distance_km_cached(
    source_id: str,
    target_id: str,
    cache: dict[tuple[str, str], float],
) -> float:
    key = (source_id, target_id)
    if key in cache:
        return cache[key]
    import h3

    source_lat, source_lon = h3.cell_to_latlng(source_id)
    target_lat, target_lon = h3.cell_to_latlng(target_id)
    phi1 = np.radians(float(source_lat))
    phi2 = np.radians(float(target_lat))
    dphi = np.radians(float(target_lat) - float(source_lat))
    dlambda = np.radians(float(target_lon) - float(source_lon))
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    distance = float(2.0 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0))))
    cache[key] = distance
    return distance
