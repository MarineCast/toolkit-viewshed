# Runtime telemetry

The [2026-11-04 audit](reports/audit-2026-11-04.md) records the pre-fix findings and bounded
measurements for revision `5d1d2b6`. The [implementation report](reports/implementation-2026-11-04.md)
records subsequent fixes and validation. The later
[completed regional run](reports/full-regional-run-2026-11-05.md) measured **3h 4m 9s** end to
end with retained inputs, including preparation, map export and independent validation.

Each component build records wall time, parent CPU time, sampled peak process-tree RSS,
work-directory disk usage, and applicable row/source/pair throughput in its run manifest.
RSS sampling uses a 50 ms interval; a separate disk sampler walks the work directory every
5 seconds. Both also sample at stage boundaries. Recorded wall and parent CPU time include
telemetry overhead, including those boundary scans. Short peaks can be missed; CPU excludes
children and disk usage covers only the configured work directory. Unavailable memory is null,
with partial/scope flags. The manifest records both sampling intervals.

The package uses Python, GDAL, and Numba. No Rust extension or regional benchmark suite is included.
Regional run telemetry describes the executed configuration and is not a general performance claim.

Regional execution avoids repeated work while preserving numerical contracts:

- Batch packing reuses candidate merge scores, with exact equivalence to exhaustive greedy packing.
- Input checksum reuse checks device, inode, size, modification time, and change time; changed files are fully reread. Durable output validation still calculates full checksums.
- Water-source denominator conversion is limited to candidate targets, and cached per-source result tables are bounded.
- Component composition releases each input after its join to limit peak regional memory.
- Water dependency plans omit DEM/CHM acquisition and preparation because the opaque-land model uses geometry.
- Land component builds share batch preparation when both DEM and CHM need computation, retaining independent component caches and stage commands.
- Factor-map export performs one aggregate scan and reuses target geometry across factors.
- `build static` finalizes and validates the selected role's table without map export; `build ... --dry-run` reports prerequisites and planned work before computation.

## Bounded measurements

The following local synthetic probes were run on 2026-11-04. They isolate particular costs and
do not predict a full regional run. Replay details and retained evidence are linked from the
[implementation report](reports/implementation-2026-11-04.md).

| Change | Probe and result | Correctness and limits |
|---|---|---|
| Tiled terrain aggregation | 1,048,576 pixels and 256 targets; aggregation-only speedup of 6.647× | Exact output SHA parity in this fixture; excludes LOS, preparation, acquisition, and complete workflow costs. |
| Shared land preparation | Three warmed, alternating native trials: median 0.987939 s independently versus 0.792830 s paired, about 19.7% less elapsed time; batch preparations fell from 6 to 3 | Each DEM, CHM, and static table had 73 matching rows at absolute tolerance `1e-7`; this is a small synthetic component workflow. |
| Separate disk telemetry cadence | 5,000 files during a 0.5 s idle workload; median monitoring CPU fell from 0.235695 s to 0.079772 s, about 66%; directory walks fell from 8 to 2 | Outer elapsed time did not improve in this probe. The result measures monitoring CPU, not viewshed computation speed. |

For a real workload, use its manifest to distinguish acquisition, preparation, LOS, composition,
and map costs. Shared DEM/CHM execution is recorded on the DEM stage with `includes_stages`; the
CHM stage identifies it with `computed_with_stage`. Do not count the paired computation twice or
interpret the CHM entry alone as the cost of an independent canopy calculation.

Batch shutdown removes only empty shared scaffolding. A process must not delete another source
role's active raster directory. Each completed batch retains responsibility for its own scratch
files, and failed preparations remain available for diagnosis.

Observer-specific canopy clearance uses a GDAL VRT overlay and a small private patch in the
configured batch directory. It avoids copying the full obstruction raster for every observer.
Regression checks compare every raster pixel and GDAL visibility output against the full-copy
implementation, including edge observers, clearance radii, nodata, and unchanged shared inputs.

