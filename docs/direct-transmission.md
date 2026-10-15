# Direct-ray canopy transmission

The supported `worldcover_chm_intersection_transmission_v2` method brings the demonstrated
WorldCover/CHM transmission calculation into the installed toolkit. It is an explicit alternative
workflow, selected with `viewshed-toolkit transmission`, rather than a change to the existing
GDAL paired/component methods or their `weight_vegetation` fields. A completed software generation
is a static **uncalibrated scenario**, not empirical detection probability or a qualified regional
release. Existing demo/browser artifacts remain immutable v1 research evidence.

## Configuration and execution

Use a JSON configuration matching the packaged `transmission.schema.json`. Loading it is read-only.
Required fields are `schema_version: 2`, `method: worldcover_chm_intersection_transmission_v2`,
`output_dir`, `source_bbox_wgs84`, `inputs`, and `qualification`. Paths resolve relative to the JSON
file. Unknown fields, invalid versions, nonfinite values, booleans in integer fields, unpinned VRT
sources and changed inputs fail. A fresh output directory is required; there is no overwrite,
acquisition, download, automatic cleanup, production promotion or implicit input repair.

The `inputs` mapping supplies `{path, sha256}` records for `coastline` (EPSG:4326 GeoPackage with
`land_area`/`water_area` layers), `terrain_vrt`, `terrain_provenance`, `canopy_inventory`,
`worldcover_manifest`, `gba_subset` and `gba_manifest`. Add every file referenced by a terrain VRT
as another input record, including nested VRTs. SHA256 means raw file bytes. The canopy inventory
contains checksummed absolute `raw_canopy` paths; the WorldCover manifest contains checksummed
absolute `assets` paths and `bounds_wgs84`. The building acquisition must be `completed_validated`
and bind `subset_sha256` plus `study_bbox`. All source assets remain outside Git.

`qualification` explicitly records `vertical_units: m`, canopy/building height references
`above_ground`, terrain and water vertical references, and `source_epochs` and `source_licenses`
for each of `terrain`, `canopy`, `buildings`, `worldcover`, and `coastline`.
`empirical_status` must be `uncalibrated_scenario`. These are caller declarations, not certifications.
Different or unverified vertical references remain `datum_status: unqualified`. Known non-metre
raster units and undecoded raster scale/offset are rejected; absent native unit metadata does not
establish physical equivalence to the declared water zero. Retain source receipts with the inputs.

```bash
viewshed-toolkit transmission --config request.json --stage prepare
viewshed-toolkit transmission --config request.json --stage score
viewshed-toolkit transmission --config request.json --stage validate
# --stage run performs preparation and scoring for a fresh generation.
```

Python callers use `load_transmission_config`, `TransmissionConfig`, `run_transmission`, and
`validate_transmission_product` from the package root. The method fixes EPSG:32610, R7, 30 m
bilinear terrain nested under 10 m nearest obstruction/classes, 1.7 m eye height, 1 m water target,
0 m modeled water surface, curvature 0.85714, radius 6,378,137 m, beta 0.002/m and 30 m canopy-only
observer clearance. This recipe is recorded in `model_and_grid.json`. The current supported region
uses those coordinates; selecting another projected region requires a new qualified recipe.

Configurable limits/design fields include `max_distance_m` (default 24,140.16 m, at most 15 miles),
`source_samples` (default/max 15), `target_stride` (1 or 2), `census_threshold` (default 1,000),
`census_sources` (default 12), `workers` (one or two), `memory_budget_gib` (at most 6),
`max_raster_pixels` (at most 128 million), `max_prepared_bytes` (at most 4 GiB), and `wall_seconds`
(at most six hours). Preparation checks time between bounded tiles. Worker time and parent/owned
worker RSS are sampled; these are fail-stop guards, not OS quotas. Read-only input acquisition
costs and large-input checksum time are outside the per-worker timing measurement.

## Scientific support and compatibility

Sources use deterministic samples in their clipped land geometry, excluding building-present
10 m cells. Eye height is added to **preserved unflattened land ground**. Water-center flattening
changes target/intervening terrain only. Missing source ground fails. Grounded source cells and
private canopy clearance remain explicit observer assumptions, not evidence of access or activity.

