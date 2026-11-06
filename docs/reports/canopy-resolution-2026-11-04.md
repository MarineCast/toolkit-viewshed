# Canopy gap reconciliation and paired benchmark — 2026-11-04

**Follow-up:** [Source reconciliation and the all-eight benchmark](source-reconciliation-2026-11-04.md)
resolve the five observer/water conflicts below. This report retains the preceding three-cell
snapshot and canopy-source qualifications.

The bounded Puget benchmark's canopy gaps are resolved as **source-data availability**, with
validity and provenance retained. Across the union of all eight original source-support windows,
**82,540 formerly missing pixels are supported as mapped water** and **117,084 missing land
pixels receive valid supplementary canopy-height estimates**. No prior gap remains on required
land support, and every valid ETH height is preserved exactly. This is not field validation of
heights, coastlines or water levels.

The paired benchmark ran on **three qualified source cells and their original 25 observers**:
**31.981 seconds for the first/setup run**, then **1.103 and 1.132 seconds for fresh paired LOS**.
A separate validated reuse took **0.064 seconds with zero LOS calls**. The 30 m grid, R7, full-pixel
aggregation, sampling settings, observer coordinates and physical settings were preserved.
Five other cells were excluded because original observer locations intersect the corrected mapped
water. Their source geometry requires reconciliation; observers were not moved or thinned to
make the benchmark pass.

## Why the gaps occurred

