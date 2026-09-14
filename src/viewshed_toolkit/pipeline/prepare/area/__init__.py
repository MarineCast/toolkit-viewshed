"""Area-domain preparation, source sampling, lookup generation, and batching."""

from .batching import iter_source_batches, make_batch_id
from .config import AreaLookupResult, SourceTargetLookupConfig, SourceUniverse
from .context import prepare_batch_context
from .geometry import (
    ensure_h3_geometry_artifact,
    h3_geometry_artifact_path,
    load_h3_geometry_frame,
    load_h3_geometry_lookup,
)
from .inputs import load_source_cells, source_cells_input_path, validate_viewshed_inputs
from .lookup import build_source_target_lookup
from .observers import build_observers_from_sample_points
from .sampling import (
    calculate_source_sample_count,
    h3_cell_for_latlon,
    prepare_source_samples,
    sample_points_in_h3_cell_id,
    sample_points_in_source_geometry,
    source_cell_polygon,
    source_sampling_diagnostics_path,
    summarize_source_sampling_diagnostics,
    write_source_sampling_diagnostics,
)

__all__ = [
    "build_observers_from_sample_points",
    "make_batch_id",
    "source_cells_input_path",
    "AreaLookupResult",
    "SourceTargetLookupConfig",
    "SourceUniverse",
    "build_source_target_lookup",
    "ensure_h3_geometry_artifact",
    "h3_geometry_artifact_path",
    "calculate_source_sample_count",
    "h3_cell_for_latlon",
    "iter_source_batches",
    "load_source_cells",
    "load_h3_geometry_frame",
    "load_h3_geometry_lookup",
    "prepare_batch_context",
    "prepare_source_samples",
    "sample_points_in_h3_cell_id",
    "sample_points_in_source_geometry",
    "source_cell_polygon",
    "source_sampling_diagnostics_path",
    "summarize_source_sampling_diagnostics",
    "validate_viewshed_inputs",
    "write_source_sampling_diagnostics",
]
