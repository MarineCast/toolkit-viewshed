# =============================================================================
# Pair-level vegetation path attenuation
# =============================================================================

import logging
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import rasterio
from rasterio.windows import from_bounds

from ...config import (
    DEFAULT_VEGETATION_PATH_OUTPUTS,
    get_run_version,
    load_yaml,
    normalize_viewshed_config,
    resolve_path,
    stable_config_hash,
    vegetation_path_input_defaults,
    versioned_vegetation_path_name,
    viewshed_domain_relative,
)
from ...contracts.artifacts import final_artifact_paths_from_raw

LOGGER = logging.getLogger(__name__)

DEFAULT_INPUTS = {}
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


from .defaults import vegetation_path_settings, vegetation_weight_settings


@dataclass(frozen=True)
class VegetationPathConfig:
    raw: dict[str, Any]
    config_dir: Path
    resolution_m: int
    inputs: dict[str, Path | None]
    outputs: dict[str, Path | None]
    columns: dict[str, str]
    nodata: dict[str, float]
    raw_config: dict[str, Any] = field(default_factory=dict)
    run_version: str = "unversioned"
    config_hash: str = ""


@dataclass(frozen=True)
class VegetationRasterData:
    transmission: np.ndarray
    obstruction: np.ndarray
    chm_obstruction: np.ndarray
    landcover: np.ndarray
    transform: Any
    raster_shape: tuple[int, int]
    raster_crs: Any


def _fmt(value: Any, *, resolution_m: int, version: str) -> Any:
    if isinstance(value, str):
        return value.format(resolution_m=resolution_m, version=version)
    return value


def resolve_optional_path(value: Any, config_dir: Path) -> Path | None:
    if value in {None, ""}:
        return None
    return resolve_path(value, config_dir)


def _area_lookup_path(raw: dict[str, Any], config_dir: Path) -> Path:
    final_lookup = final_artifact_paths_from_raw(raw, config_dir).source_target_lookup
    return final_lookup


def _normalize_pair_chunks_output(
    value: Any,
    *,
    config_dir: Path,
    resolution_m: int,
    version: str,
    legacy_pair_weights: bool,
) -> Path | None:
    path = resolve_optional_path(
        _fmt(value, resolution_m=resolution_m, version=version), config_dir
    )
    if path is None:
        return None
    if legacy_pair_weights and path.suffix:
        warnings.warn(
            "vegetation_path_weights.outputs.pair_weights pointed to a file path; "
            "using its parent/chunks directory for pair chunks.",
            DeprecationWarning,
            stacklevel=3,
        )
        return path.parent / "chunks"
    return path


def load_vegetation_path_config(config_path: str | Path) -> VegetationPathConfig:
    raw, config_dir = load_yaml(config_path)
    return vegetation_path_config_from_mapping(raw, config_dir=config_dir)


