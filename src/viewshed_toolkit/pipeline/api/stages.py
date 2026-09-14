"""Typed adapters for individual viewshed stages."""

from __future__ import annotations

import shutil
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from ..config import (
    AppConfig,
    apply_source_type_policy,
    initialize_app_config,
    load_app_config,
)
from .registry import StageInvocation


def _runtime_app(
    config: str | Path | AppConfig,
    *,
    source_type: str,
    overwrite: bool,
    max_workers: int | None = None,
    batch_size: int | None = None,
) -> AppConfig:
    app = config if isinstance(config, AppConfig) else load_app_config(config)
    app = apply_source_type_policy(app, source_type)
    if overwrite:
        app = replace(app, run=replace(app.run, overwrite=True))
    if max_workers is not None:
        app = replace(app, batch=replace(app.batch, max_workers=int(max_workers)))
    if batch_size is not None:
        app = replace(app, batch=replace(app.batch, batch_size_cells=int(batch_size)))
    return initialize_app_config(app)


def download_dem(
    config: str | Path,
    *,
    overwrite: bool = False,
    workers: int | None = None,
    chunk_grid: tuple[int, int] | None = None,
    resolutions: Sequence[int] | None = None,
    keep_intermediates: bool | None = None,
) -> object:
    from ..prepare.elevation import download_dem_for_config

    return download_dem_for_config(
        config,
        overwrite=overwrite,
        workers=workers,
        chunk_grid=chunk_grid,
        resolutions=resolutions,
        keep_intermediates=keep_intermediates,
    )


def download_data(
    config: str | Path,
    *,
    overwrite: bool = False,
    workers: int | None = None,
    chunk_grid: tuple[int, int] | None = None,
    resolutions: Sequence[int] | None = None,
    keep_intermediates: bool | None = None,
) -> tuple[object, object]:
    from ..prepare.vegetation import download_canopy_height_for_config

    dem = download_dem(
        config,
        overwrite=overwrite,
        workers=workers,
        chunk_grid=chunk_grid,
        resolutions=resolutions,
        keep_intermediates=keep_intermediates,
    )
    canopy = download_canopy_height_for_config(config, overwrite=overwrite, dry_run=False)
    return dem, canopy


def download_canopy_height(
    config: str | Path, *, overwrite: bool = False, dry_run: bool = False
) -> object:
    from ..prepare.vegetation import download_canopy_height_for_config

    return download_canopy_height_for_config(config, overwrite=overwrite, dry_run=dry_run)


def download_landcover(
    config: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    method: str | None = None,
) -> object:
    from ..prepare.vegetation import download_landcover_for_config

    return download_landcover_for_config(
        config, overwrite=overwrite, dry_run=dry_run, method=method
    )


def build_vegetation_weights(
    config: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    resolution_m: int | None = None,
    h3_resolution: int | None = None,
    skip_rasters: bool = False,
    skip_h3: bool = False,
) -> Mapping[str, Path | None]:
    from ..weights.vegetation import run_vegetation_weights

    return run_vegetation_weights(
        config,
        overwrite=overwrite,
        dry_run=dry_run,
        resolution_m=resolution_m,
        h3_resolution=h3_resolution,
        skip_rasters=skip_rasters,
        skip_h3=skip_h3,
    )


def cleanup_static_input_caches(
    output_paths: Sequence[Path], cache_paths: Sequence[Path]
) -> tuple[Path, ...]:
    """Remove caches only after every durable output exists."""

    missing = [path for path in output_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Refusing to remove static input caches because outputs are missing: "
            + ", ".join(str(path) for path in missing)
        )
    removed: list[Path] = []
    for path in cache_paths:
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    return tuple(removed)


def build_land_cells(
    config: str | Path, *, overwrite: bool = False, h3_resolution: int | None = None
) -> object:
    from ..prepare.area.land import build_land_cells_for_config

    return build_land_cells_for_config(config, overwrite=overwrite, h3_resolution=h3_resolution)


def prepare_source_target_lookup(
    config: str | Path,
    *,
    overwrite: bool = False,
    combine: bool = True,
    limit_per_source_type: int | None = None,
) -> object:
    from ..prepare.area import build_source_target_lookup

    return build_source_target_lookup(
        config,
        overwrite=overwrite,
        combine=combine,
        limit_per_source_type=limit_per_source_type,
    )


