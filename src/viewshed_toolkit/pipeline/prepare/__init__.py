"""Input preparation for the human viewshed pipeline.

Preparation is grouped by responsibility:

- :mod:`area` builds spatial domains, samples, batches, and pair lookups.
- :mod:`elevation` acquires DEMs and builds terrain/canopy surfaces.
- :mod:`vegetation` acquires and aligns canopy and land-cover assets.
"""

__all__ = ["area", "elevation", "vegetation"]
