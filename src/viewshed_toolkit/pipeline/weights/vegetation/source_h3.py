# =============================================================================
# Source-cell vegetation rasters and H3 source weights
# =============================================================================

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.mask import mask
from shapely import wkb
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from ...config import (
    DEFAULT_VEGETATION_PATH_INPUTS,
    DEFAULT_VEGETATION_PATH_OUTPUTS,
    DEFAULT_VEGETATION_WEIGHT_INPUTS,
    DEFAULT_VEGETATION_WEIGHT_OUTPUTS,
)
from ...prepare.vegetation.assets import (
    profile_timer,
)

LOGGER = logging.getLogger(__name__)
REQUIRED_CLASS_KEYS = (
    "class_name",
    "obstruction_multiplier",
    "obstruction_floor",
    "source_access_weight",
)
DEFAULT_INPUT_PATH_TEMPLATES = dict(DEFAULT_VEGETATION_WEIGHT_INPUTS)
DEFAULT_OUTPUT_PATH_TEMPLATES = dict(DEFAULT_VEGETATION_WEIGHT_OUTPUTS)


from .support_rasters import (
    VegetationWeightsConfig,
    assert_land_source_h3_resolution,
    assert_rasters_cover_config_bbox,
    assert_weight_rasters_aligned,
    class_weight_table,
    load_vegetation_weights_config,
    write_class_weights_csv,
    write_weight_rasters,
)


def _valid_values(arr: np.ndarray, nodata: float) -> np.ndarray:
    data = arr.compressed() if np.ma.isMaskedArray(arr) else arr.reshape(-1)
    data = data[np.isfinite(data)]
    return data[data != nodata]


def _stats(prefix: str, values: np.ndarray, stats: Iterable[str]) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {f"valid_pixel_count_{prefix}": int(values.size)}
    if values.size == 0:
        for stat in stats:
            out[f"{prefix}_{stat}"] = None
        return out
    for stat in stats:
        if stat == "mean":
            out[f"{prefix}_mean"] = float(np.mean(values))
        elif stat == "median":
            out[f"{prefix}_median"] = float(np.median(values))
        elif stat == "max":
            out[f"{prefix}_max"] = float(np.max(values))
        elif stat == "min":
            out[f"{prefix}_min"] = float(np.min(values))
        elif stat == "std":
            out[f"{prefix}_std"] = float(np.std(values))
        else:
            raise ValueError(f"Unsupported aggregation stat: {stat}")
    return out


def _geometry_from_value(value: Any):
    if isinstance(value, bytes):
        return wkb.loads(value)
    if isinstance(value, str):
        return wkb.loads(bytes.fromhex(value))
    return value


def _maybe_decode_geometry_column(name: str, value: Any) -> Any:
    if "geometry" not in name.lower():
        return value
    if isinstance(value, (bytes, str)):
        try:
            return _geometry_from_value(value)
        except Exception:
            return value
    return value


def _mask_values(
    src: rasterio.io.DatasetReader, geom: Any, nodata: float, *, all_touched: bool
) -> np.ndarray:
    try:
        arr, _ = mask(src, [mapping(geom)], crop=True, all_touched=all_touched, filled=False)
    except ValueError:
        return np.array([], dtype="float32")
    return _valid_values(arr[0], nodata)


def _landcover_values(
    src: rasterio.io.DatasetReader, geom: Any, nodata: float, *, all_touched: bool
) -> np.ndarray:
    try:
        arr, _ = mask(src, [mapping(geom)], crop=True, all_touched=all_touched, filled=False)
    except ValueError:
        return np.array([], dtype="int32")
    return _valid_values(arr[0], nodata).astype("int32")


def _progress(iterable: Iterable[Any], *, total: int, desc: str):
    try:
        from tqdm.auto import tqdm

        return tqdm(iterable, total=total, desc=desc, unit="cell")
    except Exception:
        return iterable