def build_distance_weights(
    config: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
    limit: int | None = None,
    start: int = 0,
    combine: bool = False,
    clean_intermediates: bool = False,
) -> dict[str, object]:
    from ..config.distance import (
        DistanceWeightConfig,
        load_distance_runtime,
        load_distance_weight_config,
    )
    from ..weights.distance.compute import (
        build_distance_weight_partitions,
        clean_distance_weight_outputs,
        combine_distance_weight_partitions,
    )

    runtime = load_distance_runtime(config)
    weight_config = load_distance_weight_config(runtime.raw_config)
    if overwrite:
        weight_config = DistanceWeightConfig(**{**weight_config.__dict__, "overwrite": True})
    manifest = build_distance_weight_partitions(
        runtime,
        weight_config,
        limit=limit,
        start=start,
        progress_every=0,
        source_type=source_type,
    )
    result: dict[str, object] = {"partition_manifest": manifest}
    if combine:
        partition_paths = [
            Path(path) for path in manifest["distance_weight_partition"].dropna().tolist()
        ]
        result["combined"] = combine_distance_weight_partitions(
            runtime,
            weight_config,
            partition_paths=partition_paths,
            source_type=source_type,
        )
        if clean_intermediates:
            clean_distance_weight_outputs(runtime, weight_config, source_type)
    return result


def combine_distance_weights(
    config: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
    clean_intermediates: bool = False,
) -> Mapping[str, object]:
    from ..config.distance import (
        DistanceWeightConfig,
        load_distance_runtime,
        load_distance_weight_config,
    )
    from ..weights.distance.compute import (
        clean_distance_weight_outputs,
        combine_distance_weight_partitions,
    )

    runtime = load_distance_runtime(config)
    weight_config = load_distance_weight_config(runtime.raw_config)
    if overwrite:
        weight_config = DistanceWeightConfig(**{**weight_config.__dict__, "overwrite": True})
    result = combine_distance_weight_partitions(runtime, weight_config, source_type=source_type)
    if clean_intermediates:
        clean_distance_weight_outputs(runtime, weight_config, source_type)
    return result


def aggregate_distance_weights(
    config: str | Path,
    *,
    source_type: str,
    by: str,
) -> Mapping[str, object]:
    from ..config.distance import load_distance_runtime, load_distance_weight_config
    from ..weights.distance.compute import (
        aggregate_distance_weight_partitions_to_source,
        aggregate_distance_weight_partitions_to_target,
    )

    runtime = load_distance_runtime(config)
    weight_config = load_distance_weight_config(runtime.raw_config)
    if by == "target":
        return aggregate_distance_weight_partitions_to_target(
            runtime, weight_config, source_type=source_type
        )
    if by == "source":
        return aggregate_distance_weight_partitions_to_source(
            runtime, weight_config, source_type=source_type
        )
    raise ValueError("by must be 'source' or 'target'")


def clean_distance_weights(config: str | Path, *, source_type: str) -> None:
    from ..config.distance import load_distance_runtime, load_distance_weight_config
    from ..weights.distance.compute import clean_distance_weight_outputs

    runtime = load_distance_runtime(config)
    weight_config = load_distance_weight_config(runtime.raw_config)
    clean_distance_weight_outputs(runtime, weight_config, source_type)


def finalize_view_score(config: str | Path, *, source_type: str, overwrite: bool = False) -> Path:
    from ..weights.view_score import finalize_view_score as finalize

    return finalize(config, source_type=source_type, overwrite=overwrite)


def run_terrain_weights(
    config: str | Path | AppConfig,
    *,
    source_type: str,
    overwrite: bool = False,
    limit: int | None = None,
    start: int = 0,
    max_workers: int | None = None,
    batch_size: int | None = None,
    combine: bool = True,
    clean_intermediates: bool = False,
) -> object:
    """Run canonical terrain weights for one source domain.

    Land production always uses the paired bare-earth/canopy workflow. The
    single-surface terrain runner is retained only for water and explicit
    low-level diagnostic commands.
    """

    if str(source_type).strip().lower() == "land":
        if limit is not None or start:
            raise ValueError(
                "Partial land terrain runs must use the explicit low-level terrain "
                "commands; production land weighting always runs the complete paired "
                "bare-earth/canopy contract."
            )
        return build_dual_surface_canopy_weights(
            config.config_path if isinstance(config, AppConfig) else config,
            overwrite=overwrite,
            max_workers=max_workers,
            batch_size=batch_size,
        )

    from ..weights.terrain.cleanup import cleanup_terrain_intermediates
    from ..weights.terrain.runner import run_source_cells

    app = _runtime_app(
        config,
        source_type=source_type,
        overwrite=overwrite,
        max_workers=max_workers,
        batch_size=batch_size,
    )
    if combine:
        app = replace(app, run=replace(app.run, combine_final_parquet=True))
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"h3\.aggregation_mode=sampled with pixel_stride > 1 is approximate.*",
            category=RuntimeWarning,
        )
        result = run_source_cells(app, limit=limit, start=start)
    if combine and clean_intermediates:
        cleanup_terrain_intermediates(app)
    return result