Targets are actual 30 m water centers within the union of observer reach. Each H3/block representative
has its true population weight. The denominator is actual observer count times the full **in-reach
water-center population**; individual out-of-range comparisons contribute zero. This is partial H3
support, not the legacy equivalent-area full-cell denominator. There is no implicit all-region pair
universe or water-source model. Absent pairs are outside this source-specific population, not an
observed LOS result. Source-cell, target-cell and comparison-pooled means require explicit choices.

Terrain and roofs are opaque direct-grid obstacles. Canopy transmission is exponential in weighted
ray length inside ground-to-CHM columns, using retained WorldCover coefficients. Terrain is added
once; canopy/roof lower heights combine by maximum for opaque obstruction helpers. Supported roofs
remain intact beneath canopy clearance. Building absence is known zero only under the explicit
`building_inventory_policy: assume_complete`; `unverified` preserves absent inventory uncertainty.
`water_class_policy: zero_if_missing` explicitly assumes zero missing canopy in WorldCover water
with zero height floor; `retain_unknown` declines that assumption. Positive-height water conflicts,
unknown classes and missing roof heights are not converted to measured zero.

V2 direct rays retain unknown target obstruction while still traversing the known lower surface.
An intervening known roof or terrain barrier can prove blocked zero even with an unknown target.
A path without proven blockage stays unknown when unresolved upper obstruction is relevant.
V1 ray/height-envelope/transmission results require genuine recalculation under v2 identity.

`average_viewability` is the arithmetic mean of modeled transmission over all weighted comparisons.
It stays null with unknown comparisons. `lower`, `upper`, `input_unknown_fraction`,
`out_of_range_fraction`, actual observer count and target census/sample counts remain explicit.
It is not a binary visible fraction or an available-only mean.

`weight_terrain = mean(bare_LOS * D)` integrates the configured distance curve once.
`above_ground_lower/upper` divide weighted joint transmission bounds by terrain-clear weighted
support. `weight_above_ground` is null with relevant unknowns and all three factor fields are null
with `above_ground_status: not_applicable` when baseline support is zero. Known roof/canopy blockage
is a resolved zero. Their pairwise product reconstructs weighted transmission bounds.
`weight_distance` is a separate H3-centroid diagnostic and must not be multiplied into terrain again.
The default distance profile is normalized logistic d50 9 km, slope 2.7 km, cutoff 30 km; the direct
rays retain the stricter configured reach. Component target means cannot generally be multiplied.

## Completion and validation

Preparation writes separate `observer_ground.npy` and water-modeled `ground.npy`, aligned obstruction
and class bands, clipped geometry, source cells, effective configuration, and a prepared receipt.
V1 prepared grids without preserved source ground are rejected rather than relabeled. Scoring uses
installed-package workers and writes source pair partitions/observer evidence followed by
`average_viewability.parquet`. `manifest.json` is written last after source completion, unique
canonical pair keys, bounds/missingness validation, exact configuration, input, prepared-band and
package implementation hash checks. Interrupted or failed runs retain evidence without a completed
receipt; retry in a fresh directory. A product is accepted only with its validated completed receipt.

Census checks cover distributed sources and small target populations. A rejected maximum/p95
approximation gate prevents completion; partitions survive. The gates include unweighted bounds,
weighted terrain and conditional above-ground bounds plus applicability changes. Default tolerances
are 10 percentage points maximum and 5 p95, with only stricter overrides allowed. They are numerical
experiment tolerances, not empirical calibration or a guarantee for unchecked sources.

The regression suite covers the three reviewed failures, analytic arithmetic/factor denominators,
package/demo shared-calculation parity and a tiny real subprocess scoring generation. It does not
establish full regional input coverage, vertical-datum equivalence, inventory completeness,
class/beta calibration, field accuracy or full-data runtime/resource performance. Those remain
separate qualification steps before a regional release or consumer migration.

Execution receipts also pin Python/library versions plus GDAL/PROJ runtime versions. Workers
reject a package or runtime different from the preparation receipt. Existing demo adapters record
the actual v2 ray/transmission contracts on new calculations and retain the input model separately;
old retained artifacts are not relabeled. Empty target populations complete with an explicit
`no_target_population` source state, null summaries and no fabricated zero-valued pair rows.

## Bounded preparation and shared ray inputs

