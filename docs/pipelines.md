# Pipelines

Run exactly one operation with `viewshed-toolkit stage <name> --config <file>`. Inspect all stages
with `viewshed-toolkit stage --help`. The dependency graph is owned by `api/registry.py`.

| Stage | Required upstream inputs / effect |
|---|---|
| resolve-area | Validate provider/composition settings; record configured AOI |
| download-dem / download-chm | Retrieve raw assets only |
| prepare-dem / prepare-chm | Consume download manifest; prepare one raster independently |
| build-source-cells | Build configured land source cells |
| build-target-cells | Build inspectable water/mixed target geometry |
| build-source-target-lookup | Existing source-role and target-policy pair builder |
| build-dem-weights | Land: DEM, geometry, lookup; water: land-mask geometry and lookup |
| build-chm-weights | Land: DEM component, CHM, same observer design; water: explicit N/A factor |
| build-distance-weights | Pair distances only; no raster dependency |
| compose-static-weights | Validate current component lineage and exact pair coverage, then compose |
| finalize | Persist one source role's durable final component table |
| validate | Check current components, formula, and final output checksum |
| export-maps | Static mean support by target over candidate source cells |

`build dem|chm|distance|all` resolves only the requested dependency closure. `all` includes maps;
source role is explicit (`--source-type land|water`). Map geometry/data are embedded; rendering
still uses Folium's external Leaflet/CDN JavaScript. Maps provide no current weather or observer
activity interpretation.

## Caching and failures

No stage claims a cache hit from file existence alone. New products require matching contract and
output checksum; old underlying stages retain their existing metadata/grid checks. Invalid legacy
caches may require `--overwrite` or a fresh work directory. A changed source invalidates dependent
component composition. Complete sparse LOS execution is checked before absent rows can mean
observed zero; duplicate keys and invalid observed values are errors.

Run manifests are per source type and run ID. Reusing an ID with a different configuration or
plan fails before stages execute unless `--overwrite` is explicit. An interrupted run records
completed outputs and a failed stage; it does not claim completion. Concurrent writers to the
same logical run are not supported; use separate run/output directories.

## Cleanup

Explicit component builds do not automatically delete work files. Their raw caches, weight tables,
final tables, and manifests live in the separate durable component namespace. Keep source and
work inputs until required validation/reproduction is complete. Existing legacy cleanup tests and
rollback-safe legacy land/water promotion remain unchanged. Do not run legacy cleanup against a
custom root that also contains the new durable namespace.
