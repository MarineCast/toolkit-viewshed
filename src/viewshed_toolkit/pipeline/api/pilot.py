"""Explicit one-source paired engineering pilot; no acquisition or publication."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import h3
import polars as pl

from viewshed_toolkit._internal.pilot import PilotCaps, supervise_pilot

from ..config import AppConfig, load_app_config
from ..config.study import study_selection
from ..weights.terrain.gdal import _area_lookup_path_for_app
from ..weights.terrain.runner import _validate_paired_surface_apps, run_paired_surface_source_cells


def validate_pilot_apps(bare: AppConfig, canopy: AppConfig, root: Path, source: str) -> int:
    """Preserve the canonical land policy and the complete selected candidate table."""
    _validate_paired_surface_apps(bare, canopy)
    if not h3.is_valid_cell(source) or h3.get_resolution(source) != 7:
        raise ValueError("Pilot requires exactly one canonical R7 source")
    if source != source.lower():
        raise ValueError("Pilot source H3 must be canonical lowercase")
    for app in (bare, canopy):
        if (
            app.h3.source_resolution != 7
            or app.h3.output_resolution != 7
            or app.h3.source_sampling_mode != "active_fraction"
            or app.h3.min_sample_points_per_source_cell != 5
            or app.h3.sample_points_per_source_cell != 10
            or app.viewshed.max_distance_m != 30000
            or app.viewshed.aoi_margin_m != 1000
            or app.viewshed.dem_resolution_m != 30
            or app.batch.max_workers != 1
            or app.batch.raster_stack_mode != "windowed"
        ):
            raise ValueError(
                "Pilot must retain R7, land sampling 5-10, 30 m/31 km support and one worker"
            )
        if (
            not app.run.keep_batch_intermediates
            or app.run.combine_final_parquet
            or app.run.overwrite
            or app.run.write_maps
            or app.run.write_geojson
            or app.run.write_cumulative_rasters
        ):
            raise ValueError(
                "Pilot must preserve batch checkpoints and disable combine/promotion/maps/overwrite"
            )
        for name in (
            "output_dir",
            "final_output_dir",
            "map_dir",
            "final_visibility_path",
            "partitioned_visibility_dir",
            "manifest_path",
            "projected_dem_path",
        ):
            path = Path(getattr(app.paths, name)).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Pilot mutable path must remain in task staging: {name}")
        for path in (app.paths.manifest_path, app.paths.partitioned_visibility_dir):
            if path.exists() and (path.is_file() or any(path.iterdir())):
                raise ValueError("Use fresh pilot manifests/partitions; preserve existing evidence")
    if (
        bare.paths.manifest_path == canopy.paths.manifest_path
        or bare.paths.partitioned_visibility_dir == canopy.paths.partitioned_visibility_dir
    ):
        raise ValueError("Paired pilot surface manifests and partitions must have separate paths")
    lookup = _area_lookup_path_for_app(canopy)
    if lookup is None:
        raise ValueError("Pilot requires a prepared complete candidate lookup")
    frame = pl.scan_parquet(lookup)
    if "source_type" in frame.collect_schema().names():
        frame = frame.filter(pl.col("source_type") == "land")
    selected = (
        frame.filter(pl.col("source_h3") == source)
        .select("source_h3", "target_h3")
        .limit(6001)
        .collect()
    )
    if not 0 < selected.height <= 6000:
        raise ValueError("Pilot selected role-pair count must be between 1 and 6000")
    keys = selected.select("source_h3", "target_h3")
    if keys.null_count().sum_horizontal().item() or keys.unique().height != keys.height:
        raise ValueError("Pilot candidate keys must be non-null and unique")
    return selected.height


def run_bounded_land_pilot(
    bare_config: Path,
    canopy_config: Path,
    *,
    source_h3: str,
    staging_root: Path,
    checkpoint_dir: Path,
    study_config: Path | None = None,
    caps: PilotCaps | None = None,
) -> dict[str, Any]:
    """Launch only after prepared config/source qualification and independent review.

    This API does not grant source-relative permission or weaken raster gates.
    Only a completed process is reported; pair completeness remains a separate check.
    """
    root = staging_root.resolve(strict=True)
    bare = load_app_config(bare_config, study_config=study_config)
    canopy = load_app_config(canopy_config, study_config=study_config)
    pairs = validate_pilot_apps(bare, canopy, root, source_h3)
    command = [
        sys.executable,
        "-m",
        __name__,
        str(bare_config.resolve()),
        str(canopy_config.resolve()),
        source_h3,
        str(root),
        str(study_config.resolve()) if study_config is not None else "-",
    ]
    result = supervise_pilot(command, staging_root=root, checkpoint_dir=checkpoint_dir, caps=caps)
    return {
        **result,
        "source_h3": source_h3,
        "source_type": "land",
        "selected_candidate_pairs": pairs,
        "scope": "one-source paired computational pilot; not a regional release",
    }


def _worker(arguments: list[str]) -> None:
    bare_path, canopy_path, source, root, study = arguments
    selected = None if study == "-" else Path(study)
    bare = load_app_config(Path(bare_path), study_config=selected)
    canopy = load_app_config(Path(canopy_path), study_config=selected)
    validate_pilot_apps(bare, canopy, Path(root), source)
    with study_selection(selected):
        bare_manifest, canopy_manifest = run_paired_surface_source_cells(
            bare, canopy, selected_source_cells=[source]
        )
    for frame in (bare_manifest, canopy_manifest):
        if len(frame) != 1 or set(frame["source_h3_cell"].astype(str)) != {source}:
            raise RuntimeError("Pilot did not complete exactly its one source on both surfaces")
        if "status" not in frame or not frame["status"].eq("ok").all():
            raise RuntimeError("Pilot source completion failed")
    print(json.dumps({"source_h3": source, "paired_source_completed": True}))


if __name__ == "__main__":
    _worker(sys.argv[1:])
