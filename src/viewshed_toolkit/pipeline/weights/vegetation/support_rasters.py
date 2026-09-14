# =============================================================================
# Source-cell vegetation rasters and H3 source weights
# =============================================================================

import logging
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import polars as pl
import rasterio

from ...config import (
    DEFAULT_VEGETATION_WEIGHT_INPUTS,
    DEFAULT_VEGETATION_WEIGHT_OUTPUTS,
    bbox_from_config,
    land_h3_path_from_config,
    normalize_viewshed_config,
)
from ...prepare.vegetation.assets import (
    load_raw_config,
    profile_timer,
    resolve_path,
    valid_raster,
)
from .path_config import resolve_optional_path

LOGGER = logging.getLogger(__name__)
REQUIRED_CLASS_KEYS = (
    "class_name",
    "obstruction_multiplier",
    "obstruction_floor",
    "source_access_weight",
)
DEFAULT_INPUT_PATH_TEMPLATES = dict(DEFAULT_VEGETATION_WEIGHT_INPUTS)
DEFAULT_OUTPUT_PATH_TEMPLATES = dict(DEFAULT_VEGETATION_WEIGHT_OUTPUTS)


from .defaults import (
    DEFAULT_VEGETATION_BBOX_BUFFER_DEG,
    vegetation_weight_settings,
)


@dataclass(frozen=True)
class VegetationWeightsConfig:
    project_raw: dict[str, Any]
    raw: dict[str, Any]
    config_dir: Path
    resolution_m: int
    h3_resolution: int | None
    inputs: dict[str, Path]
    outputs: dict[str, Path | None]
    nodata: dict[str, float]
    landcover_classes: dict[int, dict[str, Any]]
    overwrite: bool
    all_touched: bool
    unknown_class_policy: str
    drop_geometry: bool


def _as_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc


def _format_config_value(value: Any, *, resolution_m: int, h3_resolution: int | None = None) -> Any:
    if isinstance(value, str):
        return value.format(resolution_m=resolution_m, h3_resolution=h3_resolution)
    return value


def _default_inputs(raw: dict[str, Any], config_dir: Path, resolution_m: int) -> dict[str, str]:
    defaults = {
        name: _format_config_value(value, resolution_m=resolution_m)
        for name, value in DEFAULT_INPUT_PATH_TEMPLATES.items()
    }
    defaults["land_source_cells"] = str(land_h3_path_from_config(raw, config_dir))
    return defaults


def _default_outputs(
    resolution_m: int,
    h3_resolution: int | None,
    *,
    support_dir: str | None = None,
    output_dir: str | None = None,
) -> dict[str, str | None]:
    defaults = {
        name: _format_config_value(value, resolution_m=resolution_m, h3_resolution=h3_resolution)
        for name, value in DEFAULT_OUTPUT_PATH_TEMPLATES.items()
    }
    if support_dir:
        support = str(support_dir).rstrip("/")
        defaults.update(
            {
                "class_weights_csv": f"{support}/LAND_COVER_CLASS_WEIGHTS.csv",
                "lc_obstruction_multiplier_raster": f"{support}/LAND_COVER_OBSTRUCTION_MULTIPLIER_{{resolution_m}}M.tif",
                "lc_obstruction_floor_raster": f"{support}/LAND_COVER_OBSTRUCTION_FLOOR_{{resolution_m}}M.tif",
                "lc_source_access_weight_raster": f"{support}/LAND_COVER_SOURCE_ACCESS_WEIGHT_{{resolution_m}}M.tif",
                "chm_obstruction_raster": f"{support}/CHM_OBSTRUCTION_{{resolution_m}}M.tif",
                "surface_obstruction_raster": f"{support}/SURFACE_OBSTRUCTION_{{resolution_m}}M.tif",
                "surface_transmission_raster": f"{support}/SURFACE_VISIBILITY_TRANSMISSION_{{resolution_m}}M.tif",
            }
        )
    if output_dir:
        out = str(output_dir).rstrip("/")
        defaults["h3_cell_weights_parquet"] = (
            f"{out}/LAND_SOURCE_CELL_VEGETATION_WEIGHTS_{{resolution_m}}M_R{{h3_resolution}}.parquet"
        )
    return {
        name: _format_config_value(value, resolution_m=resolution_m, h3_resolution=h3_resolution)
        for name, value in defaults.items()
    }


