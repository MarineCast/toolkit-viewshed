# Inland-water elevation correction and paired benchmark — 2026-11-04

The land LOS endpoint raster previously assigned `sea_level_m` to every canonical water pixel,
including elevated lakes and rivers. The corrected implementation preserves finite nonmarine
DEM heights and river gradients. Sea-level assignment now requires an explicit marine mask.
The same regional US/Canadian cohort was rerun at **30 m, native H3 R7, stride 1 and unchanged
adaptive 5–10 observer sampling**. This report supersedes the inland-water limitation and runtime
figures in the [preceding regional report](regional-qualification-2026-11-04.md).

## Implemented behavior and compatibility

- `paths.marine_water_mask_path` is optional. Without it, DEM heights and nodata are preserved
  before the established endpoint repair/gap policy. Generic water labels no longer imply zero.
- An explicit marine mask must be single-band binary 0/1, on the exact projected DEM grid,
  with nodata absent or 0. Positive pixels must be a subset of canonical water. Invalid masks
  fail before land LOS. Sea level may fill a marine DEM void; an inland void remains missing
  before the existing deterministic exact-source/3×3 endpoint repair and configured gap policy.
- Inland water retains the source DEM height on both paired surfaces. Canopy remains excluded
  over all canonical water. There is no lake-wide averaging, river smoothing, altitude threshold,
  or ocean-connectivity inference.
- Canonical stack v4 and `preserve_dem_except_explicit_marine_v1` invalidate old land endpoint,
  partition and component caches, including configurations without the optional mask. Marine
  input bytes participate in partition identity and completed-stage input fingerprints. Verified
  geometry-only water masks can be reused across v3/v4 when water algorithms, source/grid/domain
  identities and actual output bytes match; old endpoint heights are always rebuilt.

Canonical behavior and configuration are documented in [methodology](../scientific-methodology.md)
and [configuration](../configuration.md). The low-level legacy function name and endpoint filename
are retained; they now implement the explicit marine-only policy.

## Retained source qualification

No new download was needed. The regional DEM, CHM, water polygons, observer geometries and native
lookup were reused in place. Terrain remains the previously qualified USGS/NRCan bare-earth
recipe in common CGVD2013 metres; canopy retains its existing mixed-source/mixed-vintage policy.