def _base_h3_output_row(
    row: pd.Series,
    source_columns: Sequence[str],
    geom_col: str,
    geom: Any,
    *,
    drop_geometry: bool,
) -> dict[str, Any]:
    out = {
        col: _maybe_decode_geometry_column(col, row[col])
        for col in source_columns
        if col != geom_col or not drop_geometry
    }
    if not drop_geometry:
        out[geom_col] = geom
    return out


def _update_landcover_stats(
    out: dict[str, Any],
    lc_vals: np.ndarray,
    class_lookup: dict[int, dict[str, Any]],
    configured_codes: Sequence[int],
) -> None:
    out["valid_pixel_count_landcover"] = int(lc_vals.size)
    if lc_vals.size:
        codes, counts = np.unique(lc_vals, return_counts=True)
        dominant_idx = int(np.argmax(counts))
        dominant_code = int(codes[dominant_idx])
        out["dominant_landcover_code"] = dominant_code
        out["dominant_landcover_class_name"] = class_lookup.get(dominant_code, {}).get("class_name")
        out["dominant_landcover_group"] = class_lookup.get(dominant_code, {}).get("group")
        out["landcover_class_count_total"] = int(lc_vals.size)
        count_map = {int(code): int(count) for code, count in zip(codes, counts)}
        for code in configured_codes:
            out[f"landcover_prop_{code}"] = count_map.get(int(code), 0) / float(lc_vals.size)
    else:
        out["dominant_landcover_code"] = None
        out["dominant_landcover_class_name"] = None
        out["dominant_landcover_group"] = None
        out["landcover_class_count_total"] = 0
        for code in configured_codes:
            out[f"landcover_prop_{code}"] = None


def _update_final_vegetation_weight(
    out: dict[str, Any],
    h3_cfg: dict[str, Any],
    *,
    valid: bool,
) -> None:
    out["vegetation_weight_valid"] = bool(valid)
    out["raster_coverage_fraction"] = 1.0 if valid else 0.0
    if valid:
        final_cfg = h3_cfg.get("final_weight", {})
        final = float(out.get("source_access_weight_mean") or 0.0) * float(
            out.get("surface_transmission_mean") or 0.0
        )
        final = float(
            np.clip(
                final,
                float(final_cfg.get("clamp_min", 0.0)),
                float(final_cfg.get("clamp_max", 1.0)),
            )
        )
    else:
        final_cfg = h3_cfg.get("final_weight", {})
        invalid_policy = str(final_cfg.get("invalid_policy", "neutral"))
        if invalid_policy == "neutral":
            final = float(final_cfg.get("invalid_value", 1.0))
        elif invalid_policy == "zero":
            final = 0.0
        else:
            raise ValueError(
                "vegetation_weights.h3_aggregation.final_weight.invalid_policy "
                "must be one of: neutral, zero"
            )
    out[h3_cfg.get("final_weight", {}).get("name", "land_source_vegetation_weight")] = final


def _common_h3_aggregation_inputs(
    cfg: VegetationWeightsConfig,
) -> tuple[
    dict[str, Any],
    str,
    str,
    dict[str, Any],
    gpd.GeoDataFrame,
    dict[int, dict[str, Any]],
    list[int],
]:
    h3_cfg = cfg.raw.get("h3_aggregation", {})
    id_col = str(h3_cfg.get("id_column", "h3_cell"))
    geom_col = str(h3_cfg.get("geometry_column", "geometry"))
    metrics = h3_cfg.get("metrics", {})
    source_df = gpd.read_parquet(cfg.inputs["land_source_cells"])
    if geom_col not in source_df.columns:
        raise ValueError(
            f"Configured geometry column {geom_col!r} is missing from land source cells."
        )
    if source_df.crs is None:
        raise ValueError(
            f"Source cell geometry file has no CRS metadata: {cfg.inputs['land_source_cells']}. "
            "Cannot safely rasterize H3 cells onto vegetation rasters."
        )
    if source_df.geometry.name != geom_col:
        source_df = source_df.set_geometry(geom_col)
    assert_land_source_h3_resolution(cfg)
    class_lookup = {
        code: {
            "class_name": values.get("class_name"),
            "group": values.get("group", ""),
        }
        for code, values in cfg.landcover_classes.items()
    }
    configured_codes = sorted(cfg.landcover_classes)
    return h3_cfg, id_col, geom_col, metrics, source_df, class_lookup, configured_codes


