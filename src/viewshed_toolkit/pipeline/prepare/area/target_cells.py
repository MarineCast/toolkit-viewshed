"""Inspectable water-target domain, using the existing H3 classification policy."""

import uuid
from pathlib import Path

from ...config import AppConfig
from ...config.distance import load_distance_runtime, load_distance_weight_config
from ...contracts.components import cache_matches, component_root, provenance, record_product
from . import domains
from .config import _lookup_config
from .universe import _classify_bbox_cells, _physical_cell_type_frame


def build_target_cells(app: AppConfig, *, overwrite: bool = False) -> Path:
    output = component_root(app) / "geometry" / "target_cells.parquet"
    contract = provenance(
        app,
        "target_cells_existing_policy_v1",
        {
            "land": app.paths.land_polygon_path,
            "water": app.paths.water_polygon_path,
        },
    )
    if not overwrite and cache_matches(output, contract):
        return output
    runtime = load_distance_runtime(app.config_path)
    settings = _lookup_config(app.raw_config, runtime, load_distance_weight_config(app.raw_config))
    polygon = domains.target_domain_polygon(runtime)
    candidates = domains.bbox_h3_cells(
        tuple(polygon.bounds),
        runtime.source_resolution,
        buffer_rings=settings.bbox_buffer_rings,
        strict_intersection=False,
    )
    classified = _classify_bbox_cells(runtime, settings, cells=candidates, extent="target")
    roles = _physical_cell_type_frame(classified, settings)
    allowed = []
    if settings.include_water_targets:
        allowed.append("water")
    if settings.include_mixed_as_water_targets:
        allowed.append("mixed")
    cells = roles.loc[roles["cell_type"].isin(allowed), "h3_cell"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        classified[classified["h3_cell"].isin(cells)].sort_values("h3_cell").to_parquet(temporary)
        temporary.replace(output)
        record_product(output, contract)
    finally:
        temporary.unlink(missing_ok=True)
    return output