The explicit marine classification uses the retained **GSHHG 2.3.7 full-resolution L1** archive.
GSHHG documents L1 as the land/ocean boundary and higher levels as lakes/islands nested within
land. The builder fills L1 polygon holes before taking the ocean complement so holes cannot
silently turn inland lakes into ocean. See the [GSHHG project](https://www.soest.hawaii.edu/wessel/gshhg/)
and [NOAA shoreline documentation](https://www.ngdc.noaa.gov/mgg/shorelines/shorelines.html).
The independently qualified BC Texada ocean patch from the preceding work is included.

Mapped inland evidence takes priority at shared coast/river-mouth pixels: NHD FTypes 390
(lake/pond), 436 (reservoir), 460 (stream/river), plus definite BC FWA lake and river polygons.
NHD estuary class 493 is excluded from this inland override. There are 1,465 intersecting GSHHG
L1 features, 22,079 NHD inland features, 43,728 BC lakes and 4,760 BC rivers. Both classes use the
existing all-touched 30 m grid, and the final marine mask is intersected with canonical water.

| Exhaustive grid result | Pixels |
|---|---:|
| Explicit marine | 236,160,260 |
| Mapped inland within canonical water | 7,226,286 |
| Inland evidence overriding marine cartography | 511,079 |
| Canonical water without marine assignment | 7,841,799 |
| Finite nonmarine water heights preserved exactly | 7,799,706 |
| Changed finite nonmarine water heights | **0** |
| Mapped inland pixels lacking a finite source DEM height | **0** |
| Missing required inland endpoint pixels | **0** |
| All required endpoint gaps after existing repair | **0** |
| Water height mismatch between bare/canopy surfaces | **0** |

All **7,226,286 mapped lake/river pixels** retain their finite source DEM values exactly.
The canonical all-water raster is unchanged at every pixel. Every source geometry remains valid,
all 1,229 observer coordinates match the previous benchmark exactly, and no observer intersects
vector water or a water-mask pixel. Marine-mask construction took 24.285 s; canonical preparation
took 23.113 s, or 29.147 s including the exhaustive preservation audit, with the projected DEM
and verified water mask reused.

This is nominal static cartography and source-height preservation. It does not establish
surveyed lake levels, hydrologic consistency, field accuracy, a uniform canopy vintage, or a
validated local tidal datum. Unclassified water conservatively retains the DEM; mixed mouth/coast
pixels with inland evidence do so as well. Missing heights are distinct from observed zero.

## Paired native-GDAL benchmark

The frozen cohort contains **144 sources (87 Canadian / 57 US), 1,229 observers, 33,555 candidate
pairs and 32 geographic/country strata**. Maximum LOS distance remains 30 km plus the existing
1 km preparation margin. The retained 60 m qualification guard only checks preparation support.
Batch size 7, 14 workers, the 12-million-pixel batch cap and 768 MiB estimate guard are unchanged.
Each fresh trial writes distinct physical partitions while reusing the same prepared inputs.

| Trial | Paired elapsed | Native LOS calls |
|---|---:|---:|
| First, including lazy setup | 119.032 s | 2,458 |
| Warm 1 | 63.853 s | 2,458 |
| Warm 2 | 74.552 s | 2,458 |
| Verified partition reuse | 1.872 s | **0** |

All 288 source/surface results succeed in each fresh trial. All **576 repeat partition
comparisons across 22 scientific columns match exactly**, with finite weights bounded to [0, 1]
and unique source/target keys. Parent-only sampled RSS peaks at about 1.65 GiB in the first
trial; process-tree/RSS sampling is partial, so this is not a total memory bound.

The elevation correction changes physical results, as expected: 1,230 bare-earth and 259 canopy
source/target weights differ from blanket flattening. Their maximum absolute changes are
0.525439 and 0.414467. Sparse positive-output rows can appear or disappear: bare-earth gains
208 and loses 28; canopy gains 21 and loses 13. The candidate population, target denominator,
source geometry, resolution and sampling remain fixed. Prior blanket-flattened products must
not be reused as corrected outputs.

Using the retained original active population of 233,225 requested observers, linear scaling
gives **3.37–3.93 hours** of steady paired land LOS. Country/geography post-stratification of
native work, calibrated to measured elapsed time, gives **3.44–4.03 hours**. These are conditional
projections from two warm trials, not confidence intervals or complete workflow ETAs. The full
corrected regional observer census, geographic packing/cache locality, acquisition, initialization,
composition, water-source work and maps remain outside this measurement. The timing variation
does not isolate the cost of the elevation fix.

## Validation and replay

Synthetic regressions cover a 125 m lake, a 50→48→46 m river, marine nodata filled at configured
sea level, inland nodata retained, absent marine evidence, binary/nodata/grid/subset rejection,
and cache/resume invalidation after same-path marine-byte changes. Existing canonical-stack,
sampling, aggregation, GDAL and architecture regressions remain applicable.

The final complete owner suite passes **421 tests, no skips, two existing GeoArrow warnings**.
Ruff over source/tests, Black at line length 100 for all nine changed Python files, the component
quality gate (expanded lint/format and strict mypy), and byte-compilation pass. Component,
case-study and architecture tests are included in the complete suite. No documentation build
configuration is present; affected links and Knowledge properties are checked locally.

Local replay evidence is retained in `Reports/viewshed-inland-water-2026-11-04/` at the MarineCast
workspace root: `build_marine_mask.py`, `prepare_and_validate.py`, `benchmark.py`, `validate.py`,
the classification/endpoint/paired receipts, independent `summary.json`, checks, and the final
staging/checksum inventory. `marine-classification.json` binds the raw GSHHG archive and retained
NHD/BC evidence by SHA-256. The explicit marine raster SHA-256 is
`e40584673a2bccdf1aed99338135eafc7d895addf7cae678d00d01c1c79e59d9`.

The benchmark configuration, evidence rasters and partitions remain task-owned under
`.marinecast-work/viewshed/inland-water-elevation-20261104/`. New versioned canonical rasters live
beside the reused projected DEM under the preceding regional work root, with exact paths recorded
in `canonical-stack.json` and the staging manifest. Existing input files and previous evidence
are retained. No production rebuild, Data/pinned-study promotion, commit, push, redistribution or
permanent cleanup occurred. Source terms and earlier qualification limits continue to apply.
