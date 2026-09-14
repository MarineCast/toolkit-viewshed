# Salish Sea case-study snapshot

The migrated OrcaCast snapshot contains land and water H3 resolution 7 static-viewability
kernels plus generated map-support artifacts. The validation report records 2,081,219 unique
land pairs and 3,208,581 unique water pairs. Both kernels have unique, non-null pair keys,
finite weights in `[0, 1]`, and checksums matching their metadata sidecars.

This is a preservation result, not a rebuild result. The raw DEM, canopy, land-mask, and water
domain inputs needed for a regional rerun are not included. The reporting-opportunity notebook
also references population, access, calendar, weather, daylight, sightings, and routing inputs
that remain OrcaCast-specific.

See `viewshed_migration_validation.json` for machine-readable evidence and
`../../analysis/case_studies/orcacast/manifests/artifacts.json` for the portable file inventory.
