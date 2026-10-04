# Pipelines

Run exactly one operation with `viewshed-toolkit stage <name> --config <file>`. Inspect all stages
with `viewshed-toolkit stage --help`. The dependency graph is owned by `api/registry.py`.

## Workflow contracts

| Contract | Established production workflow | Explicit component workflow |
|---|---|---|
| Main API | `run_viewshed` / `process` | `run_components` / `run_component_stage` |
| Durable outputs | Existing compact final-output contract | Separate durable component namespace |
| Automatic cleanup | After successful complete execution | No implicit cleanup |
| Land/water promotion | Rollback-safe paired promotion | Per-role manifests; no combined atomic generation pointer |

These lifecycle guarantees are not interchangeable. The established workflow owns its compact
cleanup and paired-promotion behavior; component stages must not invoke or emulate it.

## Explicit component stages

| Stage | Required upstream inputs / effect |
|---|---|
| resolve-area | Validate provider/composition settings; record configured AOI |
| download-dem / download-chm | Retrieve raw assets only |
| prepare-dem / prepare-chm | Consume download manifest; prepare one raster independently |
| build-source-cells | Build configured land source cells |
| build-target-cells | Build inspectable water/mixed target geometry |
| build-source-target-lookup | Existing source-role and target-policy pair builder |
| build-pair-distances | Promote validated lookup distances to a durable role-specific raw product; no rasters |
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

For water, `build dem|chm|all --source-type water` resolves geometry and pair dependencies,
but omits DEM/CHM download and preparation: mapped land is the opaque blocker and canopy is
explicitly not applicable. Explicit `stage download-dem` or `stage download-chm` still does the
requested acquisition. Land builds retain their raster dependencies. The `build distance` target
remains raster-independent for both roles.

`build-distance-weights` is a compatibility stage: it applies the integrated model's existing
default profile to `build-pair-distances` and adapts that product to the established component
schema. Separate profiles use `build_distance_profile` or `build-distance-profile` and are not
dependencies of static composition. They require only the persisted pair product and sidecar.
See [distance products](distance-products.md).

## Caching and failures

No stage claims a cache hit from file existence alone. New products require matching contract and
output checksum; old underlying stages retain their existing metadata/grid checks. Invalid legacy
caches may require `--overwrite` or a fresh work directory. A changed source invalidates dependent
component composition. Complete sparse LOS execution is checked before absent rows can mean
observed zero; duplicate keys and invalid observed values are errors.

Paired production checks completion records and current partition metadata for both land
surfaces before composition. It materializes dense bare-earth clear-sky diagnostics over the
candidate lookup, then stages both compact static tables and both observation-geometry tables
before promoting the four-file set. A staging or promotion failure preserves the previous set.
This is rollback safety within one writer, not an atomic generation pointer for concurrent readers.
Water partition identity and in-process geometry caches include land/water geometry and lookup
content, so changes at the same path invalidate reuse.

Component run manifests are per source type and run ID. Reusing an ID with a different configuration or
plan fails before stages execute unless `--overwrite` is explicit. An interrupted run records
completed outputs and a failed stage; it does not claim completion. Concurrent writers to the
same logical run are not supported; use separate run/output directories.

Distance caches are dependency-specific. Raster configuration changes do not invalidate raw pair
distances or standalone profiles. Geometry, H3 resolution, candidate coverage, lookup content,
distance method, schema, or algorithm changes invalidate pair distances and therefore profiles.
A profile change creates a distinct content-addressed weighted product while preserving the raw
pair universe and existing profiles.

## Cleanup

Explicit component builds do not automatically delete work files. Their raw caches, weight tables,
final tables, and manifests live in the separate durable component namespace. Keep source and
work inputs until required validation/reproduction is complete. Existing legacy cleanup tests and
rollback-safe legacy land/water promotion remain unchanged. Do not run legacy cleanup against a
custom root that also contains the new durable namespace.


## Producer lineage and replacement

Working terrain, clear-sky, centroid distance, dual-surface canopy and neutral water vegetation
products carry `viewshed_factor_producer_v1` receipts beside their Parquet files. Successful
producer stages write them after product validation. Their dependency scopes retain effective
scientific settings, role, lookup content identity, prepared surfaces and grid/sampling/denominator
contracts. File identities use the repository checksum (filename plus bytes); independent byte
SHA-256 values are explicitly recorded for exported surface identity.

Finalization validates all required producers before staging, including with `overwrite=True`.
Overwrite replaces durable outputs; it cannot certify that upstream computations ran. Missing or
stale lineage names the artifact and required rebuild stage. Historical products without these
receipts require a genuine producer rebuild, rather than receipt backfilling. Pure lazy joins are
calculation helpers; publication entry points enforce lineage. The directly observed LOS method
and `weight_terrain * weight_vegetation` formula are unchanged.

Paired durable receipts use `viewshed_output_set_v3` and retain all producer evidence. Single-role
publication stages its compact table, geometry and `viewshed_single_role_v1` receipt together.
A promotion error restores previous files and sidecars; this does not promise a concurrent-reader
snapshot. Valid retained durable receipts support read-only reuse after permitted intermediate
cleanup. New finalization requires working producer evidence. A pruned generation must rebuild
those stages before replacement.

Terrain workers consume one expected-metadata snapshot per stage. A new run recomputes content
identity; the stage checks fresh identity before combination/publication and rejects changed
inputs. This is a measured reduction in hash calls, not a measured regional speedup.

## Manifest-backed paired orchestration

`process(ViewshedRequest(...))` validates a simple run ID and checks manifest identity **before**
execution or cleanup. Reusing an ID with different configuration/stages requires `force=True`;
`resume=True` alone is not overwrite authorization. A same-identity invocation may refresh its
manifest after normal cache-aware execution.

Complete-run resume validates the durable generation receipt, typed products, metadata and
currently available input identity before accepting the stored output checksums. Permitted
input pruning remains supported by the retained receipt. Invalid generation evidence fails
without running stages or replacing the manifest. Partial runs have no equivalent complete
generation contract, so resume re-enters stage-specific checks instead of skipping from a
manifest checksum alone. No stage formulas or paired cleanup rules change.

Partial-run manifests use schema version 3 to record canonical local input checksums, including
missing inputs and shapefile sidecars. Partial resume rejects changed/missing input identity and
historical manifests without that inventory before entering stage checks. It does not silently
certify older cached outputs. Rebuild affected producers with their explicit overwrite controls
or a fresh workspace, then create a new manifest. Remote source freshness is not certified by
local checksum validation.