def _project_source_cells_to_raster_crs(
    source_gdf: gpd.GeoDataFrame,
    raster_crs: Any,
    *,
    source_path: Path | None = None,
    raster_path: Path | None = None,
) -> gpd.GeoDataFrame:
    if source_gdf.crs is None:
        location = f": {source_path}" if source_path is not None else ""
        raise ValueError(
            f"Source cell geometry file has no CRS metadata{location}. "
            "Cannot safely rasterize H3 cells onto vegetation rasters."
        )
    if raster_crs is None:
        location = f": {raster_path}" if raster_path is not None else ""
        raise ValueError(f"Vegetation raster has no CRS metadata{location}.")

    LOGGER.info("Vegetation source geometry CRS: %s", source_gdf.crs)
    LOGGER.info("Vegetation raster CRS: %s", raster_crs)
    if source_gdf.crs == raster_crs:
        return source_gdf
    return source_gdf.to_crs(raster_crs)


def _aggregate_to_h3_mask_per_cell(
    cfg: VegetationWeightsConfig, raster_paths: dict[str, Path]
) -> pd.DataFrame:
    h3_cfg, _id_col, geom_col, metrics, source_df, class_lookup, configured_codes = (
        _common_h3_aggregation_inputs(cfg)
    )
    out_rows = []

    with (
        rasterio.open(raster_paths["surface_obstruction"]) as surface_src,
        rasterio.open(raster_paths["surface_transmission"]) as transmission_src,
        rasterio.open(raster_paths["lc_source_access_weight"]) as access_src,
        rasterio.open(raster_paths["chm_obstruction"]) as chm_src,
        rasterio.open(cfg.inputs["landcover"]) as lc_src,
    ):
        projected_source_df = _project_source_cells_to_raster_crs(
            source_df,
            surface_src.crs,
            source_path=cfg.inputs["land_source_cells"],
            raster_path=raster_paths["surface_obstruction"],
        )

        for (_, row), (_, projected_row) in zip(
            _progress(
                source_df.iterrows(),
                total=len(source_df),
                desc="Vegetation H3 mask aggregation",
            ),
            projected_source_df.iterrows(),
        ):
            geom = _geometry_from_value(row[geom_col])
            raster_geom = _geometry_from_value(projected_row[geom_col])
            out = _base_h3_output_row(
                row, source_df.columns, geom_col, geom, drop_geometry=cfg.drop_geometry
            )
            surface_vals = _mask_values(
                surface_src,
                raster_geom,
                cfg.nodata.get("output_float", -9999.0),
                all_touched=cfg.all_touched,
            )
            transmission_vals = _mask_values(
                transmission_src,
                raster_geom,
                cfg.nodata.get("output_float", -9999.0),
                all_touched=cfg.all_touched,
            )
            access_vals = _mask_values(
                access_src,
                raster_geom,
                cfg.nodata.get("output_float", -9999.0),
                all_touched=cfg.all_touched,
            )
            chm_vals = _mask_values(
                chm_src,
                raster_geom,
                cfg.nodata.get("output_float", -9999.0),
                all_touched=cfg.all_touched,
            )
            lc_vals = _landcover_values(
                lc_src,
                raster_geom,
                cfg.nodata.get("landcover", 0.0),
                all_touched=cfg.all_touched,
            )

            out.update(
                _stats(
                    "surface_obstruction",
                    surface_vals,
                    metrics.get("surface_obstruction", {}).get(
                        "stats", ["mean", "median", "max", "std"]
                    ),
                )
            )
            out.update(
                _stats(
                    "surface_transmission",
                    transmission_vals,
                    metrics.get("surface_transmission", {}).get(
                        "stats", ["mean", "median", "min", "std"]
                    ),
                )
            )
            out.update(
                _stats(
                    "source_access_weight",
                    access_vals,
                    metrics.get("source_access_weight", {}).get("stats", ["mean", "median", "max"]),
                )
            )
            out.update(
                _stats(
                    "chm_obstruction",
                    chm_vals,
                    metrics.get("chm_obstruction", {}).get("stats", ["mean", "median", "max"]),
                )
            )
            _update_landcover_stats(out, lc_vals, class_lookup, configured_codes)

            valid = surface_vals.size > 0 and transmission_vals.size > 0 and access_vals.size > 0
            _update_final_vegetation_weight(out, h3_cfg, valid=valid)
            out_rows.append(out)

    result = pd.DataFrame(out_rows)
    LOGGER.info(
        "Aggregated vegetation weights for %d H3 cells with mask_per_cell method; valid=%d",
        len(result),
        int(result["vegetation_weight_valid"].sum()),
    )
    return result