The ETH product deliberately masks built-up, permanent-water, snow and ice pixels to nodata 255;
these are not measurements of zero canopy. See the
[original paper](https://doi.org/10/gtvx2s) and
[ETH source product](https://langnico.github.io/globalcanopyheight/).
The residual gap map showed the urban pattern, including Seattle-area built-up land.

The earlier water comparison considered lakes/reservoirs but omitted NHD estuaries. The
[USGS NHD dictionary](https://www.usgs.gov/ngp-standards-and-specifications/national-hydrography-dataset-nhd-data-dictionary-feature-classes)
defines FType 390 as LakePond, 436 as Reservoir, 493 as Estuary, and 466 as SwampMarsh. The correction
uses retained NHDWaterbody geometry for 390/436/493, excludes intermittent lake FCode 39001, and
subtracts mapped swamp/marsh overlaps from new additions. Existing physical-water geometry is
retained. A marsh is not declared clear open water because canopy is missing.

Every added water pixel within the audited eight-source support has independent NHD geometry
evidence. Two full-grid differences outside that support are retained as unvalidated; the whole
prepared rectangle is not certified.

The original physical-water context is supplemented with this independently supported nominal
water geometry in a separate benchmark input. Source records, feature identifiers, FTypes/Fcodes,
modification dates and source-archive checksum are retained. This is mapped static water, not an
assertion of the exact 2020 shoreline or water level. The existing configured water-surface policy
still applies, including sea-level flattening; lake-level realism was not newly validated.

## Supplementary land canopy

Land gaps use the published
[WRI/Meta global v6 canopy-height product](https://registry.opendata.aws/dataforgood-fb-forests/),
with [provider documentation](https://github.com/facebookresearch/HighResCanopyHeight).
It supplies modeled canopy height above ground in metres. Native pixels were range-read and
maximum-aggregated to the existing 30 m grid, with no overviews used for height reduction.
Supplementation is restricted to missing ETH values on nonwater pixels. Existing ETH values are
never averaged, smoothed or replaced. Valid supplementary zeros remain source-model predictions;
unknown values are not filled with a literal zero.

Accepted availability requires finite main-product heights in `[0,255)`, the published main
GeoTIFF's GDAL validity contract, and a complete 30 m pixel inside independently dated imagery
coverage. The joined coverage footprint is eroded 24 m to allow for the output half-diagonal and
native-pixel support. Joining adjacent tile/date footprints **before** erosion prevents artificial
seam gaps; per-tile erosion initially created 651 false gaps, corrected before benchmarking.
An independent coverage check confirms every accepted fill is inside the dated imagery footprint.

The provider's separately published auxiliary `.tif.msk` files are not adjacent sidecars to the
CHM URLs and have undocumented/conflicting validity semantics in this area: interpreting them
as ordinary valid-data masks rejects almost all of the east Puget tiles despite dated imagery
coverage. They are retained for audit and **are not used to define accepted validity**. Direct
probes of all four published main CHMs confirm metre units, no declared nodata, and GDAL's
all-valid mask. The extra dated-footprint gate prevents a bare zero value from establishing
coverage. This source-policy qualification remains distinct from empirical height accuracy or
unresolved auxiliary quality-mask meaning.

The accepted fill's image-date labels span 2017–2019, predominantly August 2019; source-tile
metadata extends to 2016 elsewhere. The composite is **mixed-source and mixed-vintage**, not a
restored unmasked ETH 2020 product or a uniform 2020 observation. A date raster records the
provider image date at the output-pixel center. Native pixels contributing the 30 m maximum can
span date boundaries, so the exact date of the maximum is not claimed. Source masks, date-code
legend, URLs, ETags, metadata checksums, output checksums and software/code hashes are retained.
WRI/Meta maps and ETH data require CC BY 4.0 attribution.

## Coverage and sampling validation

| Check | Result |
| --- | --- |
| Original missing pixels in the eight-source support union | 199,624 |
| Reclassified as independently supported nominal water | 82,540 |
| Missing land pixels filled from valid supplementary height estimates | 117,084 |
| Remaining prior gaps on required land | **0** |
| Previously modeled water pixels removed | **0** |
| All eight source supports have complete DEM and accepted canopy data | **Yes** |
| Original sources with nonconflicting unchanged observer locations | **3** |
| Observer coordinates or counts changed for accepted sources | **None** |

Required support is each complete retained source-land polygon plus 31 km: the 30 km viewing radius
and 1 km margin. Raster extent containment, finite DEM, accepted canopy coverage, CRS, exact grid
alignment and source-point water conflicts are checked before execution. Per-pixel codes distinguish
ETH heights(1), supplementary land heights(2), mapped water/not-applicable(3), and unresolved(255).
The original five withheld source IDs and per-source checks are in `resolution.json`.

The correction changes physical-water support and its canonical water-area denominators while
retaining selected target IDs/pair keys. Old source fractions/geometry are not silently rewritten.
The three accepted cells retain the same source geometry, sampling method, counts and exact
observer coordinates. The pinned shared-study registry, delivered source cells and original
physical-water context were not modified.

## Measured paired execution

| Trial | Work | Wall seconds | Native LOS calls |
| --- | --- | ---: | ---: |
| First/setup | 3 sources, 25 observers, 901 candidate pairs,both surfaces | 31.981 | 50 |
| Fresh compute, prepared inputs | Same | 1.103 | 50 |
| Fresh compute, prepared inputs | Same | 1.132 | 50 |
| Verified source-partition reuse | Same | 0.064 | **0** |

Every fresh trial recomputed partitions in independent trial directories. All source/surface
completion records succeeded. All12 corresponding repeated physical partition comparisons matched
exactly on pair keys, terrain weights and LOS fractions. Reuse validated the existing contracts
and reported `skipped_existing` for all six source/surface partitions. Sampled parent RSS reached
about 2.18 GiB; process-tree sampling was partial, so this is not a hard total-memory bound.

At the retained nominal 241,307 requested land observers, steady-state linear scaling gives
**2.96–3.04 hours for paired land LOS**. The first/setup 31.981 seconds must not be charged to every
25 observers. The warm rate is a convenience sample from one small Puget area, not a statistical
confidence interval or full-region trial. Corrected source populations, diverse regional terrain,
Canadian inputs, regional raster/geometry/lookup preparation, composition, water-source execution,
and maps remain outside this timing. A complete wall-clock ETA is still unmeasured; this result
supports roughly 3 hours of steady paired land computation after inputs/preparation are ready.

## Retained artifacts and replay

Workspace `Reports/viewshed-canopy-resolution-2026-11-04/` contains diagnosis, acquisition,
resolution, benchmark and validation scripts plus small QA evidence and manifests. The reusable
resolved inputs and runtime configuration are in
`.marinecast-work/viewshed/canopy-resolution-20261104/`. The owner raw cache under
`data/raw/canopy/meta_wri/v6/` retains bounded height windows, source coverage/date metadata and
auxiliary masks; original Puget DEM/ETH and NHD sources are referenced in place.

From the MarineCast workspace root with the compatible environment, the prepared-input replay is:

```bash
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-canopy-resolution-2026-11-04/benchmark.py
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-canopy-resolution-2026-11-04/validate.py
```

Replay replaces task trial results and recomputes fresh LOS; preserve earlier evidence before
rerunning. The method is a scripted benchmark input reconciliation, not a new registered provider
or an automatic shared-study migration. The generic provider registry and canonical configuration
are unchanged. Raw sources and output hashes are recorded for reuse.

The one-time native supplementary range read took 483 seconds and produced a retained height window
of about 5.5 MB. Raw provider metadata/windows total about 161 MiB; task staging about 292 MiB, within
the planned 2 GiB cap, with about 64 GiB free. No permanent cleanup, regional publication, Data product
promotion or commit occurred. Source code is unchanged from the preceding 391-test implementation
validation; this work validates new input data and native paired runs, not a new whole-region build.

See the [preceding collection audit](coverage-collection-2026-11-04.md) for historical gaps and
Canadian collection requirements. The source-placement disagreements are resolved in the
[source-reconciliation follow-up](source-reconciliation-2026-11-04.md). Auxiliary-mask semantics
and mixed vintage still limit general regional promotion and uniform-vintage modeling claims.