def build_dual_surface_canopy_weights(
    config: str | Path,
    *,
    overwrite: bool = False,
    max_workers: int | None = None,
    batch_size: int | None = None,
) -> object:
    from ..weights.canopy_visibility import run_dual_surface_canopy_weights

    return run_dual_surface_canopy_weights(
        config,
        overwrite=overwrite,
        max_workers=max_workers,
        batch_size=batch_size,
    )


def build_vegetation_path_weights(
    config: str | Path,
    *,
    source_type: str,
    overwrite: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    clean_intermediates: bool = True,
) -> Mapping[str, Path | None]:
    from ..weights.vegetation import run_vegetation_path_weights

    return run_vegetation_path_weights(
        config,
        overwrite=overwrite,
        dry_run=dry_run,
        limit=limit,
        clean_intermediates=clean_intermediates,
        source_type=source_type,
    )


def finalize_static_outputs(
    config: str | Path,
    *,
    overwrite: bool = False,
    clean_intermediates: bool = False,
) -> Mapping[str, Path]:
    from ..finalize.final_artifacts import (
        cleanup_viewshed_dir_to_static_outputs,
        materialize_static_viewability_outputs,
    )

    outputs = materialize_static_viewability_outputs(config, overwrite=overwrite)
    if clean_intermediates:
        cleanup_viewshed_dir_to_static_outputs(config)
    return outputs


def export_static_maps(
    config: str | Path,
    *,
    source_type: str = "all",
    overwrite: bool = False,
) -> object:
    from ..visualization.exports import (
        export_source_type_static_weight_map,
        export_static_weight_maps,
    )

    if source_type == "all":
        return export_static_weight_maps(config, overwrite=overwrite)
    return export_source_type_static_weight_map(
        config, source_type=source_type, overwrite=overwrite
    )


def run_stage(config: str | Path | AppConfig, invocation: StageInvocation) -> object:
    """Run one registered workflow stage without routing through a CLI parser."""

    config_path = config.config_path if isinstance(config, AppConfig) else config
    stage = invocation.stage
    if stage == "download-data":
        return download_data(config_path, overwrite=invocation.overwrite)
    if stage == "build-land-cells":
        return build_land_cells(config_path, overwrite=invocation.overwrite)
    if stage == "prepare-source-target-lookup":
        return prepare_source_target_lookup(config_path, overwrite=invocation.overwrite)
    if stage == "build-distance-weights":
        if invocation.source_type is None:
            raise ValueError("build-distance-weights requires source_type")
        return build_distance_weights(
            config_path,
            source_type=invocation.source_type,
            overwrite=invocation.overwrite,
            combine=True,
        )
    if stage == "build-dual-surface-canopy-weights":
        return build_dual_surface_canopy_weights(config_path, overwrite=invocation.overwrite)
    if stage == "terrain-weight":
        if invocation.source_type is None:
            raise ValueError("terrain-weight requires source_type")
        return run_terrain_weights(
            config,
            source_type=invocation.source_type,
            overwrite=invocation.overwrite,
        )
    if stage == "build-vegetation-path-weights":
        if invocation.source_type is None:
            raise ValueError("build-vegetation-path-weights requires source_type")
        return build_vegetation_path_weights(
            config_path,
            source_type=invocation.source_type,
            overwrite=invocation.overwrite,
        )
    if stage == "finalize-viewshed-lookups":
        return finalize_static_outputs(config_path, overwrite=invocation.overwrite)
    if stage == "export-static-maps":
        return export_static_maps(config_path, overwrite=invocation.overwrite)
    raise ValueError(f"Unknown viewshed stage: {stage}")