def _flat_valid_values(values: np.ndarray, nodata: float) -> np.ndarray:
    data = values[np.isfinite(values)]
    return data[data != nodata]


@dataclass(frozen=True)
class _LabelPixelGroups:
    labels: np.ndarray
    starts: np.ndarray
    counts: np.ndarray
    pixel_indices: np.ndarray


def _label_pixel_groups(label_raster: np.ndarray) -> _LabelPixelGroups:
    label_flat = label_raster.reshape(-1)
    labeled_positions = np.flatnonzero(label_flat > 0)
    if labeled_positions.size == 0:
        return _LabelPixelGroups(
            labels=np.array([], dtype=np.int32),
            starts=np.array([], dtype=np.int64),
            counts=np.array([], dtype=np.int64),
            pixel_indices=np.array([], dtype=np.intp),
        )

    labels_at_positions = label_flat[labeled_positions]
    order = np.argsort(labels_at_positions, kind="stable")
    sorted_labels = labels_at_positions[order].astype("int32", copy=False)
    sorted_positions = labeled_positions[order]
    labels, starts, counts = np.unique(sorted_labels, return_index=True, return_counts=True)
    return _LabelPixelGroups(
        labels=labels.astype("int32", copy=False),
        starts=starts.astype("int64", copy=False),
        counts=counts.astype("int64", copy=False),
        pixel_indices=sorted_positions.astype(np.intp, copy=False),
    )


