# Salish Sea case-study snapshot

The migrated OrcaCast snapshot contains land and water H3 resolution 7 static-viewability
kernels. The validation snapshot records 2,081,219 unique land pairs and 3,208,581 unique water
pairs. Both kernels have unique, non-null pair keys, finite weights in `[0, 1]`, and checksums
matching their metadata sidecars.

This is a preservation result, not a rebuild result. The raw DEM, canopy, land-mask, and water
domain inputs needed for a regional rerun are not included. The reporting-opportunity notebook
also references population, access, calendar, weather, daylight, sightings, and routing inputs
that remain OrcaCast-specific.

The portable manifest also describes 52 generated map-support files under
`outputs/effort/viewshed`, but the checked-in validation snapshot found none at that canonical
location. A local copy under `data/outputs/effort/viewshed` does not satisfy the manifest paths.
Consequently the snapshot's overall `valid` value is false even though both primary kernels pass
their recorded checks.

See [`viewshed_migration_validation.json`](viewshed_migration_validation.json) for machine-readable
evidence and the
[portable artifact inventory](../../analysis/case_studies/orcacast/manifests/artifacts.json) for
the expected paths, byte sizes, and SHA-256 values.