def vegetation_path_config_from_mapping(
    raw_config: Mapping[str, Any],
    *,
    config_dir: str | Path,
) -> VegetationPathConfig:
    """Build pair-vegetation settings directly from an in-memory config."""

    raw = normalize_viewshed_config(dict(raw_config))
    config_dir = Path(config_dir).expanduser().resolve()
    cfg = vegetation_path_settings(raw.get("vegetation_path_weights", {}))
    if "landcover_classes" not in cfg:
        cfg["landcover_classes"] = vegetation_weight_settings(raw.get("vegetation_weights", {}))[
            "landcover_classes"
        ]
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is False:
        raise ValueError("Missing or disabled vegetation_path_weights config section.")
    resolution_m = int(
        cfg.get(
            "resolution_m",
            raw.get("vegetation_weights", {}).get(
                "resolution_m", raw.get("viewshed", {}).get("dem_resolution_m", 30)
            ),
        )
    )
    version = str(cfg.get("version") or versioned_vegetation_path_name(raw))
    area_lookup = _area_lookup_path(raw, config_dir)
    h3 = raw.get("h3", {}) or {}
    source_resolution = int(
        h3.get("source_resolution", h3.get("resolution", raw.get("h3_resolution", 6)))
    )
    support_dir = str(cfg.get("support_dir", "")).rstrip("/")
    str(cfg.get("output_dir", "")).rstrip("/")
    input_defaults = {
        **vegetation_path_input_defaults(
            area_lookup=area_lookup,
            source_resolution=source_resolution,
            resolution_m=resolution_m,
        ),
    }
    if support_dir:
        input_defaults.update(
            {
                "surface_transmission": f"{support_dir}/SURFACE_VISIBILITY_TRANSMISSION_{{resolution_m}}M.tif",
                "surface_obstruction": f"{support_dir}/SURFACE_OBSTRUCTION_{{resolution_m}}M.tif",
                "chm_obstruction": f"{support_dir}/CHM_OBSTRUCTION_{{resolution_m}}M.tif",
            }
        )
    input_cfg = {**input_defaults, **dict(cfg.get("inputs", {}))}
    raw_outputs = dict(cfg.get("outputs", {}))
    legacy_pair_weights = "pair_weights" in raw_outputs and "pair_chunks" not in raw_outputs
    if "pair_weights" in raw_outputs and "pair_chunks" not in raw_outputs:
        warnings.warn(
            "vegetation_path_weights.outputs.pair_weights is deprecated; use pair_chunks instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        raw_outputs["pair_chunks"] = raw_outputs["pair_weights"]
    if "target_aggregation" in raw_outputs and "target_summary" not in raw_outputs:
        raw_outputs["target_summary"] = raw_outputs["target_aggregation"]
    if "source_aggregation" in raw_outputs and "source_summary" not in raw_outputs:
        raw_outputs["source_summary"] = raw_outputs["source_aggregation"]
    output_dir = str(cfg.get("output_dir", "")).rstrip("/")
    output_defaults = {
        **DEFAULT_OUTPUTS,
        "candidate_pairs": viewshed_domain_relative(
            "land",
            "vegetation_path_weights",
            version,
            "VEGETATION_CANDIDATE_PAIRS.parquet",
        ),
        "pair_chunks": viewshed_domain_relative(
            "land", "vegetation_path_weights", version, "pair_chunks"
        ),
        "target_summary": viewshed_domain_relative(
            "land",
            "vegetation_path_weights",
            version,
            "target_vegetation_summary.parquet",
        ),
        "source_summary": viewshed_domain_relative(
            "land",
            "vegetation_path_weights",
            version,
            "source_vegetation_summary.parquet",
        ),
        "manifest": viewshed_domain_relative(
            "land",
            "vegetation_path_weights",
            version,
            f"vegetation_path_weight_manifest_{version}.csv",
        ),
    }
    if output_dir:
        output_defaults.update(
            {
                "candidate_pairs": f"{output_dir}/VEGETATION_CANDIDATE_PAIRS.parquet",
                "pair_chunks": f"{output_dir}/pair_chunks",
                "target_summary": f"{output_dir}/target_vegetation_summary.parquet",
                "source_summary": f"{output_dir}/source_vegetation_summary.parquet",
                "manifest": f"{output_dir}/vegetation_path_weight_manifest_{{version}}.csv",
            }
        )
    output_cfg = {**output_defaults, **raw_outputs}
    if "pair_weights" in dict(cfg.get("outputs", {})) and "pair_chunks" in dict(
        cfg.get("outputs", {})
    ):
        warnings.warn(
            "vegetation_path_weights.outputs.pair_weights is deprecated; use pair_chunks instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    inputs = {
        name: resolve_optional_path(
            _fmt(value, resolution_m=resolution_m, version=version), config_dir
        )
        for name, value in input_cfg.items()
    }
    parsed = VegetationPathConfig(
        raw_config=raw,
        raw=cfg,
        config_dir=config_dir,
        run_version=get_run_version(raw),
        config_hash=stable_config_hash(raw),
        resolution_m=resolution_m,
        inputs=inputs,
        outputs={
            name: (
                _normalize_pair_chunks_output(
                    value,
                    config_dir=config_dir,
                    resolution_m=resolution_m,
                    version=version,
                    legacy_pair_weights=legacy_pair_weights,
                )
                if name == "pair_chunks"
                else resolve_optional_path(
                    _fmt(value, resolution_m=resolution_m, version=version), config_dir
                )
            )
            for name, value in output_cfg.items()
        },
        columns={**DEFAULT_COLUMNS, **dict(cfg.get("columns", {}))},
        nodata={
            "surface_transmission": -9999.0,
            "surface_obstruction": -9999.0,
            "chm_obstruction": -9999.0,
            "landcover": 0.0,
            **{k: float(v) for k, v in dict(cfg.get("nodata", {})).items()},
        },
    )
    validate_vegetation_path_config(parsed)
    return parsed


def validate_vegetation_path_config(cfg: VegetationPathConfig) -> None:
    required_inputs = [
        "clear_sky_pairs",
        "source_cells",
        "surface_transmission",
        "surface_obstruction",
        "chm_obstruction",
        "landcover",
    ]
    missing = [
        name
        for name in required_inputs
        if cfg.inputs.get(name) is None or not cfg.inputs[name].exists()
    ]
    if missing:
        raise FileNotFoundError(f"Missing vegetation path input(s): {missing}")
    pair_mode = cfg.raw.get("sampling", {}).get("pair_mode", "paired")
    if pair_mode not in {"paired", "cross_product"}:
        raise ValueError(
            "vegetation_path_weights.sampling.pair_mode must be paired or cross_product."
        )
    method = cfg.raw.get("path_transmission", {}).get(
        "method", "mean_transmission_over_land_pixels"
    )
    if method not in {
        "mean_transmission_over_land_pixels",
        "p10_transmission_over_land_pixels",
        "min_transmission_over_land_pixels",
        "exp_obstruction_length_decay",
    }:
        raise ValueError(f"Unsupported vegetation path transmission method: {method}")
    selected_path_weight = cfg.raw.get("path_transmission", {}).get(
        "selected_path_weight",
        cfg.raw.get("selected_path_weight", "mean"),
    )
    if selected_path_weight not in {"mean", "length_decay"}:
        raise ValueError(
            "vegetation_path_weights.path_transmission.selected_path_weight must be "
            "one of: mean, length_decay"
        )
    assert_path_rasters_aligned(
        [
            cfg.inputs["surface_transmission"],
            cfg.inputs["surface_obstruction"],
            cfg.inputs["chm_obstruction"],
            cfg.inputs["landcover"],
        ]
    )
    access_mode = cfg.raw.get("raster_access_mode", "windowed")
    if access_mode not in {"full_read", "windowed"}:
        raise ValueError(
            "vegetation_path_weights.raster_access_mode must be full_read or windowed."
        )
    out = cfg.outputs.get("pair_chunks")
    if out is None:
        raise ValueError("vegetation_path_weights.outputs.pair_chunks is required.")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("candidate_pairs", "target_summary", "source_summary", "manifest"):
        path = cfg.outputs.get(name)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)