def load_vegetation_weights_config(
    config_path: str | Path,
    *,
    resolution_m: int | None = None,
    h3_resolution: int | None = None,
    overwrite: bool | None = None,
    check_paths: bool = True,
) -> VegetationWeightsConfig:
    raw, config_dir = load_raw_config(config_path)
    return vegetation_weights_config_from_mapping(
        raw,
        config_dir=config_dir,
        resolution_m=resolution_m,
        h3_resolution=h3_resolution,
        overwrite=overwrite,
        check_paths=check_paths,
    )


def vegetation_weights_config_from_mapping(
    raw_config: Mapping[str, Any],
    *,
    config_dir: str | Path,
    resolution_m: int | None = None,
    h3_resolution: int | None = None,
    overwrite: bool | None = None,
    check_paths: bool = True,
) -> VegetationWeightsConfig:
    """Build source-vegetation settings directly from an in-memory config."""

    raw = normalize_viewshed_config(dict(raw_config))
    config_dir = Path(config_dir).expanduser().resolve()
    cfg = raw.get("vegetation_weights")
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is False:
        raise ValueError("Missing or disabled vegetation_weights config section.")
    cfg = vegetation_weight_settings(cfg)

    selected_resolution = int(
        resolution_m or cfg.get("resolution_m", raw.get("viewshed", {}).get("dem_resolution_m", 30))
    )
    selected_h3_resolution = int(
        h3_resolution
        if h3_resolution is not None
        else cfg.get("h3_resolution", raw.get("h3", {}).get("source_resolution", 6))
    )
    input_cfg = {
        **_default_inputs(raw, config_dir, selected_resolution),
        **dict(cfg.get("inputs", {})),
    }
    output_cfg = {
        **_default_outputs(
            selected_resolution,
            selected_h3_resolution,
            support_dir=cfg.get("support_dir"),
            output_dir=cfg.get("output_dir"),
        ),
        **dict(cfg.get("outputs", {})),
    }
    inputs = {
        name: resolve_path(
            _format_config_value(value, resolution_m=selected_resolution), config_dir
        )
        for name, value in input_cfg.items()
        if value not in {None, ""}
    }
    outputs = {
        name: resolve_optional_path(
            _format_config_value(
                value,
                resolution_m=selected_resolution,
                h3_resolution=selected_h3_resolution,
            ),
            config_dir,
        )
        for name, value in output_cfg.items()
    }
    nodata = {
        name: _as_float(value, f"nodata.{name}")
        for name, value in dict(cfg.get("nodata", {})).items()
    }
    classes = {
        int(code): dict(values or {})
        for code, values in dict(cfg.get("landcover_classes", {})).items()
    }

    parsed = VegetationWeightsConfig(
        project_raw=raw,
        raw=cfg,
        config_dir=config_dir,
        resolution_m=selected_resolution,
        h3_resolution=selected_h3_resolution,
        inputs=inputs,
        outputs=outputs,
        nodata=nodata,
        landcover_classes=classes,
        overwrite=bool(cfg.get("overwrite", False) if overwrite is None else overwrite),
        all_touched=bool(cfg.get("all_touched", True)),
        unknown_class_policy=str(cfg.get("unknown_class_policy", "error")),
        drop_geometry=bool(cfg.get("h3_aggregation", {}).get("drop_geometry", False)),
    )
    validate_vegetation_weights_config(parsed, check_paths=check_paths)
    return parsed


