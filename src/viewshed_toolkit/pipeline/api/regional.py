"""Read-only regional readiness and completed-product validation."""

from pathlib import Path
from typing import Any

import polars as pl

from viewshed_toolkit.resources import default_config_path

from ..config import load_app_config
from ..config.paths import bbox_from_config
from ..contracts.components import cache_matches, component_path, component_root, provenance
from ..finalize.composition import validate_composed


def validate_region(config: str | Path, *, require_outputs: bool = True) -> dict[str, Any]:
    app = load_app_config(config)
    canonical = load_app_config(default_config_path())
    bbox = bbox_from_config(app.raw_config)
    matches_canonical = bbox == bbox_from_config(canonical.raw_config)
    explicit_case_study = "case_study" in app.raw_config
    if not matches_canonical and not explicit_case_study:
        raise ValueError("Regional validation requires the canonical OrcaCast SRKW model_area bbox")
    required = {
        "DEM": app.paths.regional_dem_path,
        "CHM": app.paths.canopy_height_path,
        "land": app.paths.land_polygon_path,
        "water": app.paths.water_polygon_path,
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    report: dict[str, Any] = {
        "bbox": list(bbox),
        "canonical_domain": app.region.name if explicit_case_study else "OrcaCast SRKW model_area",
        "matches_canonical_model_area": matches_canonical,
        "bbox_policy": "explicit_case_study" if explicit_case_study else "canonical_model_area",
        "missing_inputs": missing,
        "valid": not missing,
        "outputs": {},
    }
    if missing:
        return report
    if require_outputs:
        for source_type in ("land", "water"):
            paths = {
                name: component_path(app, name, source_type) for name in ("dem", "chm", "distance")
            }
            path = component_path(app, "static", source_type)
            if not path.exists() or not all(value.exists() for value in paths.values()):
                report["outputs"][source_type] = "missing"
                report["valid"] = False
                continue
            validate_composed(app, source_type)
            final = component_root(app) / "final" / f"{source_type}_static_weights.parquet"
            if not cache_matches(
                final, provenance(app, "final_static_component_v1", {"composition": path})
            ):
                raise ValueError(f"Stale or corrupt regional {source_type} final artifact")
            frame = pl.read_parquet(path)
            report["outputs"][source_type] = {"rows": frame.height}
    return report