def assert_path_rasters_aligned(paths: Sequence[Path | None]) -> None:
    clean = [p for p in paths if p is not None]
    if len(clean) < 2:
        return
    with rasterio.open(clean[0]) as ref:
        shape, crs, transform, bounds = ref.shape, ref.crs, ref.transform, ref.bounds
    for path in clean[1:]:
        with rasterio.open(path) as src:
            if src.shape != shape or src.crs != crs or src.transform != transform:
                raise ValueError(f"Raster alignment mismatch: {path} does not match {clean[0]}.")
            if not np.allclose(tuple(src.bounds), tuple(bounds), rtol=0, atol=1e-8):
                raise ValueError(f"Raster bounds mismatch: {path} does not match {clean[0]}.")


def _require_raster_crs(path: Path):
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"Vegetation raster has no CRS; cannot safely sample paths: {path}")
        return src.crs


def load_vegetation_rasters_full(cfg: VegetationPathConfig) -> VegetationRasterData:
    with (
        rasterio.open(cfg.inputs["surface_transmission"]) as trans_src,
        rasterio.open(cfg.inputs["surface_obstruction"]) as obs_src,
        rasterio.open(cfg.inputs["chm_obstruction"]) as chm_src,
        rasterio.open(cfg.inputs["landcover"]) as lc_src,
    ):
        if trans_src.crs is None:
            raise ValueError("Vegetation raster has no CRS; cannot safely sample paths.")
        return VegetationRasterData(
            transmission=trans_src.read(1),
            obstruction=obs_src.read(1),
            chm_obstruction=chm_src.read(1),
            landcover=lc_src.read(1),
            transform=trans_src.transform,
            raster_shape=trans_src.shape,
            raster_crs=trans_src.crs,
        )


def load_vegetation_rasters_window(
    cfg: VegetationPathConfig,
    bounds: tuple[float, float, float, float],
) -> VegetationRasterData:
    with (
        rasterio.open(cfg.inputs["surface_transmission"]) as trans_src,
        rasterio.open(cfg.inputs["surface_obstruction"]) as obs_src,
        rasterio.open(cfg.inputs["chm_obstruction"]) as chm_src,
        rasterio.open(cfg.inputs["landcover"]) as lc_src,
    ):
        if trans_src.crs is None:
            raise ValueError("Vegetation raster has no CRS; cannot safely sample paths.")
        window = from_bounds(*bounds, transform=trans_src.transform)
        window = window.round_offsets().round_lengths()
        full_window = rasterio.windows.Window(
            col_off=0,
            row_off=0,
            width=trans_src.width,
            height=trans_src.height,
        )
        window = window.intersection(full_window)
        if int(window.width) <= 0 or int(window.height) <= 0:
            raise ValueError(f"Vegetation raster window is empty for bounds: {bounds}")
        return VegetationRasterData(
            transmission=trans_src.read(1, window=window),
            obstruction=obs_src.read(1, window=window),
            chm_obstruction=chm_src.read(1, window=window),
            landcover=lc_src.read(1, window=window),
            transform=trans_src.window_transform(window),
            raster_shape=(int(window.height), int(window.width)),
            raster_crs=trans_src.crs,
        )
