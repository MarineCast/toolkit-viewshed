---
name: regional-run
description: Prepare and execute bounded viewshed acquisition, regional workflows, notebooks, or case-study runs with explicit data and output requirements.
---

# regional-run

All paths and commands below are relative to the toolkit-viewshed checkout root.
Source/tests, explicit contracts and architecture remain authoritative.

## Environment and installation

Use an isolated environment with Python 3.11 or newer. GDAL and Rasterio must be binary-compatible
for real geospatial runs.

```bash
python -m pip install -e ".[dev,analysis]"
```

Install `.[acquisition]` only when source-download work needs it. Do not assume that a checkout has
the regional DEM, canopy raster, land/water polygons, H3 support products, or OrcaCast datasets.

Relative data and output paths resolve from the project root. Use `VIEWSHED_TOOLKIT_ROOT` when a
different root is intentional. Do not encode workstation-specific absolute paths in source,
configuration, metadata, notebooks, or documentation.

For regional case-study work, use `analysis/salish_sea/`, `docs/salish-sea-case-study.md`, and the
canonical configuration. Historical OrcaCast locations are recorded only in
`docs/reports/orcacast-history.md`; do not recreate the removed subtree or substitute the current
Salish Sea study for it.

Before running, read `docs/api.md` and `docs/pipelines.md` for workflow effects. Confirm GDAL,
source inputs, configured data/output locations, bounded geography, disk/compute needs, and
cleanup/promotion behavior. Use a dedicated output directory; do not run acquisition or a
regional workflow merely to smoke-test an unrelated change. No datasets are implied by a clone.
Report exact commands, missing inputs, skipped integrations, and inspected artifacts. Do not
claim regional success from unit tests. Preserve historical report dates and provenance.

