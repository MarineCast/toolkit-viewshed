# Documentation

Viewshed Toolkit exposes a small supported API over a staged geospatial pipeline.

- [Project overview](../README.md): installation, repository layout, case-study scope, and checks.
- [Supported API](api.md): stable imports, stage-level imports, and execution behavior.
- [Methodology](methodology.md): scientific model, artifact grain, and interpretation limits.
- [Salish Sea case study](salish-sea-case-study.md): regional acquisition, components, and report.
- [Historical OrcaCast snapshot](reports/case-study-salish-sea.md): preserved copied-kernel evidence.
- [Historical OrcaCast resources](reports/orcacast-history.md): pinned source tree and artifact
  restoration instructions.
- [Historical validation status](reports/validation.md): migration-era checks and their limits.
- [Historical repository organization](reports/repository-organization.md): migration-era source
  boundaries and deferred maintainability work.

Implementation modules are grouped by phase under `viewshed_toolkit.pipeline`: `prepare`,
`weights`, `finalize`, and `visualization`, with `api`, `cli`, `config`, `contracts`, and
`diagnostics` supporting those phases. Shared persistence, geometry, raster, and configuration
primitives live under the private `viewshed_toolkit._internal` namespace.

## Explicit component workflow

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Acquisition](data-acquisition.md)
- [Pipelines](pipelines.md)
- [Scientific methodology](scientific-methodology.md)
- [Distance products](distance-products.md): reusable pair distances and attenuation profiles.
- [Performance](performance.md)
- [Salish Sea case study](salish-sea-case-study.md)
- [Refactor validation](reports/productionization.md)
