"""Finalization and cleanup entry points for viewshed artifacts.

Scientific and operational role
-------------------------------
This module supports the viewshed scientific stages without owning their core
model equations. It provides common configuration dataclasses, raster helpers,
H3 geometry helpers, batching logic, water-domain preparation, input validation,
Parquet utilities, and final lookup materialization.

The central scientific contract supported here is:

    viewability_weight =
        weight_terrain * weight_vegetation

The terrain kernel integrates observer-to-pixel distance decay. The centroid
distance artifact is retained as a diagnostic; final composition multiplies
the terrain kernel only by conditional vegetation attenuation.

Current compact factor-table schemas are:

    terrain:    source_h3, target_h3, weight_terrain
    distance:   source_h3, target_h3, distance_km, weight_distance
    vegetation: source_h3, target_h3, source_type, weight_vegetation,
                vegetation_status

Major responsibilities
----------------------
- Load the viewshed YAML config into typed runtime dataclasses.
- Prepare projected DEM and batch-level water masks for terrain viewsheds.
- Generate source-cell sample points and source-cell batches.
- Validate required DEM, water, and land/source H3 inputs.
- Stream/merge Parquet partition outputs without eager full Pandas reads where
  possible.
- Compose final terrain, distance, vegetation, and viewability lookup tables.
- Optionally clean intermediate partitions after safe finalization.

Boundary of responsibility
--------------------------
Do not add terrain line-of-sight math, distance-decay models, or vegetation
attenuation science here. Those belong in `weights.terrain`, `weights.distance`,
and `weights.vegetation`. This module should remain shared plumbing plus the
explicit final composition step.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import (
    DEFAULT_CONFIG,
    load_yaml_config,
    output_dir_from_config,
)
from ..contracts.cleanup import cleanup_data_contract
from ..finalize.final_artifacts import (
    cleanup_viewshed_dir_to_static_outputs,
    materialize_static_viewability_outputs,
)


def clean_intermediates(config_path: Path, final_outputs: dict[str, Path]) -> list[Path]:
    missing = [path for path in final_outputs.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Refusing to clean intermediates because final outputs are missing:\n"
            + "\n".join(str(path) for path in missing)
        )

    raw = load_yaml_config(config_path)
    benchmark_dir = output_dir_from_config(raw, config_path.parent) / "benchmarks"
    return cleanup_data_contract(
        config_path,
        remove_stage_scratch=True,
        preserve_paths=[*final_outputs.values(), benchmark_dir],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize final static viewshed weights and optionally remove "
            "intermediate viewshed data."
        )
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--clean-intermediates",
        action="store_true",
        help=(
            "After successful materialization, delete non-static files strictly "
            "under the configured viewshed output directory."
        ),
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser().resolve()
    outputs = materialize_static_viewability_outputs(config_path, overwrite=args.overwrite)
    print("Finalized viewshed outputs:")
    for name, path in outputs.items():
        print(f"  {name}: {path}")
    if args.clean_intermediates:
        removed = cleanup_viewshed_dir_to_static_outputs(config_path)
        print("Removed intermediate paths:")
        print(json.dumps([str(path) for path in removed], indent=2))
    else:
        print("Kept intermediate paths (pass --clean-intermediates to remove them).")


if __name__ == "__main__":
    main()
