"""Read-only local readiness checks; no acquisition, writes, or model execution."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

import rasterio

from ..config import load_app_config


def inspect_environment(config: str | Path, *, workflow: str = "land") -> dict[str, Any]:
    if workflow not in {"land", "water", "distance"}:
        raise ValueError("workflow must be land, water or distance")
    app = load_app_config(config)
    required = {"land": app.paths.land_polygon_path, "water": app.paths.water_polygon_path}
    versions = {
        "python": platform.python_version(),
        "rasterio": rasterio.__version__,
        "rasterio_gdal": rasterio.__gdal_version__,
    }
    errors = []
    if workflow == "land":
        required.update(DEM=app.paths.regional_dem_path, CHM=app.paths.canopy_height_path)
        try:
            from osgeo import gdal

            versions["gdal_bindings"] = str(gdal.VersionInfo("RELEASE_NAME"))
        except ImportError:
            errors.append("GDAL Python bindings unavailable for the land workflow")
    missing = {name: str(path) for name, path in required.items() if not path.is_file()}
    if missing:
        errors.append("Required local inputs are missing; see missing_inputs")
    outputs = {}
    for name, path in {
        "work": app.paths.output_dir,
        "durable": app.paths.final_output_dir,
        "maps": app.paths.map_dir,
    }.items():
        ancestor = path.resolve()
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        writable = ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
        outputs[name] = {
            "path": str(path),
            "existing_ancestor": str(ancestor),
            "permission_hint": writable,
        }
        if not writable:
            errors.append(f"Output directory has no writable directory ancestor: {name}")
    if app.paths.final_output_dir.resolve().is_relative_to(app.paths.output_dir.resolve()):
        errors.append("Component durable output must be outside the work directory")
    return {
        "valid": not errors,
        "workflow": workflow,
        "config_hash": app.config_hash,
        "versions": versions,
        "missing_inputs": missing,
        "outputs": outputs,
        "errors": errors,
        "scope": "Local presence and permission hints only; no writes, downloads, schema validation or native ABI certification",
    }