def _aggregate_to_h3_rasterized(
    cfg: VegetationWeightsConfig, raster_paths: dict[str, Path]
) -> pd.DataFrame:
    h3_cfg, _id_col, geom_col, metrics, source_df, class_lookup, configured_codes = (
        _common_h3_aggregation_inputs(cfg)
    )
    if geom_col not in source_df.columns:
        raise ValueError(
            f"Configured geometry column {geom_col!r} is missing from land source cells."
        )

    with (
        rasterio.open(raster_paths["surface_obstruction"]) as surface_src,
        rasterio.open(raster_paths["surface_transmission"]) as transmission_src,
        rasterio.open(raster_paths["lc_source_access_weight"]) as access_src,
        rasterio.open(raster_paths["chm_obstruction"]) as chm_src,
        rasterio.open(cfg.inputs["landcover"]) as lc_src,
    ):
        assert_weight_rasters_aligned(
            [
                Path(surface_src.name),
                Path(transmission_src.name),
                Path(access_src.name),
                Path(chm_src.name),
                Path(lc_src.name),
            ]
        )
        shape = surface_src.shape
        transform = surface_src.transform
        raster_crs = surface_src.crs
        surface = surface_src.read(1)
        transmission = transmission_src.read(1)
        access = access_src.read(1)
        chm = chm_src.read(1)
        landcover = lc_src.read(1)

    projected_source_df = _project_source_cells_to_raster_crs(
        source_df,
        raster_crs,
        source_path=cfg.inputs["land_source_cells"],
        raster_path=raster_paths["surface_obstruction"],
    )

    geometries: list[Any] = []
    shapes: list[tuple[Any, int]] = []
    for label, ((_idx, row), (_projected_idx, projected_row)) in enumerate(
        _progress(
            zip(source_df.iterrows(), projected_source_df.iterrows()),
            total=len(source_df),
            desc="Vegetation H3 geometry prep",
        ),
        start=1,
    ):
        geom = _geometry_from_value(row[geom_col])
        raster_geom = _geometry_from_value(projected_row[geom_col])
        geometries.append(geom)
        if raster_geom is not None and not raster_geom.is_empty:
            shapes.append((mapping(raster_geom), label))

    LOGGER.info("Rasterizing %d vegetation H3 source-cell geometries", len(shapes))
    labels = (
        rasterize(
            shapes,
            out_shape=shape,
            fill=0,
            transform=transform,
            dtype="int32",
            all_touched=cfg.all_touched,
        )
        if shapes
        else np.zeros(shape, dtype="int32")
    )
    LOGGER.info("Finished rasterizing vegetation H3 source-cell labels")
    groups = _label_pixel_groups(labels)
    label_slices = {
        int(label): (int(start), int(count))
        for label, start, count in zip(groups.labels, groups.starts, groups.counts)
    }
    LOGGER.info(
        "Prepared grouped vegetation H3 label index: labels=%d labeled_pixels=%d",
        len(label_slices),
        int(groups.pixel_indices.size),
    )
    surface_flat = surface.reshape(-1)
    transmission_flat = transmission.reshape(-1)
    access_flat = access.reshape(-1)
    chm_flat = chm.reshape(-1)
    lc_flat = landcover.reshape(-1)
    out_rows = []
    output_nodata = cfg.nodata.get("output_float", -9999.0)
    lc_nodata = cfg.nodata.get("landcover", 0.0)

    with profile_timer(
        "aggregate_h3_rasterized_grouped",
        cells=len(source_df),
        labeled_pixels=int(groups.pixel_indices.size),
    ):
        for label, (_idx, row) in enumerate(
            _progress(
                source_df.iterrows(),
                total=len(source_df),
                desc="Vegetation H3 raster aggregation",
            ),
            start=1,
        ):
            geom = geometries[label - 1]
            out = _base_h3_output_row(
                row, source_df.columns, geom_col, geom, drop_geometry=cfg.drop_geometry
            )
            group = label_slices.get(label)
            if group is None:
                pixel_indices = np.array([], dtype=np.intp)
                raster_pixel_count = 0
            else:
                start, count = group
                pixel_indices = groups.pixel_indices[start : start + count]
                raster_pixel_count = count

            surface_vals = _flat_valid_values(surface_flat[pixel_indices], output_nodata)
            transmission_vals = _flat_valid_values(transmission_flat[pixel_indices], output_nodata)
            access_vals = _flat_valid_values(access_flat[pixel_indices], output_nodata)
            chm_vals = _flat_valid_values(chm_flat[pixel_indices], output_nodata)
            lc_vals = _flat_valid_values(lc_flat[pixel_indices], lc_nodata).astype("int32")

            out["raster_pixel_count"] = int(raster_pixel_count)
            out["valid_pixel_count"] = int(surface_vals.size)
            out["raster_nodata_fraction"] = (
                None
                if raster_pixel_count == 0
                else float(np.clip(1.0 - (surface_vals.size / float(raster_pixel_count)), 0.0, 1.0))
            )
            out.update(
                _stats(
                    "surface_obstruction",
                    surface_vals,
                    metrics.get("surface_obstruction", {}).get(
                        "stats", ["mean", "median", "max", "std"]
                    ),
                )
            )
            out.update(
                _stats(
                    "surface_transmission",
                    transmission_vals,
                    metrics.get("surface_transmission", {}).get(
                        "stats", ["mean", "median", "min", "std"]
                    ),
                )
            )
            out.update(
                _stats(
                    "source_access_weight",
                    access_vals,
                    metrics.get("source_access_weight", {}).get("stats", ["mean", "median", "max"]),
                )
            )
            out.update(
                _stats(
                    "chm_obstruction",
                    chm_vals,
                    metrics.get("chm_obstruction", {}).get("stats", ["mean", "median", "max"]),
                )
            )
            _update_landcover_stats(out, lc_vals, class_lookup, configured_codes)

            valid = surface_vals.size > 0 and transmission_vals.size > 0 and access_vals.size > 0
            _update_final_vegetation_weight(out, h3_cfg, valid=valid)
            out_rows.append(out)

    result = pd.DataFrame(out_rows)
    LOGGER.info(
        "Aggregated vegetation weights for %d H3 cells with rasterized method; valid=%d",
        len(result),
        int(result["vegetation_weight_valid"].sum()),
    )
    return result


