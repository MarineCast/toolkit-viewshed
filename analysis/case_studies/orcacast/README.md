# OrcaCast case study

This directory preserves the application-specific material migrated from OrcaCast. It is not
part of the reusable package API.

- `notebooks/01_STATIC_VIEWABILITY.ipynb` inspects retrospective land and water viewability.
- `notebooks/02_LAND_REPORTING_OPPORTUNITY.ipynb` explores later observer-access, weather,
  reporting, and sightings layers. It requires several OrcaCast datasets that were not copied.
- `manifests/artifacts.json` inventories every ignored kernel and map artifact by relative path,
  byte size, and SHA-256.
- `observation_contracts.py` preserves OrcaCast's broader schema-v3 research contract; it is
  intentionally excluded from the installable toolkit package.
- `history/` contains dated source-repository documentation. Treat it as historical evidence,
  not current toolkit documentation.

To restore the ignored artifact bundle, copy each file from a matching source bundle to the exact
repo-relative `path` in `manifests/artifacts.json`, then verify its recorded byte count and SHA-256.
There is no artifact-sync helper in the current checkout.

The copied kernels were validated against the migrated configuration, but they were not rebuilt
inside this repository. The checked-in
[`viewshed_migration_validation.json`](../../../docs/reports/viewshed_migration_validation.json)
is a historical validation snapshot whose top-level result is currently false because the expected
map-output paths were not present. See the [validation status](../../../docs/reports/validation.md)
and the dated audit under `history/` for the precise boundaries.
