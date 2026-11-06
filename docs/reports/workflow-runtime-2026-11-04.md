# Bounded workflow runtime and reusable caches — checked 2026-11-05

This follow-up exercises the computational workflow from retained qualified regional inputs
through land and water factors, final tables, observation geometry and static maps. It preserves
**30 m, native H3 R7, stride 1, adaptive 5–10 land observers and the configured water sampling**.
It does not establish a full-region wall-clock estimate or change delivered Data products.
The [inland elevation report](inland-water-elevation-2026-11-04.md) remains the source/elevation
qualification reference; the changes here preserve its marine-only sea-level policy.

## Scope and comparison limits

The frozen representative cohort has 144 land sources, the same 1,229 land observer coordinates,
and 144 distinct nearby native wet water-source cells. It uses all 13,461 retained native target
IDs and the current public polygon-distance candidate builder: **39,156 land pairs and 48,073
water pairs**. These counts differ from the preceding LOS-only benchmark's frozen 33,555-pair
lookup. This workflow exercises canonical pair construction and verified lookup reuse with
frozen memberships. Resolution, observer coordinates and sampling policy are unchanged.

The local driver substitutes only the frozen source-membership provider, then calls maintained
stages for canonical raster reuse, H3 geometry preparation, lookup, both distance products,
paired land LOS/composition/diagnostics, water LOS, water vegetation N/A, finalization/validation
and both roles' maps. Existing DEM/CHM/marine masks and qualified prepared raster stacks are
referenced in place. Acquisition, full regional membership correction and cold raster preparation
are excluded. Cleanup is not executed against retained benchmark evidence; its behavior is tested
with isolated fixtures. The final trials have a 900-second process deadline.

The original baseline did not finish: paired land work took 116.72 seconds and the global
shoreline-union water stage was stopped after 402 seconds without completing. Later exploratory
native whole-polygon/exterior buffers also stalled. No complete before/after workflow speedup
ratio can be inferred from those incomplete attempts.

## Implemented improvements

- One bounded Parquet pass now emits the three land factor projections and reduces their
  diagnostics, replacing independent repeated scans. Writers stage and validate outputs before
  promotion; failure preserves previous products. Input changes during streaming fail closed.
- Finalization reuses input digests only while filesystem identity, size, mtime and ctime match.
  Durable outputs retain full checksum validation, including changed inputs at the same path.
- The established workflow defaults to `run.cleanup_policy: retain_reusable`. Prepared rasters,
  canonical geometry/pixel indexes, lookup/factor tables, land/water checkpoints and distance
  partitions survive successful final validation. `compact` is available explicitly. Both
  protect configured sources and metadata; neither evicts external input stores.
- Clipped source unions and complete land/water domains are persisted with source-content,
  extent, algorithm/library and output-checksum contracts. The shoreline-fill set operation
  avoids a global water dissolve. Water-source geometry loading skips an unused land complement.
- Projected land is validated and repaired with native linework `make_valid` when projection
  creates invalid topology. Prepared land intersections plus an STRtree of original boundary
  segments now evaluate exact planar clearance distance, avoiding regional buffer construction.
  The configured clearance remains 30 m. This versioned improvement removes the former
  16-quadrant-segment arc approximation (at most 0.0361 m radial deficit at 30 m). Exact
  zero-length rays use valid Point predicates, avoiding a GEOS STRtree `dwithin` failure on
  invalid repeated-coordinate LineStrings; whole-geometry validation covers self-pairs. Numerical
  differences near the former buffered boundary are possible; actual cohort parity is checked separately.
  Native whole buffers, spatial clipping and boundary-buffer experiments were superseded;
  their partial logs are diagnostic evidence, not completed workflow validation.
- Static maps reuse content-addressed water masks on unchanged grids with the original
  pixel-centre rasterization. Grid, source, library and TIFF checksum contracts are checked.
- Metadata sidecars use atomic replacement. During benchmark setup, a copied cache sidecar was
  initially hard-linked and an in-place metadata update propagated to retained regional sidecars.
  Both originals were restored **byte-for-byte**, verified against the previously recorded
  SHA-256 `b87c1c959095d210b0049d643663bf6cc28b047bdc48e0ef7204feb0ee36cd60`.
  The driver now copies small metadata and regression coverage verifies hard-link preservation.

