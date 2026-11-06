# Regional US/Canadian inputs and paired benchmark — 2026-11-04

Follow-up: the [inland-water correction and paired rerun](inland-water-elevation-2026-11-04.md)
supersedes this report's blanket-water flattening limitation and runtime projection. This report
retains the preceding input-qualification and benchmark evidence.

Regional DEM and canopy inputs are collected and coverage-qualified for the original source
population, including Canada. The final audit finds **zero missing DEM or canopy pixels across
149,200,653 mapped nonwater pixels** in the preparation support. The larger paired benchmark
completed **144 active source cells / 1,229 observers / 33,555 candidate pairs**, spanning all
32 geographic/country strata. Fresh paired LOS took **109.940 s including lazy setup**, then
**67.949 / 63.335 s** with prepared inputs; verified reuse took **1.774 s with zero LOS calls**.

Conditional scaling supports approximately **3.4–3.7 hours of steady paired land LOS** on this
machine. Complete regional wall time also depends on source reconciliation, initialization,
composition, the separate water-source model and maps. No complete regional production run,
delivered Data replacement, shared-study migration or readiness upgrade was performed.

The analysis remains **30 m EPSG:32610, native H3 R7, full pixel aggregation / stride 1, 30 km
range plus 1 km preparation margin, and the same deterministic adaptive 5–10 observer policy**.
Observer coordinates/counts follow corrected land geometry under that policy. The batch-area cap
was reduced from 20 to 12 million pixels to respect the unchanged 768 MiB estimated batch-memory
guard. Batch size 7, maximum workers 14 and the other physical/sampling settings were preserved.

## Sources, vertical reference and qualifications