The numerical helpers can retain the explicit-v2 method while avoiding repeated work.
`prepare.area.transmission.endpoint_grid` bounds full source land and all potentially reachable
reporting-water endpoints. It crops in 60 m steps relative to the original source window so the
stride-two quadrature phase is unchanged. The canonical terrain warp still uses its original
1536-pixel parent grid and partial-edge dimensions. Cropped ray inputs carry the original window
as `ray_reference_transform`, preserving the original floating-point traversal arithmetic.

An explicit `terrain_support_mask` permits raw NaNs outside required support to remain unchanged.
Every declared required cell must be finite, and the direct kernel rejects any visited target or
ray cell outside that support. No missing terrain is filled, made opaque, or removed from the
requested population. Without this explicit mask the original whole-grid finite requirement
continues to apply.

`WaterTargetProvider` retains canonical 30 m water-centre row/column/H3 labels once. Each source
still selects its exact observer-reach population, counts, quadrature weights and denominator.
Cached full-H3 water areas are not a substitute for those source-specific counts. Geometry,
CRS, grid, implementation and native input identities must be pinned by the caller.

`weights.transmission.prepare_calculation` owns read-only stationary ground, roof and canopy
inputs. `calculate_prepared` reuses class coefficients across observers and census calls, and
accepts an explicit `observer_workers` count (default 1). Native ray kernels release the GIL;
observer threads write disjoint result rows and reduction order remains fixed. Preparation/cache
ownership stays serialized. The caller must measure and enforce aggregate RAM and avoid stacking
these threads with other native thread pools. This does not enable additional worker processes
or change the CLI's existing resource policy automatically.

The tile cache optionally stores lossless compressed arrays and reports hits, misses, evictions,
parent builds and preparation timings. Its worst-case reservation, byte hashes, array contracts
and atomic publication remain mandatory. Include both cached parent fields and the reusable
water/H3 provider when enforcing the shared-cache budget. Compression and thread-count gains
must be measured on representative inputs rather than inferred from small synthetic tests.

## Persistent xarray tile preparation and scoring

The explicit `transmission-tiles` workflow separates provisioned-input preprocessing from weights.
It retains one current immutable prepared store. Rasterio/GDAL still performs the canonical terrain
warps and building rasterization; xarray supplies named, aligned `(y, x)` layers and metre pixel-center
coordinates. It does not replace the native ray kernels or imply that Dask would accelerate them.

The versioned store uses one lossless compressed NPZ file per **384 × 384 storage tile**, sliced from
unchanged **1536 × 1536 canonical preparation parents**, including their original partial-edge sizes.
All nine fields retain their native Float32, bool and UInt8 representations without CF mask/scale
or fill decoding. Water-modeled ground and unflattened observer ground remain separate. No all-fill
chunks are omitted: every required tile has an explicit byte receipt. The independent 30 m water
index stores Int32 row/column/dictionary codes and exact H3 strings; it does not align onto the 10 m
xarray dimensions or convert H3 to floating point.

Preparation plans the union of source windows once and builds each required canonical parent once
per uninterrupted pass. Full eligible R7 land remains the observer support; reporting-water reach
controls target support. The conservative centroid read envelope adds the maximum projected full-cell centroid-to-vertex
distance to viewing range, plus a raster margin (150 m at this range: two 60 m padding/phase allowances,
a 10 m pixel diagonal, conservative buffer expansion, rounded up to 30 m). It changes only IO bounds.
The narrower endpoint crop must fit wholly inside that envelope. Every reachable endpoint belongs
to the endpoint union; its padded bounding rectangle contains every straight sightline and raster
footprint between those endpoints. Actual observer-to-target cutoff remains unchanged.
Extraction retains the original 60 m quadrature phase, original ray-reference
origin and at least 60 m endpoint padding. Paths require source ground, targets and all intervening
cells. Raw terrain NaNs outside declared required support remain NaNs; required missing terrain fails.

The strict request pins the provisioned metadata, native files and VRT members, ordered canopy/class
assets through metadata hashes, full source inventory, reporting-water WKB, geometry, grid, parent and
storage layout, preparation policies, code, codec and runtime. Distance coefficients and observer
threads affect scoring identity rather than field values. Preparation freezes census source IDs in
the original source order. Changes to relevant identities reject reuse; old tiles are never relabeled.