## Regional coverage and runtime evidence — 2026-11-04

The [input collection audit](reports/coverage-collection-2026-11-04.md) identified canopy nodata
in newly collected Puget windows. The [canopy reconciliation](reports/canopy-resolution-2026-11-04.md)
resolved required land coverage with independent water evidence and separately attributed canopy
estimates. The [source reconciliation and eight-cell benchmark](reports/source-reconciliation-2026-11-04.md)
subsequently corrected the five observer/water conflicts and validated all eight source geometries.

All eight cells now run with 53 observers under the unchanged adaptive 5–10 policy; one cell's
corrected land fraction reduces its count by one. The 30 m/R7 grid, full-pixel aggregation and
physical settings remain unchanged. Paired fresh LOS took 2.324/2.300 seconds warm after a
20.813-second first/setup run using retained prepared inputs. Verified reuse took 0.122 seconds
with zero LOS calls. Repeated physical partitions matched exactly; no observer/water conflicts or
missing required DEM/canopy pixels remain in the corrected support.

Nominal scaling gives 2.91–2.94 hours of steady paired land LOS. This is a small,
mixed-source/mixed-vintage benchmark, not a full-region wall-clock ETA. Regional data acquisition,
Canadian input qualification, corrected source populations, preparation, composition, water-source
work and maps remain outside this estimate. Source corrections are benchmark-scoped; delivered
Data and the pinned shared study are unchanged. The older 4–8-hour scheduling budget remains
superseded.

## Larger US/Canadian benchmark — 2026-11-04

The [regional qualification report](reports/regional-qualification-2026-11-04.md) supersedes the
smaller Puget-only runtime projection. Qualified USGS/NRCan terrain and ETH/WRI-Meta canopy have
zero missing heights over 149,200,653 mapped nonwater preparation pixels, including a conservative
qualification guard. Analysis remains 30 m/R7/full-pixel aggregation with unchanged adaptive
5–10 sampling. The successful benchmark uses a 12-million-pixel batch-area cap under the same
768 MiB estimate guard, rather than reducing resolution, radius or observer sampling.

It completes 144 active sources, 1,229 observers and 33,555 candidate pairs across 32 strata
(87 Canadian / 57 US). Fresh paired LOS takes **109.940 s including lazy setup, then 67.949 /
63.335 s warm**. Each fresh trial makes 2,458 native calls. Verified existing partitions take
**1.774 s / zero native calls**. All 576 repeat scientific-partition comparisons and 52 comparisons
against the earlier larger batch cap match exactly. Sampled parent RSS is around 2.0 GiB;
process-tree sampling is partial.

The original source membership has 25,432 cells with native target pairs and 233,225 requested
observers before full source reconciliation. Linear active-population scaling yields 3.34–3.58 h;
geography/country post-stratified native work calibrated to elapsed time yields **3.43–3.69 h**.
These are conditional steady paired **land LOS** estimates, rather than a statistical confidence
interval or complete-run ETA. Source population corrections, spatial packing/locality, initialization,
composition, water-source work and maps can change total time. One-time acquisition/preparation
is retained for reuse and is excluded from these steady figures.

Preparation now uses bounded native-H3 water clips (13.632 → 1.757 s in a real 27-target probe),
avoids constructing an unused land complement for supplied-water H3 areas, and reuses verified
water masks when only DEM heights change. Canonical raster-stack v3 fixes large-domain boundary
conversion; H3 geometry v2 and corresponding partition identity invalidate affected old artifacts.
The report retains source/datum qualifications, the scope of numerical parity, coverage diagnostics,
all exclusions and replay evidence. Those timings precede the inland-water correction below;
mixed-vintage modeled inputs remain a scientific limitation. No delivered metric or readiness
status was promoted.

## Inland-water elevation correction — 2026-11-04

