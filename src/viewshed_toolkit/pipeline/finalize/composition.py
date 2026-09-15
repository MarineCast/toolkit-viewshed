"""Explicit, strict composition of separately persisted static components."""

from pathlib import Path

import polars as pl

from ..config import AppConfig
from ..config.datasets import CompositionConfig
from ..contracts.components import (
    PAIR_KEYS,
    cache_matches,
    component_path,
    provenance,
    validate_pairs,
    write_component,
)


def validate_composed(app: AppConfig, source_type: str) -> Path:
    """Validate lineage and contents without running a producer."""
    from ..weights.components import _distance_component_contract, _lookup, _surface_contract

    paths = {name: component_path(app, name, source_type) for name in ("dem", "chm", "distance")}
    _lookup(app, source_type)
    distance_contract = _distance_component_contract(app, source_type)
    for name, expected in (
        ("dem", _surface_contract(app, source_type)),
        ("chm", _surface_contract(app, source_type, canopy=True)),
        ("distance", distance_contract),
    ):
        if not cache_matches(paths[name], expected):
            raise ValueError(f"Stale or corrupt {name} component")
    path = component_path(app, "static", source_type)
    contract = provenance(app, "distance_integrated_static_v1", paths)
    contract["source_type"] = source_type
    contract["formula"] = "weight_terrain * weight_vegetation"
    contract["distance_role"] = "centroid_diagnostic_only; attenuation_already_integrated_in_LOS"
    if not cache_matches(path, contract):
        raise ValueError("Stale or corrupt composed static artifact")
    frame = pl.read_parquet(path)
    validate_pairs(
        frame,
        ("weight_terrain", "weight_vegetation", "weight_distance", "weight_static_viewability"),
    )
    if (
        not (
            frame["weight_static_viewability"]
            - frame["weight_terrain"] * frame["weight_vegetation"]
        )
        .abs()
        .le(1e-7)
        .all()
    ):
        raise ValueError("Static component formula does not agree")
    return path


def compose_components(
    app: AppConfig, *, source_type: str = "land", overwrite: bool = False
) -> Path:
    CompositionConfig.model_validate(app.raw_config.get("composition", {}))
    from ..weights.components import _distance_component_contract, _lookup, _surface_contract

    paths = {name: component_path(app, name, source_type) for name in ("dem", "chm", "distance")}
    _, lookup = _lookup(app, source_type)
    distance_contract = _distance_component_contract(app, source_type)
    for name, expected in (
        ("dem", _surface_contract(app, source_type)),
        ("chm", _surface_contract(app, source_type, canopy=True)),
        ("distance", distance_contract),
    ):
        if not cache_matches(paths[name], expected):
            raise ValueError(
                f"Missing, corrupt, or stale {name} component; rebuild it before composition"
            )
    output = component_path(app, "static", source_type)
    contract = provenance(app, "distance_integrated_static_v1", paths)
    contract["source_type"] = source_type
    contract["formula"] = "weight_terrain * weight_vegetation"
    contract["distance_role"] = "centroid_diagnostic_only; attenuation_already_integrated_in_LOS"
    if not overwrite and cache_matches(output, contract):
        return output
    # Release each input after its join rather than retaining all three regional
    # tables alongside the growing composed result.
    result = pl.read_parquet(paths["dem"])
    validate_pairs(result, ("weight_terrain",))
    if (
        result.select(PAIR_KEYS).join(lookup.select(PAIR_KEYS), on=PAIR_KEYS, how="anti").height
        or lookup.select(PAIR_KEYS).join(result.select(PAIR_KEYS), on=PAIR_KEYS, how="anti").height
    ):
        raise ValueError("DEM component does not cover the current lookup")
    del lookup
    for name, column in (("chm", "weight_vegetation"), ("distance", "weight_distance")):
        factor = pl.read_parquet(paths[name])
        validate_pairs(factor, (column,))
        if (
            result.select(PAIR_KEYS).join(factor.select(PAIR_KEYS), on=PAIR_KEYS, how="anti").height
            or factor.select(PAIR_KEYS)
            .join(result.select(PAIR_KEYS), on=PAIR_KEYS, how="anti")
            .height
        ):
            raise ValueError(f"{name} component does not cover exactly the DEM pair universe")
        result = result.join(factor, on=PAIR_KEYS, validate="1:1")
        del factor
    result = result.with_columns(
        (pl.col("weight_terrain") * pl.col("weight_vegetation")).alias("weight_static_viewability")
    )
    return write_component(result, output, contract, weights=("weight_static_viewability",))