Create a request from an existing pinned regional recipe without executing it:

```bash
python scripts/make_transmission_tile_request.py --regional-config regional.json \
  --store-dir prepared-current --score-dir weights-current --output tile-request.json
python scripts/preprocess_transmission_tiles.py --config tile-request.json --stage plan
python scripts/preprocess_transmission_tiles.py --config tile-request.json --stage prepare
python scripts/score_transmission_tiles.py --config tile-request.json
viewshed-toolkit transmission-tiles --config tile-request.json --stage validate
```

The default stage is read-only `plan`. Native inputs must already exist; these scripts never acquire
sources. The request has explicit limits of at most 4 GiB aggregate resident memory and 2 GiB for the
combined prepared store/scoring products, with at most 180 seconds per invocation. Scoring uses one
source process and 1–4 observer threads. Owned child supervision covers individual preparation calls,
scoring and finalization. Byte reservations precede publication; limit failures retain partial evidence
and require stopping/reestimating rather than silently enlarging the budget. External workspace budgets
must also account for pre-existing task artifacts. Compressed-region sizing is an extrapolation until
all required chunks are measured, not permission to run the region.

Tile payloads and checksummed receipts are atomic; `complete.json` is written only after all expected
tiles and sidecars validate. Normal controlled partial preparation releases its lock and can continue
with `--stage prepare --resume`, skipping validated tiles. A hard interruption requires the explicit
`recover-store` stage first. Recovery refuses live/reused/unreadable owners, identity changes,
committed corruption, symlinks and unrelated files. It records hash evidence before removing only
uncommitted declared artifacts. It never steals a live writer lock or deletes committed tile data.

Scoring reads only completed immutable stores, verifies required chunks, slices before loading the
current source window and bounds its decoded RAM cache. It reuses the prepared water/H3 index and
stationary canopy coefficients across observer/census calls. Source partitions publish with atomic
receipts; `--stage score --resume` validates previous sources before skipping them. A live scoring
coordinator is protected by an exclusive lock recording its PID and generation identity. Abrupt
coordinator death leaves that lock and intentionally blocks resume: this version provides no scoring
lock-recovery command. Keep evidence and verify both coordinator and its workers have stopped before
requesting recovery of that specific lock; an absent coordinator PID alone does not prove its workers
have stopped. Never remove a live or unverified scoring lock. Final acceptance
rechecks all prepared tile bytes and weighted/unweighted/applicability census gates. Final output and
source evidence remain separately checksummed; validated atomic final bytes interrupted before their
receipt may be accepted only after exact comparison against every completed source partition.

