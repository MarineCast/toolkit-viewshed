# Viewshed audit fixes and optimization — 2026-11-04

The [audit](audit-2026-11-04.md) identified unsafe cache reuse, discarded CLI limits, numerical
edge cases, and avoidable preparation and aggregation work. This follow-up implements those
repairs in the local checkout based on `5d1d2b662ec23855d58c5e6690a68dac63e81729`.
Configured raster resolution, H3 resolution, sampling counts, aggregation stride, and physical
distance models were not reduced or changed to obtain speedups. No regional acquisition, build,
data promotion, publication, or commit was performed.

## Correctness and cache recovery

| Audit item | Implemented behavior |
| --- | --- |
| F1: legacy distance cache | Partitions bind effective model/cutoff, lookup content, role, schema, and exact source selection. Atomic writes and checksum sidecars protect reuse; combination selects the current manifest rather than globbing old generations. Target/source summary commands use the same verified inputs. |
| F2: whole-run resume | Run manifests bind relevant source content, shapefile companions, metadata sidecars, and numerical implementation identity. Missing/stale provenance or changed/corrupt artifacts prevent completed-run reuse. |
| F3: land pair-universe cache | Lookup content enters land source-partition identity and the outer component contract, preventing an uncomputed new pair from becoming a modeled zero. |
| F4: CLI bounds | Both `build-distance-weights` and `distance-weight` forward `--start` and `--limit`; exact selections have distinct partition identities. |
| F5: run-ID collisions | Manifest conflicts are rejected before stage execution or production cleanup. |
| F6: water range | Maximum range is inclusive and enforced for each observer/target sample before land intersection; the full sample denominator is retained. |
| F7: geometry order | Repaired multipart geometries are normalized and sorted before choosing the central observer seed. Sampling version advances to v5; method and requested sample counts remain unchanged. |
| F8: experimental offsets | Coordinate matching uses zero relative tolerance, a strict absolute tolerance, and exactly one match. |
| F9: canopy backend | Unsupported xrspatial canopy execution fails early instead of claiming equivalent observer grounding. |
| F10: invalid settings | Nonfinite/invalid physical sizes, radii and heights, invalid raster resolution, and nonmetre projected CRS are rejected during configuration validation. |

Completed legacy `process` manifests are deliberately conservative: changed inputs, old manifests,
or failed integrity checks do not trigger an implicit rebuild, because some legacy stage producers
still have existence-based caches. Explicitly rebuild the affected stages with `overwrite=True`
(CLI `--overwrite`), then use a new run ID. `force` does not bypass scientific/cache validation or
manifest collisions. Unchanged, verified runs still resume without repeating computation. The
explicit component workflow keeps its own validated cache and no-implicit-cleanup contract.

The numerical edge-case corrections can change affected results. Old sampler/LOS partition
identities are invalidated. Twelve ordinary sampler geometry/count combinations retained exact
baseline coordinates; this does not imply that the deliberately corrected multipart cases match
their old outputs. Physical viewability remains distinct from observer activity, reporting,
detection, occurrence, and abundance.

## Implemented performance changes

1. Water component plans omit unnecessary DEM/CHM acquisition and preparation. Distance-only
   plans remain raster-free. `build static` stops after validation; `build all` also renders maps.
2. `plan_components` and `build --dry-run` report stage order, inputs and header readability,
   coverage envelopes, native execution mode, acquisition requirements, cache presence, run-ID
   conflicts, disk/RAM, and metadata-based workload estimates before execution. Cache presence is
   explicitly unverified; envelope checks do not establish valid pixels. Normal mandatory
   shared-study checks still run. No regional ETA is inferred from small fixtures.
3. Disk usage is sampled every five seconds plus boundaries, independently of 50 ms process-RSS
   sampling. Reported elapsed time includes monitor startup and shutdown.
4. When both land components are stale, DEM and canopy reuse paired raster/observer preparation.
   Independent component stages remain available, with their existing per-role promotion rules.
5. Repeated input fingerprints reuse a device/inode/size/mtime/ctime-aware digest cache.
   Durable output verification still computes full checksums.
6. Worker count alone no longer invalidates scientific source partitions. Other batch settings
   remain in identity until equivalence is established.
7. Canonical target codes mask only aggregation inputs before accumulation. Obstruction rasters
   and LOS are unchanged; cumulative debug rasters retain full-water support. Target-code grids
   are validated before masking, including codes on nonwater pixels.
