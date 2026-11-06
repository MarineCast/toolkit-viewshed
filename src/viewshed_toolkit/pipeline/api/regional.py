"""Read-only build planning, regional readiness and completed-product validation."""

import importlib
import importlib.util
import json
import math
import shutil
from pathlib import Path
from typing import Any

import polars as pl
import psutil
import pyarrow.parquet as pq
import rasterio
from pyproj import CRS, Transformer

from viewshed_toolkit.resources import default_config_path

from ..config import AppConfig, apply_source_type_policy, load_app_config
from ..config.datasets import DatasetsConfig
from ..config.paths import bbox_from_config
from ..config.schema import load_yaml_config
from ..contracts.artifacts import final_artifact_paths_from_raw
from ..contracts.components import cache_matches, component_path, component_root, provenance
from ..contracts.distance import pair_distance_path
from ..finalize.composition import validate_composed
from .components import component_run_context


def _required_raster_bounds(app: AppConfig) -> tuple[float, float, float, float]:
    transformer = Transformer.from_crs(4326, app.viewshed.crs_projected, always_xy=True)
    left, bottom, right, top = transformer.transform_bounds(
        *bbox_from_config(app.raw_config), densify_pts=21
    )
    margin = float(app.viewshed.max_distance_m + app.viewshed.aoi_margin_m)
    return left - margin, bottom - margin, right + margin, top + margin


def _input_header(path: Path, *, raster: bool, app: AppConfig) -> dict[str, Any]:
    """Inspect file metadata only; never read raster bands or geometry rows."""

    record: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "readable": False,
        "inspection": "metadata_only",
        "content_validated": False,
    }
    if not record["exists"]:
        record["status"] = "missing"
        return record
    try:
        record["bytes"] = path.stat().st_size
        if raster:
            with rasterio.open(path) as source:
                if source.crs is None or source.count < 1 or min(source.shape) < 1:
                    raise ValueError("Raster must have a CRS, band and nonempty grid")
                source_crs = CRS.from_user_input(source.crs)
                transformer = Transformer.from_crs(
                    source_crs, app.viewshed.crs_projected, always_xy=True
                )
                bounds_left, bounds_bottom, bounds_right, bounds_top = source.bounds
                projected = transformer.transform_bounds(
                    bounds_left, bounds_bottom, bounds_right, bounds_top, densify_pts=21
                )
                required = _required_raster_bounds(app)
                tolerance = 1e-6
                record.update(
                    crs=source_crs.to_string(),
                    width=source.width,
                    height=source.height,
                    bands=source.count,
                    dtypes=list(source.dtypes),
                    resolution=list(source.res),
                    bounds=list(source.bounds),
                    nodata=str(source.nodata) if source.nodata is not None else None,
                    configured_grid_matches=(
                        source_crs == CRS.from_user_input(app.viewshed.crs_projected)
                        and all(
                            math.isclose(value, app.viewshed.dem_resolution_m)
                            for value in source.res
                        )
                    ),
                    envelope_covers_required_bounds=(
                        projected[0] <= required[0] + tolerance
                        and projected[1] <= required[1] + tolerance
                        and projected[2] >= required[2] - tolerance
                        and projected[3] >= required[3] - tolerance
                    ),
                    required_bounds_projected=list(required),
                    observed_pixel_coverage="unverified_no_band_scan",
                )
        elif path.suffix.lower() in {".parquet", ".geoparquet"}:
            metadata = pq.read_metadata(path)  # type: ignore[no-untyped-call, unused-ignore]
            geo = json.loads((metadata.metadata or {}).get(b"geo", b"{}"))
            primary = geo.get("primary_column")
            column = geo.get("columns", {}).get(primary)
            if not isinstance(column, dict):
                raise ValueError("Geometry Parquet has no primary GeoParquet metadata")
            declared_crs = column.get("crs", "OGC:CRS84")
            if declared_crs is None:
                raise ValueError("Geometry Parquet declares an unknown CRS")
            record.update(
                crs=CRS.from_user_input(declared_crs).to_string(),
                rows=metadata.num_rows,
                bounds=column.get("bbox"),
                geometry_types=column.get("geometry_types"),
            )
        else:
            import pyogrio

            info = pyogrio.read_info(  # type: ignore[no-untyped-call, unused-ignore]
                path, force_feature_count=False, force_total_bounds=False
            )
            if not info.get("crs"):
                raise ValueError("Geometry input has no declared CRS")
            bounds = info.get("total_bounds")
            record.update(
                crs=CRS.from_user_input(info["crs"]).to_string(),
                rows=int(info["features"]) if info.get("features", -1) >= 0 else None,
                bounds=[float(value) for value in bounds] if bounds is not None else None,
                geometry_types=[info.get("geometry_type")],
            )
        record.update(readable=True, status="readable_metadata_only")
    except Exception as exc:
        record.update(status="unreadable", error=f"{type(exc).__name__}: {exc}")
    return record


