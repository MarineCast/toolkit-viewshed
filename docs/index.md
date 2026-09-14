# Documentation

Viewshed Toolkit exposes a compact supported API over a staged geospatial pipeline. Start with
the repository `README.md`, then use:

- `api.md` for supported imports and entry points.
- `methodology.md` for the scientific model and interpretation limits.
- `../analysis/case_studies/orcacast/README.md` for the migrated Salish Sea example and its data needs.
- `../MIGRATION.md` for source provenance and validation boundaries.
- `reports/repository-organization.md` for the current structure review and deferred work.

Implementation modules are grouped by phase under `viewshed_toolkit.pipeline`: `prepare`,
`weights`, `finalize`, and `visualization`. Shared configuration, persistence, geometry, and
raster primitives live under the private `viewshed_toolkit._internal` namespace.