def validate_vegetation_weights_config(
    cfg: VegetationWeightsConfig, *, check_paths: bool = True
) -> None:
    required_inputs = ("dem", "chm", "landcover", "land_source_cells")
    missing_inputs = [name for name in required_inputs if name not in cfg.inputs]
    if missing_inputs:
        raise ValueError(f"vegetation_weights.inputs missing required keys: {missing_inputs}")
    if check_paths:
        required_path_inputs = set(required_inputs)
        chm_source = str(cfg.raw.get("chm_obstruction", {}).get("source", "derive_if_missing"))
        if chm_source == "use_existing":
            required_path_inputs.add("chm_obstruction")
        missing_paths = [
            str(cfg.inputs[name])
            for name in sorted(required_path_inputs)
            if name in cfg.inputs and not cfg.inputs[name].exists()
        ]
        if missing_paths:
            raise FileNotFoundError(
                f"Required vegetation weight input paths do not exist: {missing_paths}"
            )

    if not cfg.landcover_classes:
        raise ValueError("vegetation_weights.landcover_classes must not be empty.")
    for code, values in cfg.landcover_classes.items():
        missing = [key for key in REQUIRED_CLASS_KEYS if key not in values]
        if missing:
            raise ValueError(f"Landcover class {code} missing required keys: {missing}")
        multiplier = _as_float(
            values["obstruction_multiplier"],
            f"landcover_classes.{code}.obstruction_multiplier",
        )
        floor = _as_float(
            values["obstruction_floor"], f"landcover_classes.{code}.obstruction_floor"
        )
        access = _as_float(
            values["source_access_weight"],
            f"landcover_classes.{code}.source_access_weight",
        )
        if multiplier < 0:
            raise ValueError(f"Landcover class {code} obstruction_multiplier must be >= 0.")
        if not 0 <= floor <= 1:
            raise ValueError(f"Landcover class {code} obstruction_floor must be between 0 and 1.")
        if not 0 <= access <= 1:
            raise ValueError(
                f"Landcover class {code} source_access_weight must be between 0 and 1."
            )

    output_nodata = cfg.nodata.get("output_float", -9999.0)
    if 0 <= output_nodata <= 1:
        raise ValueError(
            "vegetation_weights.nodata.output_float must not be a valid 0..1 weight value."
        )
    if cfg.unknown_class_policy not in {"error", "nodata", "default"}:
        raise ValueError(
            "vegetation_weights.unknown_class_policy must be one of: error, nodata, default."
        )


def assert_land_source_h3_resolution(cfg: VegetationWeightsConfig) -> None:
    if cfg.h3_resolution is None:
        return
    source_scan = pl.scan_parquet(str(cfg.inputs["land_source_cells"]))
    if "h3_resolution" not in source_scan.collect_schema().names():
        return
    observed = (
        source_scan.select(
            pl.col("h3_resolution").cast(pl.Int64, strict=False).drop_nulls().unique()
        )
        .collect(engine="streaming")
        .get_column("h3_resolution")
        .sort()
        .to_list()
    )
    if observed and observed != [int(cfg.h3_resolution)]:
        raise ValueError(
            "vegetation_weights.h3_resolution does not match the configured land/source-cell parquet: "
            f"expected {cfg.h3_resolution}, found {observed}."
        )


def assert_weight_rasters_aligned(paths: Sequence[Path]) -> None:
    if len(paths) < 2:
        return
    with rasterio.open(paths[0]) as ref:
        ref_shape = ref.shape
        ref_crs = ref.crs
        ref_transform = ref.transform
        ref_bounds = ref.bounds
    for path in paths[1:]:
        with rasterio.open(path) as src:
            if src.shape != ref_shape or src.crs != ref_crs or src.transform != ref_transform:
                raise ValueError(f"Raster alignment mismatch: {path} does not match {paths[0]}.")
            if not np.allclose(tuple(src.bounds), tuple(ref_bounds), rtol=0, atol=1e-8):
                raise ValueError(f"Raster bounds mismatch: {path} does not match {paths[0]}.")


def _configured_bbox_wgs84(
    cfg: VegetationWeightsConfig,
) -> tuple[float, float, float, float]:
    vegetation_data = cfg.project_raw.get("vegetation_data", {})
    buffer_deg = float(vegetation_data.get("bbox_buffer_deg", DEFAULT_VEGETATION_BBOX_BUFFER_DEG))
    return bbox_from_config(cfg.project_raw, buffer_deg)