8. The Numba path reduces accumulator tiles directly into compact per-target summaries, avoiding
   concatenated pixel arrays and the per-pixel grouping frame. Integer counts, quantized weights,
   observer-mask OR, union support, global stride, diagnostics, and canonical denominators are
   retained. The prior reducer remains the fallback. Memory planning now includes reduction and
   fallback/debug storage; it remains an estimate rather than a hard native-memory limit.
9. Map export computes all factor summaries in one lazy table scan and shares target geometry.

## Bounded measurements

| Component | Before → after | Interpretation |
| --- | --- | --- |
| Aggregation, 1,048,576-pixel grid, 839,139 visible pixels, 256 targets, five warm trials per isolated process | Median 56.02 ms → 8.43 ms; **6.65×** faster | Aggregation only, excludes LOS and preparation. Exact physical-output SHA-256 match; maximum mean-distance difference was 0 in this fixture. |
| Same aggregation process, sampled RSS | First-trial increment 136.44 → 17.69 MiB; maximum observed process RSS 785.97 → 372.16 MiB | Includes process/import state; 2 ms sampling may miss short peaks. Not a universal memory bound. |
| Shared land preparation, synthetic 73-pair component build, three alternating warm trials | Preparation calls 6 → 3; median 0.988 → 0.793 s (**19.7%** less elapsed time) | DEM, CHM and static weights match within `1e-7`; this small fixture is not a regional runtime forecast. |
| Disk telemetry around 0.5 s idle workload with 5,000 files | Scans 8 → 2; median CPU 0.236 → 0.080 s (**66%** less CPU) | Elapsed time was approximately 0.55 → 0.58 s; no latency improvement is claimed. |
| Four land map summaries | Full-table scans 4 → 1 | Regression verifies summary values and geometry reuse; no rendering speed percentage measured. |

No claim that a complete regional run finishes in minutes is supported yet. Remaining cost
depends on native raster coverage, observer count, configured radius, cache state, and LOS work.
Use the dry-run before scheduling a region, preserve validated checkpoints, and measure a bounded
representative run before choosing concurrency or estimating total runtime.

## Validation and evidence

The combined package suite passed **391 tests, no skips, two warnings, in 20.18 seconds** on
2026-11-04. It includes component and case-study fixtures, architecture boundaries, native GDAL
CLI/in-process parity, cache corruption/model/source-slice regressions, preflight, and cleanup
contracts. The two warnings concern GDAL's future exception default and GeoArrow extension loading.

New reduction checks compare the former reducer across full and sampled aggregation, target
subsets, empty support, different tile partitions, invalid target codes, compiled/fallback modes,
and 63/64-observer mask behavior with zero attenuation. A native fixture verifies exact component
table equality with early filtering enabled versus full-water cumulative debug output. Native
one- versus four-worker execution matches exactly and changing only workers reuses partitions
without LOS. Public legacy aggregate tests now reject stale model/lookup and corrupt partitions.

Repository Ruff, Black on all 33 changed Python files, byte-compilation, and `git diff --check`
passed. The required component gate passed expanded Ruff and Black over its 26-file scope and
strict mypy over 22 source files. An optional expanded Ruff check of four older terrain modules
still reports the same 50 pre-existing diagnostics by rule/file as the baseline; those modules
are outside the incremental expanded gate. This change does not claim a whole-tree expanded
Ruff/strict-mypy cleanup. Documentation links, API names/examples, and Knowledge properties were
checked; this repository has no dedicated documentation build configured.

Environment: Python 3.14.6, GDAL Python/CLI 3.12.3, Rasterio 1.5.0, NumPy 2.4.6,
Numba 0.65.1, Polars 1.42.1, GeoPandas 1.1.4, and PyArrow 24.0.0 on macOS arm64. Validation tools
were isolated in task-owned temporary storage rather than installed into the runtime environment.
Read-only CLI plans were inspected for land and water; both correctly reported missing configured
inputs, and water omitted raster dependencies.

Tests use synthetic data and exercise native GDAL locally; no complete regional dataset or native
xrspatial canopy run is claimed. The canonical configuration's input paths were absent at the
initial audit; other raw stores were not copied or searched for a substitute regional run.

Small replay scripts, benchmark JSON, validation logs, environment details, changed-file hashes,
and the task staging manifest are retained in the local MarineCast workspace at
`Reports/viewshed-fixes-2026-11-04/`. Test fixtures and isolated validation tools remain under
`/private/tmp/viewshed-fixes-20261104`; no permanent cleanup was performed. Existing delivered
`Data/` products were not modified.

See [API and recovery](../api.md), [pipeline dependencies](../pipelines.md),
[configuration](../configuration.md), [scientific methodology](../scientific-methodology.md),
and [performance](../performance.md) for the maintained interfaces and qualifications.
