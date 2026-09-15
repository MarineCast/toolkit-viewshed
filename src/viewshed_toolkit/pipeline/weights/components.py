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
from ..config.distance import load_distance_weight_config
from ..contracts.artifacts import final_artifact_paths_from_raw
from ..contracts.components import (
    PAIR_KEYS,
    cache_matches,
    component_path,
    provenance,
    validate_pairs,
    write_component,
)


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


def build_distance_component(
    app: AppConfig, *, source_type: str = "land", overwrite: bool = False
) -> Path:
    from .distance.compute import distance_weight_values

    lookup_path, lookup = _lookup(app, source_type)
    path = component_path(app, "distance", source_type)
    contract = provenance(app, "centroid_distance_v1", {"lookup": lookup_path})
    contract["source_type"] = source_type
    contract["distance_model"] = app.raw_config.get("distance_weight", {})
    if not overwrite and cache_matches(path, contract):
        return path
    cfg = load_distance_weight_config(app.raw_config)
    distances = lookup["distance_km"]
    if distances.null_count() or not distances.is_finite().all() or (distances < 0).any():
        raise ValueError("Distance must be finite, nonnegative, and observed")
    values = distance_weight_values(
        distances.to_numpy(),
        cfg,
        max_distance_km=cfg.hard_cutoff_km or app.viewshed.max_distance_m / 1000,
    )
    frame = lookup.select(*PAIR_KEYS, "distance_km").with_columns(
        (pl.col("distance_km") * 1000).alias("distance_m"),
        pl.Series("weight_distance", values),
        pl.lit(cfg.selected_model).alias("distance_model"),
    )
    return write_component(frame, path, contract, weights=("weight_distance",))


def _surface_component(
    app: AppConfig, surface: str, *, source_type: str, overwrite: bool
) -> tuple[pl.DataFrame, dict[str, Any]]:
    from .canopy_visibility import make_terrain_variant_app
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
    variant = make_terrain_variant_app(
        app,
        surface_model=surface,
        terrain_dir=app.paths.output_dir / "components" / source_type / surface,
    )
    variant = apply_source_type_policy(variant, source_type)
    variant = replace(
        variant, run=replace(variant.run, overwrite=overwrite, combine_final_parquet=False)
    )
    completed = run_source_cells(initialize_app_config(variant))
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
        / f"source={source_type}"
        / f"source_h3_cell={source}.parquet"
        for source in sorted(expected_sources)
    ]
    sparse = pl.read_parquet(paths)
    validate_pairs(sparse, ("weight_terrain",))
    if sparse.join(lookup.select(PAIR_KEYS), on=PAIR_KEYS, how="anti").height:
        # Existing runners may retain targets outside the candidate cutoff; prune
        # explicitly after validating uniqueness, preserving the authoritative universe.
        sparse = sparse.join(lookup.select(PAIR_KEYS), on=PAIR_KEYS, how="semi")
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
    return dense.drop("__observed"), contract


def _surface_contract(app: AppConfig, source_type: str, canopy: bool = False) -> dict[str, Any]:
    lookup_path, _ = _lookup(app, source_type)
    inputs = {
        "lookup": lookup_path,
        "land": app.paths.land_polygon_path,
        "water": app.paths.water_polygon_path,
    }
    if source_type == "land":
        inputs.update(dem=app.paths.regional_dem_path, sources=app.paths.land_h3_path)
        if canopy:
            inputs.update(chm=app.paths.canopy_height_path, dem_weights=component_path(app, "dem"))
    contract = provenance(app, f"{'chm' if canopy else 'dem'}_component_v1", inputs)
    contract["source_type"] = source_type
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
        validate_pairs(bare, ("weight_terrain",))
        frame = bare.join(
            canopy.select(*PAIR_KEYS, pl.col("weight_terrain").alias("canopy_los_raw")),
            on=PAIR_KEYS,
            validate="1:1",
        )
        if frame.height != bare.height:
            raise ValueError("Canopy and DEM pair universes differ")
        frame = frame.with_columns(
            pl.when(pl.col("weight_terrain") > 0)
            .then(pl.min_horizontal("canopy_los_raw", "weight_terrain") / pl.col("weight_terrain"))
            .otherwise(1.0)
            .alias("weight_vegetation"),
            pl.when(pl.col("weight_terrain") > 0)
            .then(pl.lit("computed"))
            .otherwise(pl.lit("terrain_blocked_neutral"))
            .alias("canopy_support"),
        ).drop("weight_terrain")
    return write_component(frame, path, contract, weights=("weight_vegetation",))
