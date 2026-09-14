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

To restore the ignored artifact bundle from an OrcaCast checkout with matching paths:

```bash
python scripts/sync_case_study_artifacts.py --source-root /path/to/OrcaCast
```

The copied kernels were validated against the migrated configuration, but they were not rebuilt
inside this repository. See `docs/reports/viewshed_migration_validation.json` and the historical
audit under `history/` for the precise validation and provenance boundaries.
