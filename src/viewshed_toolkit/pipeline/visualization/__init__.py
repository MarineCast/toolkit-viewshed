"""Visualization API for viewshed data, static assets, and interactive maps."""

from .data import (
    SelectedLocation,
    StaticMapOutputPaths,
    StaticMapSettings,
    ViewshedMapConfig,
    aggregate_target_weight_sums,
    select_source_for_viewshed_map,
    static_map_output_paths,
    static_map_settings,
)
from .exports import (
    SourceTypeStaticMapExportResult,
    StaticMapExportResult,
    ViewshedMapExportResult,
    export_source_type_static_weight_map,
    export_static_weight_maps,
    export_viewshed_weight_maps,
)
from .static_maps import (
    generalize_viewshed_support_geometry,
    quantile_visibility_classes,
)
from .styling import VISIBILITY_HEAT_COLORS, add_click_to_copy_coordinates

__all__ = [
    "SelectedLocation",
    "SourceTypeStaticMapExportResult",
    "StaticMapExportResult",
    "StaticMapOutputPaths",
    "StaticMapSettings",
    "VISIBILITY_HEAT_COLORS",
    "ViewshedMapConfig",
    "ViewshedMapExportResult",
    "add_click_to_copy_coordinates",
    "aggregate_target_weight_sums",
    "export_source_type_static_weight_map",
    "export_static_weight_maps",
    "export_viewshed_weight_maps",
    "generalize_viewshed_support_geometry",
    "quantile_visibility_classes",
    "select_source_for_viewshed_map",
    "static_map_output_paths",
    "static_map_settings",
]