def _native_capabilities(app: AppConfig, *, raster_required: bool) -> dict[str, Any]:
    if not raster_required:
        return {"required": False, "ready": True, "los_mode": "raster_free"}
    record: dict[str, Any] = {
        "required": True,
        "ready": False,
        "preparation_requires_python_gdal": True,
        "gdal_cli": shutil.which("gdal_viewshed"),
        "python_gdal_available": False,
    }
    try:
        gdal = importlib.import_module("osgeo.gdal")
        record.update(
            python_gdal_available=True,
            python_gdal_version=str(gdal.VersionInfo("RELEASE_NAME")),
            preparation_available=all(hasattr(gdal, name) for name in ("BuildVRT", "Warp")),
            in_process_viewshed_available=hasattr(gdal, "ViewshedGenerate"),
        )
    except Exception as exc:
        record.update(
            preparation_available=False,
            in_process_viewshed_available=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if app.viewshed.backend == "xrspatial":
        packages = {
            name: importlib.util.find_spec(name) is not None
            for name in ("xrspatial", "xarray", "rioxarray")
        }
        record.update(los_mode="experimental_xrspatial", experimental_packages=packages)
        los_ready = all(packages.values())
        record["numerical_backend_validated"] = False
    elif app.run.keep_intermediate_rasters:
        record["los_mode"] = "cli_requested_for_retained_rasters"
        los_ready = record["gdal_cli"] is not None
    elif record["in_process_viewshed_available"]:
        record["los_mode"] = "in_process_mem"
        los_ready = True
    else:
        record["los_mode"] = "cli_fallback" if record["gdal_cli"] else "unavailable"
        los_ready = record["gdal_cli"] is not None
    record["ready"] = bool(record["preparation_available"] and los_ready)
    return record


def _resource_snapshot(app: AppConfig) -> dict[str, Any]:
    disks = {}
    for name, configured in (("work", app.paths.output_dir), ("final", app.paths.final_output_dir)):
        existing = configured.resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            usage = shutil.disk_usage(existing)
            disks[name] = {
                "checked_path": str(existing),
                "free_bytes": usage.free,
                "total_bytes": usage.total,
            }
        except OSError as exc:
            disks[name] = {"checked_path": str(existing), "error": str(exc)}
    try:
        memory: dict[str, Any] = {
            "available_bytes": int(psutil.virtual_memory().available),
            "total_bytes": int(psutil.virtual_memory().total),
        }
    except (OSError, RuntimeError) as exc:
        memory = {"available_bytes": None, "error": str(exc)}
    return {"disk": disks, "memory": memory}


def _table_rows(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return int(pq.read_metadata(path).num_rows)  # type: ignore[no-untyped-call, unused-ignore]
    except Exception:
        return None


def _workload_metadata(app: AppConfig, plan: tuple[str, ...], source_type: str) -> dict[str, Any]:
    artifacts = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    source_rows = _table_rows(app.paths.land_h3_path)
    pair_rows = _table_rows(artifacts.source_target_lookup)
    report: dict[str, Any] = {
        "source_table_rows_unverified": source_rows,
        "lookup_rows_all_roles_unverified": pair_rows,
        "count_scope": "existing table headers; current role selection and cache identity unverified",
        "eta_seconds": None,
        "eta_status": "requires_representative_benchmark",
        "peak_disk_bytes": None,
        "peak_disk_status": "not_estimated_without_input_and_staging_inventory",
    }
    if "build-dem-weights" not in plan:
        return report
    effective = apply_source_type_policy(app, source_type)
    samples = effective.h3.sample_points_per_source_cell
    report["maximum_observers_per_source"] = samples
    if source_type == "water":
        targets = int(
            app.raw_config.get("water_viewing", {})
            .get("land_mask", {})
            .get("target_samples_per_cell", 3)
        )
        report["maximum_target_samples_per_cell"] = targets
        report["sample_pair_evaluations_upper_bound_from_unverified_lookup"] = (
            pair_rows * samples * targets if pair_rows is not None else None
        )
        return report
    surface_count = 2 if "build-chm-weights" in plan else 1
    side = 2 * math.ceil(app.viewshed.max_distance_m / app.viewshed.dem_resolution_m) + 5
    bounds = _required_raster_bounds(app)
    width = math.ceil((bounds[2] - bounds[0]) / app.viewshed.dem_resolution_m) + 2
    height = math.ceil((bounds[3] - bounds[1]) / app.viewshed.dem_resolution_m) + 2
    report.update(
        surfaces_per_observer=surface_count,
        observer_window_pixels_upper_bound=side * side,
        source_observer_calls_upper_bound_from_unverified_source_table=(
            source_rows * samples * surface_count if source_rows is not None else None
        ),
        prepared_float32_raster_bytes_estimate=width * height * 4 * surface_count,
        raster_storage_scope="uncompressed prepared DEM/CHM only; excludes caches, intermediates and outputs",
    )
    return report


def plan_components(
    config: str | Path | AppConfig,
    *,
    target: str = "all",
    source_type: str = "land",
    run_id: str = "components",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Plan a component build without acquisition or runtime output writes.

    Readable metadata establishes prerequisites, not scientific coverage or cache
    validity. Existing products remain explicitly unverified until normal stage
    validation runs. Configured shared-study identity validation remains mandatory;
    after loading configuration, planning performs no additional input-content
    hashes or raster/geometry row scans. No ETA is inferred from unmeasured work.
    """

    app = config if isinstance(config, AppConfig) else load_app_config(config)
    plan, manifest, identity = component_run_context(
        app, target=target, source_type=source_type, run_id=run_id
    )
    root = component_root(app)
    issues: list[dict[str, str]] = []
    inputs = {
        name: _input_header(path, raster=False, app=app)
        for name, path in (
            ("land", app.paths.land_polygon_path),
            ("water", app.paths.water_polygon_path),
        )
    }
    for name, record in inputs.items():
        if not record["readable"]:
            issues.append(
                {
                    "severity": "error",
                    "input": name,
                    "message": "Required geometry is missing or unreadable; acquisition does not create it",
                }
            )
    datasets = DatasetsConfig.model_validate(app.raw_config.get("datasets", {}))
    acquisition = []
    for name, path in (("dem", app.paths.regional_dem_path), ("chm", app.paths.canopy_height_path)):
        if f"prepare-{name}" not in plan:
            continue
        settings = getattr(datasets, name)
        record = _input_header(path, raster=True, app=app)
        record["preparation_planned"] = True
        inputs[name.upper()] = record
        item: dict[str, Any] = {
            "dataset": name,
            "provider": settings.provider,
            "enabled": settings.enabled,
            "status": "planned_cache_validation_required",
            "network_access_checked": False,
        }
        if not settings.enabled:
            issues.append(
                {"severity": "error", "input": name, "message": "Required dataset is disabled"}
            )
        if settings.provider == "local":
            sources = [
                _input_header(Path(asset), raster=True, app=app) for asset in settings.assets
            ]
            item["local_assets"] = sources
            if any(not source["readable"] for source in sources):
                issues.append(
                    {
                        "severity": "error",
                        "input": name,
                        "message": "A configured local acquisition asset is missing or unreadable",
                    }
                )
        acquisition.append(item)
        if not record["readable"] or not record.get("envelope_covers_required_bounds", False):
            issues.append(
                {
                    "severity": "warning",
                    "input": name,
                    "message": "Prepared raster needs acquisition/preparation or coverage validation",
                }
            )
    capabilities = _native_capabilities(app, raster_required=bool(acquisition))
    if not capabilities["ready"]:
        issues.append(
            {
                "severity": "error",
                "input": "backend",
                "message": "Native capabilities do not support the planned preparation and LOS",
            }
        )
    manifest_status: dict[str, Any] = {
        "path": str(manifest),
        "exists": manifest.exists(),
        "identity": identity,
        "conflict": False,
    }
    if manifest.exists():
        try:
            if manifest.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Manifest exceeds the 8 MiB bounded preflight read")
            previous = json.loads(manifest.read_text())
            same = previous.get("identity") == identity
            manifest_status.update(
                identity_matches=same,
                conflict=not same and not overwrite,
                overwrite_authorized=overwrite,
            )
            if manifest_status["conflict"]:
                issues.append(
                    {
                        "severity": "error",
                        "input": "run_id",
                        "message": "Manifest belongs to another logical run; use a new run_id or --overwrite",
                    }
                )
        except (OSError, ValueError, AttributeError) as exc:
            manifest_status["error"] = str(exc)
            if not overwrite:
                issues.append({"severity": "error", "input": "run_id", "message": str(exc)})
    caches = []
    artifacts = final_artifact_paths_from_raw(app.raw_config, app.config_path.parent)
    products = {
        "source_cells": app.paths.land_h3_path,
        "target_cells": root / "geometry" / "target_cells.parquet",
        "pair_lookup": artifacts.source_target_lookup,
        **{
            name: component_path(app, name, source_type)
            for name in ("dem", "chm", "distance", "static")
        },
    }
    if "build-pair-distances" in plan:
        products["pair_distances"] = pair_distance_path(app, source_type)
    for name, path in products.items():
        caches.append(
            {
                "product": name,
                "path": str(path),
                "exists": path.is_file(),
                "reuse_status": "present_but_unverified" if path.is_file() else "absent",
            }
        )
    return {
        "read_only": True,
        "configuration_validation": {
            "shared_study_identity_validation_required": "shared_study" in app.raw_config,
            "scope": "owning loader validation, including configured shared-study artifact checksums",
        },
        "ready": not any(issue["severity"] == "error" for issue in issues),
        "target": target,
        "source_type": source_type,
        "stages": list(plan),
        "output_roots": {
            "work": str(app.paths.output_dir),
            "final": str(app.paths.final_output_dir),
            "components": str(root),
        },
        "lifecycle": {
            "automatic_cleanup": False,
            "promotion": "per-role component artifacts; no combined generation pointer",
        },
        "run": manifest_status,
        "inputs": inputs,
        "missing_inputs": [name for name, record in inputs.items() if not record["exists"]],
        "planned_acquisition": acquisition,
        "backend": capabilities,
        "caches": caches,
        "resources": _resource_snapshot(app),
        "workload": _workload_metadata(app, plan, source_type),
        "issues": issues,
        "limits": [
            "No additional input-content hashes or raster/geometry row scans after configuration validation",
            "No provider freshness or network check",
            "Raster envelope coverage does not prove observed-data coverage",
            "No regional runtime prediction",
        ],
    }


def validate_region(
    config: str | Path, *, require_outputs: bool = True, source_type: str | None = None
) -> dict[str, Any]:
    app = load_app_config(config)
    canonical = load_yaml_config(default_config_path(), apply_study=False)
    bbox = bbox_from_config(app.raw_config)
    matches_canonical = bbox == bbox_from_config(canonical)
    explicit_case_study = bool(
        {"case_study", "marinecast_study", "shared_study"} & app.raw_config.keys()
    )
    if not matches_canonical and not explicit_case_study:
        raise ValueError("Regional validation requires the canonical OrcaCast SRKW model_area bbox")
    if source_type not in {None, "land", "water"}:
        raise ValueError("source_type must be land or water")
    required = {"land": app.paths.land_polygon_path, "water": app.paths.water_polygon_path}
    if source_type != "water":
        required.update(DEM=app.paths.regional_dem_path, CHM=app.paths.canopy_height_path)
    inspected = {
        name: _input_header(path, raster=name in {"DEM", "CHM"}, app=app)
        for name, path in required.items()
    }
    missing = [name for name, record in inspected.items() if not record["exists"]]
    invalid = [
        name
        for name, record in inspected.items()
        if not record["readable"] or record.get("envelope_covers_required_bounds") is False
    ]
    capabilities = _native_capabilities(app, raster_required=source_type != "water")
    report: dict[str, Any] = {
        "bbox": list(bbox),
        "canonical_domain": app.region.name if explicit_case_study else "OrcaCast SRKW model_area",
        "matches_canonical_model_area": matches_canonical,
        "bbox_policy": (
            "marinecast_study"
            if "marinecast_study" in app.raw_config
            else "explicit_case_study" if explicit_case_study else "canonical_model_area"
        ),
        "missing_inputs": missing,
        "invalid_inputs": invalid,
        "input_details": inspected,
        "backend": capabilities,
        "valid": not invalid and capabilities["ready"],
        "outputs": {},
    }
    if not report["valid"]:
        return report
    if require_outputs:
        for role in ((source_type,) if source_type is not None else ("land", "water")):
            paths = {name: component_path(app, name, role) for name in ("dem", "chm", "distance")}
            path = component_path(app, "static", role)
            if not path.exists() or not all(value.exists() for value in paths.values()):
                report["outputs"][role] = "missing"
                report["valid"] = False
                continue
            validate_composed(app, role)
            final = component_root(app) / "final" / f"{role}_static_weights.parquet"
            if not cache_matches(
                final, provenance(app, "final_static_component_v1", {"composition": path})
            ):
                raise ValueError(f"Stale or corrupt regional {role} final artifact")
            frame = pl.read_parquet(path)
            report["outputs"][role] = {"rows": frame.height}
    return report