The [inland-water report](reports/inland-water-elevation-2026-11-04.md) supersedes the previous
blanket-flattened endpoint benchmark. Explicit marine evidence now owns sea-level assignment;
all 7,226,286 mapped lake/river pixels retain their finite DEM heights exactly. All-water target
support, all 144 source geometries and 1,229 observer coordinates remain unchanged, as do the
30 m/R7/stride-1 grid, adaptive 5–10 sampling and batch settings.

Fresh paired LOS takes **119.032 s including lazy setup, then 63.853 / 74.552 s warm**, with
2,458 native calls per trial. Verified partition reuse takes **1.872 s / zero calls**. All 576
repeat comparisons across 22 scientific partition columns match exactly. The height correction
changes 1,230 bare-earth and 259 canopy weights relative to the earlier blanket-flattened run;
old land endpoint/partition/component caches are invalidated by canonical stack v4.

Conditional steady land-LOS scaling gives 3.37–3.93 h linearly or **3.44–4.03 h** with the retained
country/geography post-stratification. Acquisition, initialization, full corrected regional
observer census, composition, water sources and maps are still unmeasured. Two warm trials do
not isolate an elevation-fix performance effect or establish a complete workflow ETA.

## Repeated reads and reusable preparation

The paired land factor writer streams the composed Parquet once in bounded batches to produce
terrain, canopy-LOS and vegetation projections while reducing diagnostics in the same pass.
Outputs are staged and validated before replacement; a failed promotion restores previous
outputs. Finalization reuses input digests only while device, inode, size, modification time and
change time remain unchanged. Persisted output checksums are still verified in full.

Retained regional geometry caches avoid repeating clipped unions, land/water domain construction,
projection repair and exact coastal boundary distance indexes. Cache presence alone never establishes
validity: content, scientific parameters, algorithm/library versions and actual output bytes must
match. Water-source geometry loading uses the water domain directly instead of constructing an
unused land complement. Static maps share content-addressed water masks on identical grids.

The established workflow defaults to `run.cleanup_policy: retain_reusable`; see
[cache retention](configuration.md#cache-retention-and-cleanup) for the space/runtime tradeoff and
`compact` alternative. Changing this storage policy alone does not invalidate physical partitions.
Metadata sidecars use atomic replacement so updating a task copy cannot modify a hard-linked
source sidecar in place. Concurrent writers to the same output paths remain unsupported.

The [bounded real-data workflow report](reports/workflow-runtime-2026-11-04.md) separates
prepared-input fresh computation, validated reuse, and map export.

## Static map rendering

The renderer colors smoothed rasters in row strips with Matplotlib's byte palette. Pixels
outside support receive the same transparent color directly, and only present values pass
through palette lookup. Display quantiles still use float64 interpolation over the finite
positive values. Filtering before conversion avoids a full float64 copy of each raster.

On November 5, 2026, two profiled exports of the same prepared regional cohort took
118.25 s before the change and 71.61 s afterward, a 39.4% reduction. All 17 PNGs and 17
GeoTIFFs matched byte for byte. The five map value tables and three HTML maps also matched.
The HTML comparison normalized generated Folium IDs and PNG cache timestamps.

The bounded workflow used retained regional preparation and the same 144 land and 144 water
source cells, 30 m input raster, native H3 R7 grid, and sampling policy.

| Bounded pass | Total | Map export | Native land LOS calls |
|---|---:|---:|---:|
| Fresh LOS | 169.65 s | 66.54 s | 2,458 |
| Verified resume | 79.64 s | 68.54 s | 0 |

Both passes reproduced the reference map rasters, HTML content, and value tables. Map manifest
checks used each run's own finalized artifact checksums and compared the remaining fields after
normalizing run paths and configuration identity. Validation covered 452 toolkit tests. The local
replay evidence is in the MarineCast workspace under `Reports/viewshed-map-rendering-2026-11-05`.

The subsequent full eligible regional run validated 12,582,091 source-role pairs in **3h 4m 9s**
from retained preparation through final checks. The [regional run report](reports/full-regional-run-2026-11-05.md)
records the actual cohort, the one pre-LOS retry, and stage timings.