def aggregate_to_h3(cfg: VegetationWeightsConfig, raster_paths: dict[str, Path]) -> pd.DataFrame:
    method = str(cfg.raw.get("h3_aggregation", {}).get("method", "rasterized"))
    if method == "mask_per_cell":
        return _aggregate_to_h3_mask_per_cell(cfg, raster_paths)
    if method == "rasterized":
        return _aggregate_to_h3_rasterized(cfg, raster_paths)
    raise ValueError(
        "vegetation_weights.h3_aggregation.method must be one of: rasterized, mask_per_cell"
    )


def write_h3_outputs(cfg: VegetationWeightsConfig, df: pd.DataFrame) -> Path:
    out = cfg.outputs.get("h3_cell_weights_parquet")
    if out is None:
        raise ValueError("outputs.h3_cell_weights_parquet is required.")
    out.parent.mkdir(parents=True, exist_ok=True)
    geom_col = str(cfg.raw.get("h3_aggregation", {}).get("geometry_column", "geometry"))
    if geom_col in df.columns and not cfg.drop_geometry:
        gdf = gpd.GeoDataFrame(df, geometry=geom_col, crs="EPSG:4326")
        for col in gdf.columns:
            if col != geom_col and "geometry" in col.lower():
                non_null = gdf[col].dropna()
                if not non_null.empty and isinstance(non_null.iloc[0], BaseGeometry):
                    gdf[col] = gpd.GeoSeries(gdf[col], crs="EPSG:4326")
        gdf.to_parquet(out, index=False)
    else:
        df.to_parquet(out, index=False)
    LOGGER.info("Wrote H3 vegetation weights parquet: %s", out)
    csv_out = cfg.outputs.get("h3_cell_weights_csv")
    if csv_out is not None:
        df.to_csv(csv_out, index=False)
        LOGGER.info("Wrote H3 vegetation weights CSV: %s", csv_out)
    return out


