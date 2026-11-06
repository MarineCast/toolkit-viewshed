"""Compatibility entry point; new runs require the explicit supported v2 JSON configuration.

Use viewshed-toolkit transmission --config CONFIG.json --stage prepare|score|run|validate.
Old experiment outputs remain untouched and are not accepted as v2 prepared generations.
"""

import sys

from viewshed_toolkit.pipeline.api.transmission import run_transmission
from viewshed_toolkit.pipeline.contracts.transmission import RADIUS
from viewshed_toolkit.pipeline.finalize.transmission import (
    validate_legacy_pair_rows as validate_pairs,
)
from viewshed_toolkit.pipeline.prepare.transmission import land_sources as land_sources
from viewshed_toolkit.pipeline.prepare.transmission import load_cache as load_cache
from viewshed_toolkit.pipeline.prepare.transmission import observer_design as observer_design

# The historical validator supports only the original compact row shape.
from viewshed_toolkit.pipeline.prepare.transmission import (
    source_window as _source_window,
)


def source_window(arrays, grid, geometry):
    return _source_window(arrays, grid, geometry, radius=RADIUS)


if __name__ == "__main__":
    run_transmission(sys.argv[1], stage=sys.argv[2] if len(sys.argv) > 2 else "run")

__all__ = ["land_sources", "load_cache", "observer_design", "source_window", "validate_pairs"]