def _raster_covers_bbox(
    path: Path, bbox_wgs84: tuple[float, float, float, float]
) -> tuple[bool, dict[str, Any]]:
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as src:
        if src.crs is None:
            return False, {
                "path": str(path),
                "reason": "missing CRS",
                "bounds": tuple(src.bounds),
            }
        raster_bounds = tuple(float(v) for v in src.bounds)
        if str(src.crs).upper() in {"EPSG:4326", "OGC:CRS84"}:
            bbox_in_raster_crs = bbox_wgs84
        else:
            bbox_in_raster_crs = transform_bounds("EPSG:4326", src.crs, *bbox_wgs84, densify_pts=21)
        tolerance = max(abs(float(src.res[0])), abs(float(src.res[1])), 1e-8)
        left, bottom, right, top = raster_bounds
        min_x, min_y, max_x, max_y = tuple(float(v) for v in bbox_in_raster_crs)
        covers = (
            left <= min_x + tolerance
            and bottom <= min_y + tolerance
            and right >= max_x - tolerance
            and top >= max_y - tolerance
        )
        return covers, {
            "path": str(path),
            "crs": str(src.crs),
            "raster_bounds": raster_bounds,
            "bbox_in_raster_crs": (min_x, min_y, max_x, max_y),
        }


def assert_rasters_cover_config_bbox(
    cfg: VegetationWeightsConfig,
    paths: dict[str, Path],
    *,
    rebuild_hint: str,
) -> bool:
    bbox = _configured_bbox_wgs84(cfg)
    stale: list[tuple[str, dict[str, Any]]] = []
    for name, path in paths.items():
        covers, detail = _raster_covers_bbox(path, bbox)
        if not covers:
            stale.append((name, detail))
    if not stale:
        return True

    details = "; ".join(
        f"{name}={item['path']} bounds={item.get('raster_bounds')} expected_bbox={item.get('bbox_in_raster_crs')}"
        for name, item in stale
    )
    message = (
        "Configured bbox is not covered by the vegetation raster extent. "
        f"{details}. {rebuild_hint}"
    )
    LOGGER.warning(message)
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    return False


def _profile_like(
    reference_path: Path, *, dtype: str = "float32", nodata: float = -9999.0
) -> dict[str, Any]:
    with rasterio.open(reference_path) as src:
        profile = src.profile.copy()
    tiled = bool(profile.get("width", 0) >= 16 and profile.get("height", 0) >= 16)
    profile.update(dtype=dtype, nodata=nodata, compress="deflate", tiled=tiled, BIGTIFF="IF_SAFER")
    if tiled:
        profile.update(blockxsize=512, blockysize=512)
    if not tiled:
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    return profile


def class_weight_table(cfg: VegetationWeightsConfig) -> pd.DataFrame:
    rows = []
    for code, values in sorted(cfg.landcover_classes.items()):
        rows.append(
            {
                "landcover_code": code,
                "class_name": values["class_name"],
                "group": values.get("group", ""),
                "obstruction_multiplier": float(values["obstruction_multiplier"]),
                "obstruction_floor": float(values["obstruction_floor"]),
                "source_access_weight": float(values["source_access_weight"]),
                "include_as_source": bool(values.get("include_as_source", True)),
                "notes": values.get("notes", ""),
            }
        )
    return pd.DataFrame(rows)