NPZ was selected because its lossless byte parity, compression and atomic one-file chunk behavior are
already measured here and xarray is already a package dependency. Zarr would support a shared chunked
array and lazy regional slicing, but needs an additional dependency and format-specific dtype/fill,
chunk alignment and completion-ledger qualification. Its metadata writes alone are not a completion
transaction. See [xarray's Zarr write contract](https://docs.xarray.dev/en/stable/generated/xarray.Dataset.to_zarr.html)
and [raw Zarr reading options](https://docs.xarray.dev/en/stable/generated/xarray.open_zarr.html).
Rectangular chunks remain the scientific raster layout. Each planned source H3 has an explicit
`required_tile_keys` list, checked against its full padded window and the completed store before scoring.
This deterministic lookup preserves rectangular footprint coverage without a generalized hexagonal
storage abstraction.

### Bounded radius and scoring overhead

Persistent requests support an explicit `max_distance_m` up to 30,000 m; the default remains
24,140.16 m. This is a physical line-of-sight calculation ceiling, not a universal horizon.
The retained model uses eye height 1.7 m above ground, a 1 m target above the modeled water
surface, and fixed curvature/refraction assumptions. Physical transmission bounds remain
separate from optional distance-weight diagnostics. Changing the radius changes preparation
and scoring identity; never relabel an existing store.

Scoring and final proposal construction share one supervised child, retaining bounded memory
and process-local stat-qualified checksum reuse across sources and finalization. The parent
revalidates current inputs, the completed store, source receipts, final bytes, source inventory,
and the generation identity before atomically publishing completion. Child failure leaves
unreceipted final bytes for explicit resume; it cannot publish a completed product. Execution
metrics explicitly cover both scoring and finalization in that child.


### Covered building absence assumption

Persistent `assume_complete` preparation requires a pinned, `completed_validated` GBA acquisition
receipt matching the provisioned SQLite subset and an explicit user-approved
`gba_covered_absence_assumption_v1` qualification. Every acquired tile must have validated
polygon, ODbL footprint and height receipts. The full source reach and each canonical preparation
parent must fit inside the acquired tile union intersected with the retained study bounds.
Absent footprints within that qualified area mean modeled zero building height; footprint heights
that are missing remain unknown. This is an inventory-completeness assumption, not surveyed absence.

The conservative configuration records `canopy_nodata_policy="retain_unknown"`. Native valid zero canopy is
supported zero; native 255 NoData, unsupported raster coverage and missing assets are not absence.
Full-footprint qualified water retains its existing known-zero rule, including contradiction checks.
Terrain, LOS, sampling and optional distance weights are unchanged. Metadata and implementation
hashes separate this configuration from previous unverified stores and products; rebuild into fresh
roots rather than changing any existing completion manifest.


### Explicit zero imputation of acquired canopy gaps

Persistent scoring can explicitly select `canopy_gap_policy="assume_zero_acquired"` in new pinned
provisioned metadata. It requires a user-approved `eth_acquired_gap_zero_assumption_v1` qualification,
available pinned north-up ETH uint8 rasters with NoData255, and acquisition extent covering every
full source reach and canonical preparation parent. Missing files, failed reads, changed bytes and
unsupported coverage fail; they are never converted into absence. The building qualification's
`canopy_nodata_policy` must agree with this new policy.

Raw NPZ canopy values and missing masks are retained unchanged. Only qualified missing-height
zero-floor support is resolved to modelled zero during stationary calculation; valid CHM, terrain,
building uncertainty, observer/target samples and distance weights are unchanged. Source receipts
record original-mask and imputation-mask hashes, imputed counts/fractions and per-pair support
interpretation. A known result under this configuration is conditional on the imputation assumption,
not evidence of measured tree absence, and may overestimate physical visibility. New metadata and
implementation identities require fresh store/product roots; previous results are not relabelled.


### Lossless compact canopy storage

Canonical prepared canopy now uses native uint8 whole metres0–254 with a separate unchanged
missing mask;255 is the source NoData marker and is stored as a zero lower bound with missingness.
Preparation requires native uint8, NoData255, scale1 and offset0; alternate fractional-height
raster sources are explicitly rejected rather than truncated. Caller-owned floating-point arrays
remain supported by the calculation helpers without conversion. Valid canopy, imputation policy,
terrain/building float32 values and every support mask retain their scientific meaning.

The persistent store is `viewshed_direct_transmission_tile_store_v2`, the canonical preparation
identity is v2 and dense prepared-array persistence is v3. Previous stores are explicitly rejected
by the new reader; retain their previous implementation or rebuild fresh roots. Never relabel an
existing completion. The decoded payload is18 bytes/pixel instead of21; this is not a claim of a
14.3% whole-process RSS reduction. Classification keeps float64 coefficients but uses byte-sized
height-bin codes instead of a float64 height-score temporary. Precision experiments remain separate.


## Fast calculation

Persistent tile requests default to `calculation_mode: block_jump`. Set
`calculation_mode: reference` to use the original observer calculation.
`calculation_mode: exact_fast` retains every DDA step, curvature expression, float64 canopy coefficient,
full observer-row exponentiation and ordered pair reduction. Conservative
128-pixel block proofs omit obstruction or canopy checks only where immutably
owned fields prove them unnecessary. Mutable, aliased or out-of-bound surface
inputs use the original native kernel. Missing support remains fail-closed.

Requests explicitly control `observer_workers` (1–8) and `target_chunks` (1–4).
The measured scheduling choice is eight workers and four target chunks per
observer; four workers did not show a consistent chunking gain on both benchmark
windows. Worker count still defaults to one. One pool serves all source windows
in the persistent worker, and chunk jobs write disjoint slices. Whole-row
exponentiation and reductions preserve deterministic ordering in both fast modes.
The default `block_jump` additionally skips certified clear blocks as described below.

Preparation uses bounded 4,096-footprint raster batches, retaining maximum
overlapping float32 roof heights and independent presence/missing masks. The
original window-by-window footprint rasterizer remains available as
`rasterize_building_footprints_reference` for parity checks. Target grouping uses
lexicographic numeric grouping with the original water-point tie rule. The
coefficient lookup retains fractional CHM and all original float64 values.
A 64 MiB support-mask LRU is scoped to one retained water geometry, radius and
canonical grid, with the original geometry calculation used outside its proved
base coverage. Code/configuration identity continues to invalidate stale reuse.

These optimizations do not change observer eligibility, target population,
scientific uncertainty, the covered-building absence assumption or acquired
canopy-gap zero policy. Original masks and assumption provenance remain required.


The decoded tile cache can be set with `decoded_cache_bytes` (default 64 MiB,
maximum 256 MiB and at most one quarter of `max_memory_bytes`). This is private
retained tile RAM; source windows and caller copies are accounted separately by
the unchanged aggregate process guard. Every tile access still validates durable
checksums, including RAM hits. A cache miss hashes once before decoding. Store
telemetry records checksum and decode time, checks, hits, misses and decoded bytes.

Exact-fast observer clearance uses a private patch with a raster offset, preserving
the 30 m centre-distance rule and explicit source cell exception at grid edges.
Bare terrain reuses a roof-visible state only with identical geometry and validated
nonnegative building heights. Identically out-of-range states are shared; blocked
and unknown roof states still run the bare traversal. Target grouping uses integer
water dictionary codes internally and retains the original lexical H3 output order.
The unchanged whole-row exponentiation and matrix reduction order remain in force.

Window assembly consumes already decoded overlapping tiles before new misses can
evict them. Each tile still passes its durable checksum on every access, and
copies fill disjoint window regions. Source execution retains the prepared plan
order and frozen census IDs; simple raster or geographic source sorting did not
improve bounded-cache reuse in the retained-plan access-sequence experiment.

### Exact calculation and tile reuse

The `exact_fast` tile workflow shares a terrain-blockage proof from the roof/terrain
pass with the bare-terrain diagnostic. It only reuses a known terrain obstruction
under identical ray geometry; roof-only blockers and unresolved paths still receive
the separate terrain calculation. Canopy and unknown-state rules are unchanged.

On census sources, one full-population traversal also supplies the original sampled
subset. Raw sampled depths are selected before their own whole-row exponentiation,
and sample and census reductions retain their original order and weights. Both
sampling-error gates still run. Source evidence marks `shared_census_traversal`;
`scoring_seconds` then measures the sample reduction, while `reference_seconds`
accounts for the remaining shared census calculation. Their sum is the combined
calculation time. This saves duplicate rays only on census-checked sources.

The scoring worker supplies its pending source-window sequence to the decoded tile
cache. Within the existing byte budget, the cache preferentially retains tiles needed
sooner and can decline to cache a one-use tile. Sources and frozen census membership
are not reordered. Every requested tile still undergoes its durable checksum check,
including cache hits; decoded fields are not reused across changed stores. The hints
hold tile keys rather than raster data. Existing callers without a schedule retain
LRU eviction. These are runtime optimizations of the same raster model, not evidence
of physical visibility accuracy or a guaranteed regional completion time.


### Direct clear-block jumps

`block_jump` uses the same source/target design, obstruction rasters, curvature,
missingness rules, terrain-blockage reuse and shared census reduction as `exact_fast`.
It jumps to the exit of a 128-pixel block only after proving the entire surface
below the sightline, or proving zero canopy depth with complete relevant support.
Unknown heights, classes or missing terrain prevent those proofs. Cell crossings
are recomputed from lattice coordinates, so this is not bitwise identical to the
incremental-DDA `exact_fast` mode. Generic calculations outside the validated
10 m / 30 km numerical envelope retain the incremental fast path. That mode remains available for comparisons;
`reference` remains the unscheduled reference route.

Two retained SRKW windows showed 31–35% lower full-source time in bounded prototype
benchmarks, excluding durable tile IO and publication. Independent ray checks and
synthetic geometry tests supplement comparisons with the existing implementation;
this is not empirical visibility validation or a regional runtime guarantee.
Target-sampling experiments are not enabled: rare unknown coverage can be missed.

Both calculation mode and package code are already included in persisted identity.
Changed code invalidates existing preparation/completion and scoring receipts; use
fresh validated generations rather than relabeling existing caches. Existing
requests explicitly selecting `exact_fast` retain that mode; choose `block_jump`
to enable direct jumps. New requests omitting the mode select `block_jump`.


### Exact target-preparation reuse

Regional scoring continues to represent all eligible source land. The canonical
water population, deterministic observer design, sample representatives, weights,
and missing-data interpretation are unchanged by these preparation optimizations:

- Water reach tests stop checking a target after one observer establishes membership
  in the observer-reach union, using the same distance comparison at the boundary.
- H3 grouping uses the provider's existing integer dictionary without sorting every
  water point again; output H3 order remains lexical.
- Target quadrature groups and representatives are selected in one ordering pass.
  Row-ordered stride-1/2 populations use bounded integer keys and stable ordering;
  generic or overflow-prone inputs retain lexicographic ordering. Per-H3 census
  thresholds and source-relative sampling phase remain unchanged.
- Compact uint8 canopy/class inputs reuse an immutable 256-by-256 coefficient and
  unknown-class lookup (~576 KiB), derived from the generic float64 formula.
  Other dtypes retain the generic formula. This shares classification work across
  sources without retaining additional full-window rasters.

These optimizations do not reduce the number of rays, coarsen obstruction rasters,
change unknown coverage to known, or restrict sources to observation locations.
Code fingerprints still require fresh validated generations after implementation
changes. Timings from retained-window benchmarks exclude durable tile IO and do not
establish a full regional completion time.


### Bounded shared-raster batches

`source_batch_size` defaults to 2; set it to 1 for sequential window assembly.
The worker may assemble two adjacent, overlapping source windows in one tile pass,
decoding each shared tile once. It preserves source order, census membership,
all-land observers, target sampling, every raster byte and all missing-data masks.
Each source is still scored and published separately. Prepared arrays are private
and read-only; consuming each window rechecks every durable tile receipt, including
changes made after prefetch. A corrupt or changed tile fails rather than using a
stale buffered window.

Batch buffers must fit within one quarter of `max_memory_bytes`. They also must
fit a conservative scoring-memory estimate including the water index, decoded
cache and a 512 MiB reserve. Unsupported modes, duplicate/nonoverlapping windows,
or insufficient estimated headroom use sequential reads. The supervising process
retains its hard aggregate memory limit; the estimate is not an RSS guarantee.
Only two windows can be queued, and batch buffers are released after consumption
or when leaving the batch. This adds no durable storage or cleanup operation.

`raster_batch_assembly_seconds` records each window's pixel-proportional share of
batch assembly time. That share is included in its `preparation_seconds` and
`total_seconds`, so moving IO before scoring does not hide it from runtime evidence.
The window-consumption checksum checks are additionally timed in the normal path.
Code and request fingerprints require fresh validated generations after this change.

Group-level terrain-blocking proofs remain an experiment rather than an enabled
calculation path. Independent geometry and raw-ray parity checks are necessary but
do not by themselves establish either empirical accuracy or a runtime improvement.

### One-pass compact raster qualification

Canonical Float32 ground/buildings, UInt8 canopy/classes, and Boolean support masks
use one compiled pass to populate the same owned, read-only building and classified
canopy arrays. This avoids full-window temporary masks and repeated copies. The
coefficient lookup still comes from the generic classification formula; no height,
resolution, observer, target, land population, or unknown-coverage policy changes.
Other dtypes and signed building zero retain the generic preparation path. Invalid
building heights are rejected even on qualified water. Source-specific terrain
support, canopy-gap qualification/provenance and observer clearance remain separate.
The compact path's `class_preparation_seconds` includes joint qualification, while
`stationary_preparation_seconds` continues to include all stationary preparation.

Prepared-tile caching and a fused roof/terrain/canopy ray traversal were evaluated
but are not enabled: caching regressed when its working set exceeded its budget,
and the tested fused traversal was slower than the existing block-jump kernels.
No extra persistent cache or memory budget is introduced. Existing code fingerprints
require fresh validated generations; do not relabel previous calculations.