def run_vegetation_weights(
    config_path: str | Path | VegetationWeightsConfig,
    *,
    overwrite: bool | None = None,
    dry_run: bool = False,
    resolution_m: int | None = None,
    h3_resolution: int | None = None,
    skip_rasters: bool = False,
    skip_h3: bool = False,
) -> dict[str, Path | None]:
    if isinstance(config_path, VegetationWeightsConfig):
        cfg = config_path
        if resolution_m is not None and int(resolution_m) != cfg.resolution_m:
            raise ValueError(
                f"resolution_m={resolution_m} does not match the in-memory config "
                f"resolution_m={cfg.resolution_m}."
            )
        if h3_resolution is not None and int(h3_resolution) != cfg.h3_resolution:
            raise ValueError(
                f"h3_resolution={h3_resolution} does not match the in-memory config "
                f"h3_resolution={cfg.h3_resolution}."
            )
        if overwrite is not None and bool(overwrite) != cfg.overwrite:
            cfg = replace(cfg, overwrite=bool(overwrite))
    else:
        cfg = load_vegetation_weights_config(
            config_path,
            resolution_m=resolution_m,
            h3_resolution=h3_resolution,
            overwrite=overwrite,
        )
    LOGGER.info("Loaded vegetation weights config for %dm", cfg.resolution_m)
    required_alignment = [cfg.inputs["dem"], cfg.inputs["chm"], cfg.inputs["landcover"]]
    if cfg.inputs.get("chm_obstruction") and cfg.inputs["chm_obstruction"].exists():
        required_alignment.append(cfg.inputs["chm_obstruction"])
    assert_weight_rasters_aligned(required_alignment)
    assert_rasters_cover_config_bbox(
        cfg,
        {
            "dem": cfg.inputs["dem"],
            "chm": cfg.inputs["chm"],
            "landcover": cfg.inputs["landcover"],
        },
        rebuild_hint="Run `make prepare-vegetation-rasters OVERWRITE=1` to rebuild CHM and landcover for the configured bbox.",
    )
    LOGGER.info("Raster alignment validated")
    existing_weight_rasters = {
        name: path
        for name, path in {
            "chm_obstruction": cfg.outputs.get("chm_obstruction_raster"),
            "landcover_source_access_weight": cfg.outputs.get("lc_source_access_weight_raster"),
            "surface_obstruction": cfg.outputs.get("surface_obstruction_raster"),
            "surface_transmission": cfg.outputs.get("surface_transmission_raster"),
        }.items()
        if path is not None and path.exists() and not cfg.overwrite
    }
    if existing_weight_rasters:
        assert_rasters_cover_config_bbox(
            cfg,
            existing_weight_rasters,
            rebuild_hint="Run `make vegetation-weight OVERWRITE=1` after refreshing the prepared input rasters if these derived vegetation rasters need to be rebuilt.",
        )
    if not skip_h3:
        assert_land_source_h3_resolution(cfg)
        LOGGER.info("Vegetation H3 resolution validated")

    if dry_run:
        print("Dry run only; no vegetation weight files will be written.")
        print("Inputs:")
        print(f"Vegetation raster resolution: {cfg.resolution_m}m")
        print(f"Vegetation H3 resolution: {cfg.h3_resolution}")
        for name, path in cfg.inputs.items():
            print(f"  {name}: {path}")
        print("Outputs:")
        for name, path in cfg.outputs.items():
            print(f"  {name}: {path}")
        print("Landcover class weights:")
        print(class_weight_table(cfg).to_string(index=False))
        return {}

    write_class_weights_csv(cfg)
    if skip_rasters:
        raster_paths = {
            "chm_obstruction": cfg.outputs.get("chm_obstruction_raster")
            or cfg.inputs["chm_obstruction"],
            "lc_obstruction_multiplier": cfg.outputs["lc_obstruction_multiplier_raster"],
            "lc_obstruction_floor": cfg.outputs["lc_obstruction_floor_raster"],
            "lc_source_access_weight": cfg.outputs["lc_source_access_weight_raster"],
            "surface_obstruction": cfg.outputs["surface_obstruction_raster"],
            "surface_transmission": cfg.outputs["surface_transmission_raster"],
        }
    else:
        raster_paths = write_weight_rasters(cfg)
    if skip_h3:
        return raster_paths
    h3_df = aggregate_to_h3(cfg, raster_paths)
    write_h3_outputs(cfg, h3_df)
    return {
        **raster_paths,
        "h3_cell_weights": cfg.outputs.get("h3_cell_weights_parquet"),
    }


# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

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