def write_class_weights_csv(cfg: VegetationWeightsConfig) -> Path:
    out = cfg.outputs.get("class_weights_csv")
    if out is None:
        raise ValueError("outputs.class_weights_csv is required.")
    table = class_weight_table(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    LOGGER.info("Wrote class weights CSV: %s", out)
    return out


def derive_chm_obstruction_array(chm: np.ndarray, cfg: VegetationWeightsConfig) -> np.ndarray:
    chm_cfg = cfg.raw.get("chm_obstruction", {})
    thresholds = chm_cfg.get("thresholds_m", {})
    values = chm_cfg.get("values", {})
    no_max = _as_float(
        thresholds.get("no_obstruction_max"),
        "chm_obstruction.thresholds_m.no_obstruction_max",
    )
    low_max = _as_float(
        thresholds.get("low_obstruction_max"),
        "chm_obstruction.thresholds_m.low_obstruction_max",
    )
    med_max = _as_float(
        thresholds.get("medium_obstruction_max"),
        "chm_obstruction.thresholds_m.medium_obstruction_max",
    )
    no_v = _as_float(values.get("no_obstruction"), "chm_obstruction.values.no_obstruction")
    low_v = _as_float(values.get("low_obstruction"), "chm_obstruction.values.low_obstruction")
    med_v = _as_float(values.get("medium_obstruction"), "chm_obstruction.values.medium_obstruction")
    high_v = _as_float(values.get("high_obstruction"), "chm_obstruction.values.high_obstruction")
    chm_nodata = cfg.nodata.get("chm", 255.0)
    out_nodata = cfg.nodata.get("output_float", -9999.0)

    out = np.full(chm.shape, out_nodata, dtype="float32")
    valid = chm != chm_nodata
    out[valid & (chm < no_max)] = no_v
    out[valid & (chm >= no_max) & (chm < low_max)] = low_v
    out[valid & (chm >= low_max) & (chm < med_max)] = med_v
    out[valid & (chm >= med_max)] = high_v
    return out


def ensure_chm_obstruction(cfg: VegetationWeightsConfig) -> Path:
    out = cfg.outputs.get("chm_obstruction_raster") or cfg.inputs.get("chm_obstruction")
    if out is None:
        raise ValueError("CHM obstruction output/input path is required.")
    source = str(cfg.raw.get("chm_obstruction", {}).get("source", "derive_if_missing"))
    if source in {"use_existing", "derive_if_missing"} and out.exists() and not cfg.overwrite:
        LOGGER.info("Using existing CHM obstruction raster: %s", out)
        return out
    profile = _profile_like(
        cfg.inputs["chm"],
        dtype="float32",
        nodata=cfg.nodata.get("output_float", -9999.0),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with profile_timer("derive_chm_obstruction_windowed", source=cfg.inputs["chm"], output=out):
        with rasterio.open(cfg.inputs["chm"]) as src, rasterio.open(out, "w", **profile) as dst:
            for _, window in src.block_windows(1):
                chm = src.read(1, window=window)
                arr = derive_chm_obstruction_array(chm, cfg)
                dst.write(arr.astype("float32", copy=False), 1, window=window)
    LOGGER.info("Wrote raster: %s", out)
    return out


def map_landcover_weights(
    landcover: np.ndarray,
    cfg: VegetationWeightsConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    out_nodata = cfg.nodata.get("output_float", -9999.0)
    lc_nodata = cfg.nodata.get("landcover", 0.0)
    multiplier = np.full(landcover.shape, out_nodata, dtype="float32")
    floor = np.full(landcover.shape, out_nodata, dtype="float32")
    access = np.full(landcover.shape, out_nodata, dtype="float32")

    valid_known = landcover != lc_nodata
    for code, values in cfg.landcover_classes.items():
        mask_arr = landcover == code
        multiplier[mask_arr] = float(values["obstruction_multiplier"])
        floor[mask_arr] = float(values["obstruction_floor"])
        access[mask_arr] = float(values["source_access_weight"])
        valid_known &= ~mask_arr

    unknown = valid_known
    if np.any(unknown):
        if cfg.unknown_class_policy == "error":
            codes = sorted(int(v) for v in np.unique(landcover[unknown]))
            raise ValueError(f"Unknown landcover codes encountered: {codes}")
        if cfg.unknown_class_policy == "default":
            default_code = int(cfg.raw.get("default_landcover_class"))
            if default_code not in cfg.landcover_classes:
                raise ValueError(f"default_landcover_class {default_code} is not configured.")
            values = cfg.landcover_classes[default_code]
            multiplier[unknown] = float(values["obstruction_multiplier"])
            floor[unknown] = float(values["obstruction_floor"])
            access[unknown] = float(values["source_access_weight"])
    return multiplier, floor, access


def surface_obstruction_array(
    chm_obstruction: np.ndarray,
    multiplier: np.ndarray,
    floor: np.ndarray,
    cfg: VegetationWeightsConfig,
) -> np.ndarray:
    out_nodata = cfg.nodata.get("output_float", -9999.0)
    surf_cfg = cfg.raw.get("surface_obstruction", {})
    clamp_min = _as_float(surf_cfg.get("clamp_min", 0.0), "surface_obstruction.clamp_min")
    clamp_max = _as_float(surf_cfg.get("clamp_max", 1.0), "surface_obstruction.clamp_max")
    chm_policy = str(surf_cfg.get("chm_nodata_policy", "treat_as_zero"))

    valid_lc = (multiplier != out_nodata) & (floor != out_nodata)
    chm_valid = chm_obstruction != out_nodata
    chm_term = np.where(
        chm_valid, chm_obstruction, 0.0 if chm_policy == "treat_as_zero" else out_nodata
    )
    valid = valid_lc if chm_policy == "treat_as_zero" else valid_lc & chm_valid
    surface = np.full(chm_obstruction.shape, out_nodata, dtype="float32")
    computed = np.maximum(chm_term * multiplier, floor)
    surface[valid] = np.clip(computed[valid], clamp_min, clamp_max)
    return surface


def surface_transmission_array(
    surface_obstruction: np.ndarray, cfg: VegetationWeightsConfig
) -> np.ndarray:
    out_nodata = cfg.nodata.get("output_float", -9999.0)
    valid = surface_obstruction != out_nodata
    out = np.full(surface_obstruction.shape, out_nodata, dtype="float32")
    out[valid] = np.clip(1.0 - surface_obstruction[valid], 0.0, 1.0)
    return out


def write_weight_rasters(
    cfg: VegetationWeightsConfig, *, skip_existing: bool = False
) -> dict[str, Path]:
    chm_obs_path = ensure_chm_obstruction(cfg)
    assert_weight_rasters_aligned(
        [cfg.inputs["dem"], cfg.inputs["chm"], chm_obs_path, cfg.inputs["landcover"]]
    )
    profile = _profile_like(
        cfg.inputs["landcover"],
        dtype="float32",
        nodata=cfg.nodata.get("output_float", -9999.0),
    )
    paths = {
        "chm_obstruction": chm_obs_path,
        "lc_obstruction_multiplier": cfg.outputs["lc_obstruction_multiplier_raster"],
        "lc_obstruction_floor": cfg.outputs["lc_obstruction_floor_raster"],
        "lc_source_access_weight": cfg.outputs["lc_source_access_weight_raster"],
        "surface_obstruction": cfg.outputs["surface_obstruction_raster"],
        "surface_transmission": cfg.outputs["surface_transmission_raster"],
    }
    for key, path in paths.items():
        if path is None:
            raise ValueError(f"outputs.{key}_raster is required.")

    derived_paths = [
        paths["lc_obstruction_multiplier"],
        paths["lc_obstruction_floor"],
        paths["lc_source_access_weight"],
        paths["surface_obstruction"],
        paths["surface_transmission"],
    ]
    if not cfg.overwrite and all(path.exists() and valid_raster(path) for path in derived_paths):
        LOGGER.info("Reusing existing derived vegetation rasters.")
        return {key: path for key, path in paths.items() if path is not None}

    for path in derived_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()

    with profile_timer("derive_vegetation_rasters_windowed", resolution_m=cfg.resolution_m):
        with (
            rasterio.open(cfg.inputs["landcover"]) as lc_src,
            rasterio.open(chm_obs_path) as chm_src,
            rasterio.open(paths["lc_obstruction_multiplier"], "w", **profile) as mult_dst,
            rasterio.open(paths["lc_obstruction_floor"], "w", **profile) as floor_dst,
            rasterio.open(paths["lc_source_access_weight"], "w", **profile) as access_dst,
            rasterio.open(paths["surface_obstruction"], "w", **profile) as surface_dst,
            rasterio.open(paths["surface_transmission"], "w", **profile) as transmission_dst,
        ):

            for _, window in lc_src.block_windows(1):
                landcover = lc_src.read(1, window=window)
                multiplier, floor, access = map_landcover_weights(landcover, cfg)
                chm_obs = chm_src.read(1, window=window)
                surface = surface_obstruction_array(chm_obs, multiplier, floor, cfg)
                transmission = surface_transmission_array(surface, cfg)
                mult_dst.write(multiplier.astype("float32", copy=False), 1, window=window)
                floor_dst.write(floor.astype("float32", copy=False), 1, window=window)
                access_dst.write(access.astype("float32", copy=False), 1, window=window)
                surface_dst.write(surface.astype("float32", copy=False), 1, window=window)
                transmission_dst.write(transmission.astype("float32", copy=False), 1, window=window)
    for path in derived_paths:
        LOGGER.info("Wrote raster: %s", path)
    return {key: path for key, path in paths.items() if path is not None}
