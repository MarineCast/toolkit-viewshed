"""Independent durable DEM, conditional CHM, and centroid-distance products.

The existing LOS kernels and sampling design remain the numerical implementation.
No component reads the centroid-distance artifact except its own distance stage.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl

from ..config import AppConfig, apply_source_type_policy, initialize_app_config
from ..contracts.artifacts import final_artifact_paths_from_raw
from ..contracts.components import (
    PAIR_KEYS,
    cache_matches,
    component_path,
    provenance,
    validate_pairs,
    write_component,
)
from ..contracts.pairs import validate_los_diagnostics


def _distance_component_contract(app: AppConfig, source_type: str) -> dict[str, Any]:
    """Return dependency-scoped provenance for the compatibility distance table."""

    from ..contracts.distance import package_version, pair_distance_path
    from .distance.products import (
        default_distance_profile,
        distance_profile_output_path,
        validate_distance_product,
    )

    pair_path = pair_distance_path(app, source_type)
    profile_path = distance_profile_output_path(pair_path, default_distance_profile(app))
    pair_record = validate_distance_product(pair_path)
    profile_record = validate_distance_product(profile_path)
    return {
        "schema_version": "static_components_v1",
        "algorithm_version": "default_distance_component_v2",
        "software_version": package_version(),
        "source_type": source_type,
        "source_h3_resolution": app.h3.source_resolution,
        "target_h3_resolution": app.h3.target_resolution,
        "inputs": {
            "pair_distances": pair_record["output_checksum"],
            "distance_profile": profile_record["output_checksum"],
        },
        "profile_scientific_identity": profile_record["contract"]["profile_scientific_identity"],
        "distance_role": "centroid_diagnostic_only; attenuation_already_integrated_in_LOS",
    }


def _lookup(app: AppConfig, source_type: str) -> tuple[Path, pl.DataFrame]:
    if source_type not in {"land", "water"}:
        raise ValueError("source_type must be land or water")
    path = final_artifact_paths_from_raw(
        app.raw_config, app.config_path.parent
    ).source_target_lookup
    frame = (
        pl.scan_parquet(path)
        .filter(pl.col("source_type") == source_type)
        .select("source_h3", "target_h3", "distance_km")
        .collect(engine="streaming")
    )
    validate_pairs(frame)
    return path, frame


def _surface_variant(app: AppConfig, surface: str, source_type: str) -> AppConfig:
    from .canopy_visibility import make_terrain_variant_app

    return apply_source_type_policy(
        make_terrain_variant_app(
            app,
            surface_model=surface,
            terrain_dir=app.paths.output_dir / "components" / source_type / surface,
        ),
        source_type,
    )


def build_distance_component(
    app: AppConfig, *, source_type: str = "land", overwrite: bool = False
) -> Path:
    from .distance.products import (
        build_distance_profile,
        build_pair_distances,
        default_distance_profile,
    )

    pair_path = build_pair_distances(app, source_type=source_type, overwrite=overwrite)
    default_profile = default_distance_profile(app)
    profile_path = build_distance_profile(pair_path, default_profile, overwrite=overwrite)
    path = component_path(app, "distance", source_type)
    contract = _distance_component_contract(app, source_type)
    if not overwrite and cache_matches(path, contract):
        return path
    frame = pl.read_parquet(profile_path).select(
        *PAIR_KEYS,
        pl.col("distance_km").cast(pl.Float32),
        pl.col("distance_m").cast(pl.Float64),
        pl.col("weight_distance").cast(pl.Float32),
        pl.lit(default_profile.selected_model).alias("distance_model"),
    )
    return write_component(frame, path, contract, weights=("weight_distance",))


def _surface_component(
    app: AppConfig, surface: str, *, source_type: str, overwrite: bool
) -> tuple[pl.DataFrame, dict[str, Any]]:
    from .terrain.runner import run_source_cells

    lookup_path, lookup = _lookup(app, source_type)
    inputs = {
        "lookup": lookup_path,
        "land": app.paths.land_polygon_path,
        "water": app.paths.water_polygon_path,
    }
    if source_type == "land":
        inputs["dem"] = app.paths.regional_dem_path
        inputs["sources"] = app.paths.land_h3_path
        if surface == "canopy":
            inputs["chm"] = app.paths.canopy_height_path
    contract = provenance(app, f"{source_type}_{surface}_existing_los_v1", inputs)
    contract["distance_role"] = "observer_pixel_attenuation_integrated_in_weight_terrain"
    if lookup.is_empty():
        return (
            pl.DataFrame(
                schema={
                    "source_h3": pl.String,
                    "target_h3": pl.String,
                    "weight_terrain": pl.Float32,
                    "terrain_visibility": pl.Float32,
                }
            ),
            contract,
        )
    # Separate stage execution uses the same deterministic observer design and
    # the same backend as paired execution. It never invokes the water policy for land.
    variant = _surface_variant(app, surface, source_type)
    variant = replace(
        variant, run=replace(variant.run, overwrite=overwrite, combine_final_parquet=False)
    )
    completed = run_source_cells(initialize_app_config(variant))
    return _dense_surface_result(variant, lookup, completed), contract


def _dense_surface_result(variant: AppConfig, lookup: pl.DataFrame, completed: Any) -> pl.DataFrame:
    """Validate completion before absent sparse pairs can represent observed zero."""
    expected_sources = set(lookup["source_h3"].unique().to_list())
    if (
        "source_h3_cell" not in completed.columns
        or set(completed["source_h3_cell"]) != expected_sources
    ):
        raise ValueError(
            "Incomplete LOS source execution; cannot infer observed zero for absent pairs"
        )
    if (
        completed["source_h3_cell"].duplicated().any()
        or "status" not in completed.columns
        or not completed["status"].isin(["ok", "skipped_existing", "water_land_mask"]).all()
    ):
        raise ValueError("LOS sources must each have one successful completion record")
    paths = [
        variant.paths.partitioned_visibility_dir
        / f"source={variant.source_type}"
        / f"source_h3_cell={source}.parquet"
        for source in sorted(expected_sources)
    ]
    sparse = pl.read_parquet(paths)
    validate_pairs(sparse, ("weight_terrain",))
    validate_los_diagnostics(sparse.lazy())
    if sparse.join(lookup.select(PAIR_KEYS), on=PAIR_KEYS, how="anti").height:
        raise ValueError("LOS partition contains pairs outside its canonical evaluated universe")
    # Absence in successfully completed sparse partitions means observed no LOS.
    # Nulls within an observed row are rejected above and never converted to zero.
    columns = [*PAIR_KEYS, "weight_terrain"]
    if "joint_los_fraction" in sparse.columns:
        columns.append("joint_los_fraction")
    dense = lookup.select(PAIR_KEYS).join(
        sparse.select(columns).with_columns(pl.lit(True).alias("__observed")),
        on=PAIR_KEYS,
        how="left",
        validate="1:1",
    )
    dense = dense.with_columns(pl.col("weight_terrain").fill_null(0.0))
    dense = dense.with_columns(
        (
            pl.when(pl.col("__observed").is_null())
            .then(0.0)
            .otherwise(pl.col("joint_los_fraction"))
            .alias("terrain_visibility")
            if "joint_los_fraction" in dense.columns
            else pl.lit(None, dtype=pl.Float32).alias("terrain_visibility")
        ),
        pl.lit("computed").alias("support_state"),
    )
    return dense.drop("__observed")


def _surface_contract(app: AppConfig, source_type: str, canopy: bool = False) -> dict[str, Any]:
    from ..contracts.lineage import factor_contract
    from .terrain.gdal import expected_partition_metadata

    lookup_path = final_artifact_paths_from_raw(
        app.raw_config, app.config_path.parent
    ).source_target_lookup
    inputs = {
        "lookup": lookup_path,
        "land": app.paths.land_polygon_path,
        "water": app.paths.water_polygon_path,
    }
    if source_type == "land":
        inputs.update(dem=app.paths.regional_dem_path, sources=app.paths.land_h3_path)
        if canopy:
            inputs.update(chm=app.paths.canopy_height_path, dem_weights=component_path(app, "dem"))
    contract = provenance(app, f"{'chm' if canopy else 'dem'}_component_v2_direct_los", inputs)
    contract.pop("config_hash")
    contract["producer_science"] = factor_contract(
        app.raw_config, app.config_path.parent, source_type, "canopy" if canopy else "terrain"
    )
    contract["source_type"] = source_type
    surface = "canopy" if canopy and source_type == "land" else "bare_earth"
    contract["terrain_partition_identity"] = expected_partition_metadata(
        _surface_variant(app, surface, source_type)
    )
    return contract


def build_dem_component(
    app: AppConfig, *, source_type: str = "land", overwrite: bool = False
) -> Path:
    path = component_path(app, "dem", source_type)
    contract = _surface_contract(app, source_type)
    if not overwrite and cache_matches(path, contract):
        return path
    frame, _ = _surface_component(app, "bare_earth", source_type=source_type, overwrite=overwrite)
    return write_component(frame, path, contract, weights=("weight_terrain",))


def build_chm_component(
    app: AppConfig, *, source_type: str = "land", overwrite: bool = False
) -> Path:
    path = component_path(app, "chm", source_type)
    contract = _surface_contract(app, source_type, canopy=True)
    if not overwrite and cache_matches(path, contract):
        return path
    if source_type == "water":
        _, lookup = _lookup(app, source_type)
        frame = lookup.select(PAIR_KEYS).with_columns(
            pl.lit(1.0).alias("weight_vegetation"),
            pl.lit("not_applicable").alias("canopy_support"),
        )
    else:
        dem_path = component_path(app, "dem")
        if not cache_matches(dem_path, _surface_contract(app, "land")):
            raise ValueError("DEM component missing or stale; run build-dem-weights first")
        bare = pl.read_parquet(dem_path).select(*PAIR_KEYS, "weight_terrain")
        canopy, _ = _surface_component(app, "canopy", source_type="land", overwrite=overwrite)
        frame = _conditional_canopy(bare, canopy)
    return write_component(frame, path, contract, weights=("weight_vegetation",))


def _conditional_canopy(bare: pl.DataFrame, canopy: pl.DataFrame) -> pl.DataFrame:
    validate_pairs(bare, ("weight_terrain",))
    frame = bare.select(*PAIR_KEYS, "weight_terrain").join(
        canopy.select(
            *PAIR_KEYS,
            pl.col("weight_terrain").alias("canopy_los_raw"),
            pl.col("terrain_visibility").alias("canopy_visibility"),
        ),
        on=PAIR_KEYS,
        validate="1:1",
    )
    if frame.height != bare.height:
        raise ValueError("Canopy and DEM pair universes differ")
    return frame.with_columns(
        pl.when(pl.col("weight_terrain") > 0)
        .then(pl.min_horizontal("canopy_los_raw", "weight_terrain") / pl.col("weight_terrain"))
        .otherwise(1.0)
        .alias("weight_vegetation"),
        pl.when(pl.col("weight_terrain") > 0)
        .then(pl.lit("computed"))
        .otherwise(pl.lit("no_baseline_support_neutral"))
        .alias("canopy_support"),
    ).drop("weight_terrain")


def build_land_surface_components(app: AppConfig, *, overwrite: bool = False) -> dict[str, Path]:
    """Build requested land surfaces together only when both require computation.

    Reuse the paired runner within the existing component scratch namespace;
    independent stages and component promotion/cleanup behavior stay unchanged.
    """
    from .terrain.runner import run_paired_surface_source_cells

    variants = {
        surface: replace(
            variant,
            run=replace(variant.run, overwrite=overwrite, combine_final_parquet=False),
        )
        for surface in ("bare_earth", "canopy")
        for variant in (_surface_variant(app, surface, "land"),)
    }
    dem_path = component_path(app, "dem")
    chm_path = component_path(app, "chm")
    dem_contract = _surface_contract(app, "land")
    dem_current = not overwrite and cache_matches(dem_path, dem_contract)
    chm_current = (
        not overwrite
        and dem_path.is_file()
        and cache_matches(chm_path, _surface_contract(app, "land", canopy=True))
    )
    if dem_current or chm_current:
        return {
            "build-dem-weights": build_dem_component(app, overwrite=overwrite),
            "build-chm-weights": build_chm_component(app, overwrite=overwrite),
        }
    _, lookup = _lookup(app, "land")
    if lookup.is_empty():
        return {
            "build-dem-weights": build_dem_component(app, overwrite=overwrite),
            "build-chm-weights": build_chm_component(app, overwrite=overwrite),
        }
    bare_app = initialize_app_config(variants["bare_earth"])
    canopy_app = initialize_app_config(variants["canopy"])
    bare_completed, canopy_completed = run_paired_surface_source_cells(bare_app, canopy_app)
    bare = _dense_surface_result(bare_app, lookup, bare_completed)
    write_component(bare, dem_path, dem_contract, weights=("weight_terrain",))
    # Retain only the matching denominator while reading the second sparse surface.
    bare = bare.select(*PAIR_KEYS, "weight_terrain")
    canopy = _dense_surface_result(canopy_app, lookup, canopy_completed)
    frame = _conditional_canopy(bare, canopy)
    write_component(
        frame,
        chm_path,
        _surface_contract(app, "land", canopy=True),
        weights=("weight_vegetation",),
    )
    return {"build-dem-weights": dem_path, "build-chm-weights": chm_path}