| Input | Collected and used | Qualification |
|---|---|---|
| Canadian DEM | Native 30 m **DTM**, not DSM, from [NRCan MRDEM30](https://open.canada.ca/data/en/dataset/18752265-bda3-498c-a4ba-9dfe68cb98da) | CGVD2013 / CGG2013 heights; modeled forest/settlement removal outside lidar areas. Regional ranged reads retain about 594 MiB instead of downloading the complete 83.85 GB DTM. Open Government Licence – Canada. |
| US DEM | 17 latest-per-geographic-tile [USGS 1-arcsecond 3DEP bare-earth tiles](https://data.usgs.gov/datacatalog/data/USGS%3A35f9c4d4-b113-4c8d-8691-47c428c29a5b) | NAVD88 converted explicitly to CGVD2013 using retained PROJ grids, formal axis conversion and vertical shift; no ballpark operation. USGS border pixels are not accepted in mapped Canada. |
| Primary canopy | [ETH global canopy height 2020](https://langnico.github.io/globalcanopyheight/) | Native 10 m maxima to the unchanged 30 m grid, without overviews. Every valid regional ETH value is preserved. Its masked land is missing data, rather than evidence of zero canopy. CC BY 4.0. |
| Supplemental canopy | Published [WRI/Meta v6 heights](https://registry.opendata.aws/dataforgood-fb-forests/) | Native maxima, no overviews, finite `0 <= height < 255`, plus independent dated imagery support. The 89 active height objects retained unchanged identities through acquisition. Imagery dates span 2009-08-03 to 2020-08-23. CC BY 4.0 attribution retained. |

The mosaic prefers qualified USGS where available outside the retained Canadian country
reference, NRCan within Canada, and NRCan fallback where USGS is absent. The original
MRDEM-priority mosaic is retained. At 11,109 US land pixels adjacent to its transition, MRDEM
minus datum-normalized USGS had median `−0.189 m`, absolute 95th percentile `37.608 m`, and range
`−110.759` to `87.274 m`. These are inter-product differences, not ground-truth errors. Preferring
the qualified national terrain changes 17,723,318 height pixels, preserves all previously finite
coverage and the exact grid, and fits no offset or smoothing correction.

The vertical-operation fixture agrees between GDAL and pyproj to `7.8e-8 m`; this verifies the
software operation, rather than geodetic accuracy. Its stated 5.17 m operation accuracy includes
horizontal uncertainty and is not a terrain-height error estimate. Canadian field accuracy and
cross-border seam accuracy remain unvalidated. Of the required land pixels selected from MRDEM,
67,695,224 have nearest source class adjusted Copernicus, 1,477,829 blended transition, and
23,713,460 HRDEM lidar. Nearest source classes are indicative; bilinear height contributors can
straddle categories. Coverage does not establish uniform lidar or acquisition vintage.

Canopy footprints are united in their native geographic CRS, densified before projection, and
eroded 24 m to conservatively qualify complete analysis pixels. Tile date sets do not identify
the date of each maximum contributor. Conflicting auxiliary `.msk` semantics remain unresolved;
those masks are not applied. Availability uses the published main product plus dated footprint
and value-range checks. This is a mixed-source/mixed-vintage estimate, not a uniform 2020 canopy
observation or field-quality certification. Raw imagery was not collected or redistributed.

## Regional support and water reconciliation

The common grid is **23,901 × 19,508 pixels**, with EPSG:32610 bounds
`(39570, 5032950, 624810, 5749980)`. Qualification covers the original 26,573 source-land geometries
and their preparation support, excluding explicitly mapped water. Generic ocean nodata outside
required land is not treated as missing land height.

Independent nominal water combines the retained physical-water context, 22,178 accepted NHD
waterbody/area features, 43,728 definite BC FWA lakes (`GB15300000`) and 4,760 definite rivers
(`GA24850000`). Mapped NHD marsh overlaps are removed from new NHD additions; intermittent and
indefinite BC lakes are excluded. The retained BC river request covers its recorded query bbox;
it is not certified as a complete province-wide river inventory.

All 36 residual canopy-gap centers near Texada are independently mapped ocean in
[BC TRIM Enhanced Base Map Ocean](https://catalogue.data.gov.bc.ca/dataset/905940c3-284b-4bba-8669-b2bf855542ca),
layer 63, object 9, code `GG19265000`. A bounded audit window uses the actual native polygon,
with original water preserved. Native/projected shoreline and final geographic geometry checks
passed. These are water-mask omissions, rather than demonstrated missing land data; no water
polygons were invented from the missing-height pixels.

Large-grid validation exposed a second mask error: transforming only four projected raster
corners clips out mapped northern water. The boundary now uses segments of at most 1 km before
geographic conversion. Canonical raster-stack v3 invalidates old core/canopy caches, and its
version is included in land partition/component identity.

Small topology repairs after water-union reprojection changed metric area by `−0.0329895 m²`,
without simplification or precision reduction. Pre/post repair 30 m all-touched masks matched
exactly over 466 million pixels. The final canonical mask preserves the independent feature burn
except the same pre-existing boundary pixel at `(447345, 5114145)` EPSG:32610. All 36 Texada
centers are water in the actual mask. Relative to the earlier canonical mask, 452,955 pixels
become water and 15,728 cease to be water, including edge-fill changes. Its 129,542,142 additions
beyond the detailed-feature reference are separately attributed to the existing edge-connected
Natural Earth ocean-fill policy, rather than detailed BC/NHD evidence.

A candidate-level check found one pixel outside the original region-wide buffer approximation,
although it lies within 31 km of source land. Inscribed 64-segment buffer arcs can omit up to
37.341 m radially. A **qualification-only 60 m guard**, clipped to the existing grid, covers that
approximation error; it changes acquisition/QA support, rather than analysis radius, resolution
or observer sampling. Its 6,784 extra canopy-gap pixels were acquired in 176 native windows,
all inside accepted dated coverage. This resolves the candidate-specific margin gap.

| Final guarded land support | Pixels |
|---|---:|
| Required mapped nonwater | 149,200,653 |
| Missing DEM | 0 |
| Missing canopy / unqualified canopy provenance | 0 |
| ETH canopy | 139,941,171 |
| WRI/Meta canopy | 9,259,482 |
| Changed valid ETH values | 0 |

The supplement includes 149,048 pixels reused from checksum-verified earlier work, 9,103,650
filled by the main acquisition and its small domain-correction supplement, and 6,784 guard fills.
The original acquisition bitmap/checkpoints remained immutable during collection. Newly exposed
land from the corrected mask was collected separately in eight small windows.

These are nominal static hydrographic classifications, not tide-specific shorelines or a full
independent wetland audit. The existing endpoint policy flattens mapped water to configured sea
level, including inland water; inland lake/river heights are not physically qualified by this
benchmark. Source availability, software consistency and field/model accuracy remain separate.

## Measured optimizations

- Valid immutable geometries avoid redundant repair/union/validity work; weak identity caching
  does not retain regional datasets. Componentwise clipping and supported disjoint-subset union
  preserve polygon support, with fallback tests.
- Water rasterization uses bounded work tiles with the same 30 m grid and all-touched policy.
  Fixtures check holes, edge contacts, invalid-source handling and tile-size independence;
  regional pre/post masks match as described above.
- Native H3 water intersections use prepared wet/dry predicates and padded local clips. A real
  27-target probe fell from 13.632 to 1.757 seconds (7.76×), with maximum symmetric difference
  `1.52e-6 m²` from numerical overlay roundoff. Native H3 boundaries and EPSG:6933 water-area
  denominators remain unchanged. H3 geometry v2 and partition identity invalidate earlier
  artifacts; bitwise identity with legacy overlays is not asserted.
- H3 water-area preparation no longer constructs an unused regional land complement when water
  is supplied. Its water result and land-derived fallback are regression-tested.
- A DEM height update can reuse a water mask only after matching water-source hashes, domain
  algorithm and grid, and verifying cached bytes/grid/tags. Changed inputs or corrupt masks
  fail closed; the endpoint is rebuilt from revised terrain. Immutable mask bytes are linked
  within the cache when supported. The revised core took 15.219 seconds with exact water-mask
  pixel parity; the earlier full core/water preparation took 585.746 seconds. These timings
  describe different amounts of required work, rather than a general whole-run speedup.

A larger batch initially exceeded the 768 MiB estimate guard (783.8 MiB). The successful trials
use the 12-million-pixel cap and retain other batch settings. All **52 available physical
partitions** from the earlier larger-cap attempt match the smaller-cap results exactly.
Guardrail guidance now suggests reducing batch size/area instead of scientific radius/resolution.

## Benchmark selection and results

The design was fixed before timing: 0.75° latitude × 1.5° longitude × country-reference strata,
with deterministic land-fraction rank representatives at 10/30/50/70/90%. The retained full
population counts verify the stratum origins `(45.5, −130)` and the selection. This gives 157
candidates across 32 strata (95 Canadian, 62 US by the retained country reference).

One candidate is below the existing minimum land fraction. The other 156 have complete required
height support. Twelve of those have no target pairs under the unchanged native R7 / 30 km
centroid-cutoff policy and require no LOS work. The benchmark therefore uses **144 active cells
(87 Canadian, 57 US), 1,229 actual observers and 33,555 candidate pairs**, retaining all 32 strata.
Every exclusion is recorded; there are no missing-height exclusions in the final selection.

Source geometries are clipped against independent vector water, raster nonwater and native H3
boundaries. Areas/fractions are recomputed in EPSG:32610, ancestry is retained, and the existing
sampler is regenerated deterministically. Validation checks valid positive geometry, no material
area outside parent/native H3 or in mapped water, raster-land containment, no positive-area
cross-cell overlap, representative points, distinct observers and reversed-row sampling parity.
Area tolerances are `max(1e-4 m², area × 1e-10)`. Six source fractions exceed 1 by at most
`2.22e-16` from area-summation roundoff, within the explicit `1e-12` validation tolerance; no
observer-policy or geometry correction is inferred from that numerical artifact.

| Trial | Paired elapsed | Native LOS calls |
|---|---:|---:|
| First / lazy setup, retained prepared inputs | 109.940 s | 2,458 |
| Fresh LOS partitions, warm inputs | 67.949 s | 2,458 |
| Fresh LOS partitions, warm inputs | 63.335 s | 2,458 |
| Verified existing partitions | 1.774 s | 0 |

All three fresh trials use independent partition directories, with prepared inputs reused and no
download activity competing with timing. Independent validation compares every scientific
partition column: **576 repeat comparisons matched exactly**, plus the 52 batch-cap comparisons.
Each trial completed all 288 cell-surfaces. Sampled parent RSS was around 2.0 GiB; process-tree
collection was partial. CPU and disk/memory sampling scopes are retained with the timings.

## Regional runtime estimate and limits

A bounded membership/count census uses the 26,573 original sources and 13,461 existing native
targets, with exact spherical nearest-target cutoff checks; it does not expand all pairs or run
regional LOS. Of those sources, **25,432 have eligible target pairs**, requesting **233,225
observers** under the current sampler before full source reconciliation. Applying the minimum
land fraction to those original rows gives 25,230 sources / 232,215 requested observers.
The original unfiltered nominal population is 241,307 requested observers.

| Conditional steady paired land LOS estimate | Hours |
|---|---:|
| Linear scaling to original active requested observers | 3.34–3.58 |
| Geography/country post-stratified native work, calibrated to measured elapsed time | 3.43–3.69 |
| Older unfiltered nominal-observer scaling | 3.45–3.71 |

Post-stratification uses observed native-call work per stratum and original active observer
populations, with measured elapsed/native-work ratios. It assumes preparation/concurrency transfer;
this is not a statistical confidence interval. Full-region spatial packing can be denser than this
sample, and locality, cache effects, corrected source populations, initialization and outputs can
change runtime. Acquisition and regional preparation are already retained for reuse, but their
one-time costs are excluded from these steady LOS estimates. Water-source geometry work,
composition, maps and complete end-to-end production were not benchmarked. A corrected full
regional observer census was not generated.

This evidence supersedes the smaller Puget-only timing for regional planning. It supports
planning around **3½ hours for steady paired land LOS**, with the stated scope and limitations.

## Validation, storage and replay

The complete owner suite passed **412 tests with no skips**, including native GDAL fixtures,
component/case-study and architecture tests. Ruff, changed-file Black checks, compilation and the
component Ruff/Black/strict-mypy gate passed. The two suite warnings concern the existing Polars
GeoArrow extension behavior. No complete regional production, maps, application integration or
field validation was run.

At the workspace root, `Reports/viewshed-regional-qualification-2026-11-04/` retains:

- `summary.json`: independent geometry, support, repeat and packing-parity validation.
- `workload-estimate.json`: verified original population strata, active census and projection math.
- `paired-results.json`: all trials, native call traces, telemetry and per-source statuses.
- `regional-coverage-guarded.json`, `regional-water-mask-validation.json`: complete support/mask checks.
- `prepared-input-identities.json`, `prepared-source-catalog.json`, `environment-and-code.json`:
  raw-byte SHA-256, companion provenance, source receipts and software/code versions.
- `staging-manifest.json` and its inventory: exact task-owned paths, retained caches and cleanup status.

Prepared inputs are the configured `dem-qualified-30m.tif` and `canopy-qualified-30m.tif` under
`.marinecast-work/viewshed/regional-qualification-20261104/`, with provenance companions. Provider
windows/metadata and geodetic grids stay in the owning source/cache stores. No unique raw source,
active checkpoint or user file was deleted; failed/superseded task preparations are retained and
identified in the inventory.

The report scripts are a frozen local acquisition/qualification/benchmark recipe, rather than a
new registered Canadian provider. The benchmark configuration reads prepared local assets with
acquisition disabled; it is not an instruction to run the full component dependency closure.
`benchmark.py` and `validate.py` replay physical weighting/validation against those pinned inputs;
new timing attempts should use fresh partition locations and preserve the retained evidence.
Canonical provider/component commands remain documented in [data acquisition](../data-acquisition.md)
and [pipelines](../pipelines.md).
