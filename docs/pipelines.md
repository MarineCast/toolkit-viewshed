# Pipelines

Run exactly one operation with `viewshed-toolkit stage <name> --config <file>`. Inspect all stages
with `viewshed-toolkit stage --help`. The dependency graph is owned by `api/registry.py`.

## Workflow contracts

| Contract | Established production workflow | Explicit component workflow |
|---|---|---|
| Main API | `run_viewshed` / `process` | `run_components` / `run_component_stage` |
| Durable outputs | Existing compact final-output contract | Separate durable component namespace |
| Automatic cleanup | Validated scratch cleanup; reusable caches retained by default | No implicit cleanup |
| Land/water promotion | Rollback-safe paired promotion | Per-role manifests; no combined atomic generation pointer |

These lifecycle guarantees are not interchangeable. The established workflow owns its configured
cleanup and paired-promotion behavior; component stages must not invoke or emulate it. See
[cache retention](configuration.md#cache-retention-and-cleanup) for the default `retain_reusable`
policy and the explicit `compact` alternative. Both protect configured sources.

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

`build dem|chm|distance|static|all` resolves only the requested dependency closure. `static`
finishes and validates the role's final table without maps; `all` also exports factor maps.
Source role is explicit (`--source-type land|water`). Map geometry/data are embedded; rendering
still uses Folium's external Leaflet/CDN JavaScript. Maps provide no current weather or observer
activity interpretation.

Water plans use mapped land as an opaque blocker and omit DEM/CHM acquisition and preparation.
The water CHM component records its explicit N/A factor. A land build that needs both surface
components uses shared batch preparation for paired DEM/CHM execution, then persists each
component separately. If only one component is stale, the current component can be reused.
Independent `stage` commands retain their original scope; explicitly requesting a raster
preparation stage still performs that operation. `build distance` is raster-independent for both
source roles. Factor-map export aggregates all requested factors in one table scan and shares
target polygons across the maps.

`build ... --dry-run` calls the public `plan_components` API without running stages, downloading,
or writing outputs. It reports the same dependency closure, prerequisites, capabilities, run-ID
conflicts, local resource availability, and existing table-header workload counts. Configured
shared-study validation still runs during configuration loading. No additional content hashes or
raster/geometry row scans are performed. Cache presence remains unverified, and readable metadata
cannot prove valid-data coverage, provider availability/freshness, or a regional ETA. A non-ready
plan exits with status 1; see the [API report contract](api.md#explicit-component-api).

`build-distance-weights` is a compatibility stage: it applies the integrated model's existing
default profile to `build-pair-distances` and adapts that product to the established component
schema. Separate profiles use `build_distance_profile` or `build-distance-profile` and are not
dependencies of static composition. They require only the persisted pair product and sidecar.
See [distance products](distance-products.md).

## Caching and failures

Durable component products require a matching contract and full output checksum before reuse.
Input digest reuse is limited to unchanged filesystem identity and timestamps; changed files are
reread. Terrain component contracts include the underlying scientific algorithm and partition
identities, including lookup content. A changed source invalidates dependent composition.
Complete sparse LOS execution is checked before absent rows can mean observed zero; duplicate
keys and invalid observed values are errors. Legacy producers have their own cache behavior;
starting a new orchestration run alone does not rebuild their cached products.

Legacy `process` resume checks current local input fingerprints, maintained scientific algorithm
versions, and complete manifest/output contracts before skipping work. An older manifest without
current fingerprints, changed input fingerprints, invalid outputs, or an incomplete manifest fails
closed before execution. Rebuild affected stages explicitly with
`run_stage(..., StageInvocation(..., overwrite=True))`, then start a **new
run ID**. `force=True` never bypasses these checks or requests scientific stage-cache rebuilding.
Complete workflows record fingerprints after their intentional cleanup; resuming does not require
recreating intentionally removed intermediates. These checks do not query remote-provider
freshness. Algorithm identity follows maintained producer version constants; numerical changes
must update those versions because orchestration does not automatically hash source code.

Paired production checks completion records and current partition metadata for both land
surfaces before composition. It materializes dense bare-earth clear-sky diagnostics over the
candidate lookup, then stages both compact static tables and both observation-geometry tables
before promoting the four-file set. A staging or promotion failure preserves the previous set.
This is rollback safety within one writer, not an atomic generation pointer for concurrent readers.
Water partition identity and in-process geometry caches include land/water geometry and lookup
content, so changes at the same path invalidate reuse.

Component run manifests are per source type and run ID. Reusing an ID with a different configuration
or plan fails before stages execute unless `--overwrite` is explicit. An interrupted run records
completed outputs and a failed stage; it does not claim completion. Concurrent writers to the
same logical run are not supported; use separate run/output directories.

Distance caches are dependency-specific. Raster configuration changes do not invalidate raw pair
distances or standalone profiles. Geometry, H3 resolution, candidate coverage, lookup content,
distance method, schema, or algorithm changes invalidate pair distances and therefore profiles.
A profile change creates a distinct content-addressed weighted product while preserving the raw
pair universe and existing profiles.

The legacy distance partition path also binds the active model/cutoff, source role, lookup content,
and exact selected source IDs. A complete manifest selects the partitions to combine; directory
globbing is not a recovery mechanism. Combination and source/target aggregation validate partition
metadata and output checksums and reject overlapping source selections. Partition and manifest
writes are atomic, and a failed new partition build preserves the last complete manifest. Retained
partitions from other models or
source slices are not silently included. Rebuild the desired selection after an interrupted run;
do not infer completeness from files left in the partition directory.

## Cleanup

Explicit component builds do not automatically delete work files. Their raw caches, weight tables,
final tables, and manifests live in the separate durable component namespace. Keep source and
work inputs until required validation/reproduction is complete. Established workflow cleanup now
retains reusable caches by default; rollback-safe land/water promotion is preserved. Do not run cleanup against a
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
execution or cleanup. A different configuration or stage selection requires a new run ID.
A same-identity invocation can refresh its manifest after normal cache-aware execution.

Complete-run resume validates the durable generation receipt, typed products, metadata and
currently available input identity before accepting the stored output checksums. Permitted
input pruning remains supported by the retained receipt. Invalid generation evidence fails
without running stages or replacing the manifest. A completed subset of stages can resume
after verifying its current input fingerprints and expected output checksums. An interrupted
or failed invocation requires explicit recovery.

Partial-run manifests use schema version 3 to record canonical local input checksums, including
missing inputs and shapefile sidecars. Partial resume rejects changed/missing input identity and
historical manifests without that inventory before entering stage checks. It does not silently
certify older cached outputs. Rebuild affected producers with their explicit overwrite controls
or a fresh workspace, then create a new manifest. Remote source freshness is not certified by
local checksum validation.

## Explicit direct-transmission workflow

`viewshed-toolkit transmission --config request.json --stage prepare|score|run|validate` selects
the separate versioned scenario workflow registered in `TRANSMISSION_STAGES`. It neither acquires
inputs nor overwrites/promotes legacy outputs. Completion requires matched input/configuration/code
identity and validated source partitions. Failed generations remain incomplete with evidence
preserved. See [direct transmission](direct-transmission.md) for schema and lifecycle details.

### Persistent explicit-transmission tiles

The opt-in `transmission-tiles` workflow plans provisioned inputs, preprocesses each required canonical
parent once into immutable compressed storage tiles, then reads those tiles for weights. It has no
acquisition dependency or implicit cleanup. A completion ledger rejects unwritten/deleted chunks;
explicit resume validates receipts, and explicit recovery refuses live owners or committed corruption.
The [persistent workflow](direct-transmission.md#persistent-xarray-tile-preparation-and-scoring) retains
full-source observer support, source-relative sampling, original ray coordinates, conservative terrain
support and numerical census gates. Final source evidence and one prepared store remain local.

An explicit `shared_study.config_path` profile selects native R7 reporting targets independently
from `land_source` and `water_source` observer memberships. Mixed source cells can appear in both
roles. The owner-declared source extent and physical context are verified before preparation;
source raster paths still need full coverage across the configured LOS/AOI margin. The historical
standalone bbox policy applies when no shared study is selected. Shared selection changes cache
identity and does not change cleanup or promotion scope.