Native projection repair of the 7.4-million-vertex component took **894.56 seconds** once during
qualification. Its validated cache is reused in the timed workflow; this cold geometry bootstrap
cost is explicitly excluded from the prepared-input trial. Component-area sums changed by about
0.016 m² during native repair; these are topology diagnostics, not field accuracy estimates.

Canonical details: [performance](../performance.md), [cleanup configuration](../configuration.md#cache-retention-and-cleanup),
[methodology](../scientific-methodology.md#projected-land-blockers-and-repeat-preparation).

## Validated timings

| Stage | Fresh LOS, retained preparation | Verified stage rerun |
|---|---:|---:|
| Prepared rasters, H3 geometry, lookup and both distance roles | 4.91 s | 5.02 s |
| Paired land LOS, factors and diagnostics | 76.94 s | 2.87 s |
| Water LOS and combination | 31.57 s | 2.27 s |
| Water vegetation N/A, final tables and validation | 0.28 s | 0.28 s |
| Static maps, both roles | 108.04 s | 90.77 s |
| **Total** | **221.74 s (3m 42s)** | **101.20 s (1m 41s)** |

The fresh pass made **2,458 native GDAL LOS calls** for the paired land surfaces. The rerun
made none: it skipped all 288 land surface and 144 water source partitions after checking their
metadata. Water LOS uses the vector model and does not invoke GDAL viewsheds. Both trials used
pre-existing qualified domain/projection, H3/pixel and map-mask caches. The fresh pass rebuilt
source partitions; it did not rebuild those inputs. The rerun reached validated final tables in
**10.44 seconds**, then regenerated maps. Sampled parent RSS peaked at 6.65 GiB fresh and
8.55 GiB on the rerun; these are partial measurements, not process-tree memory bounds.

A prior, superseded diagnostic pass populated map masks in 184.18 seconds during export.
Its results are not a correctness reference or a complete before/after comparison. The retained
mask caches explain a removed repeated preparation step; rendering remains the main rerun cost.
The earlier conditional regional paired-land estimate is a different scope and has not been
replaced by a whole-workflow ETA.

## Independent validation

- All **432** fresh/resume native partition comparisons, both static tables, both observation
  geometry tables' scientific/state fields, and all **17** GeoTIFF grids, pixels and masks match
  exactly. Three HTML documents were produced; no browser rendering check was run.
- All 288 land native partitions, six factor tables and nine land map rasters also match their
  retained earlier references. The 39,156 land static weights are unchanged.
- Eight regional windows give topological equality for shoreline-fill preparation and **8,000**
  matching indexed versus independent whole-geometry distance decisions. The full projected
  regional land geometry independently confirms all **1,296** self-ray decisions from 432 water
  observer positions. Five water self-pair weights differ from the superseded experimental
  buffer output; the six affected points are 3.24–14.77 m from mapped land and correctly blocked
  at 30 m. All other water static weights and the entire dense pair universe match that comparison.
- **450 tests passed**, with two dependency warnings and no skips. Ruff, changed-file Black,
  component strict quality/type checks, byte compilation, architecture and `git diff --check`
  passed. Retention and compact cleanup were exercised in temporary fixtures.
- **2,014** retained input/checkpoint files match prior SHA-256 receipts, including the two
  restored source sidecars. Task evidence occupies 2.64 GiB in logical file sizes; 24.45 GiB
  remained free at inventory time. No manual cleanup or delivered Data promotion was performed.

## Retained replay evidence

Local evidence is in workspace `Reports/viewshed-workflow-runtime-2026-11-04/`; task outputs and
checkpoints are under `.marinecast-work/viewshed/workflow-runtime-20261104/`. The driver, frozen
cohort, per-stage metrics, bounded diagnostic attempts, exact-value validation, source/output
hashes, environment versions and staging manifest are retained there. No bulk raw archive was
copied and no final product was promoted into `Data/`.
