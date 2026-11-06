# Regional input search, bounded collection and coverage — 2026-11-04

**Historical collection snapshot.** The [subsequent canopy reconciliation and paired benchmark](canopy-resolution-2026-11-04.md)
resolved the bounded input gaps using independent water evidence and mixed-source canopy. The
missing-coverage findings below describe the original collection before that reconciliation.

The historical full-region DEM and canopy files were not found in accessible local storage.
A new bounded Puget Sound collection produced **complete DEM coverage for eight source windows**,
but the new canopy still has **6.3–7.4% nodata over model-defined nonwater support**. No candidate
passes joint DEM/canopy qualification. A reliable full paired-viewshed runtime estimate remains
unavailable; the earlier **4–8-hour budget must not be used for scheduling**.

## Search and acquisition

The search covered accessible user-home folders, local worktrees/caches, temporary retained files,
mounted-volume inventory and Spotlight exact-filename lookup. It enumerated 1,690 raster/archive
paths; protected directories could not be inspected. No external data volume was mounted. The
inventory's old `DEM_30M.tif` (326 MB) and `CHM_30M.tif` (119 MB) paths are absent. This does not
prove that no backup exists elsewhere. Only relevant GIS candidate names were retained in evidence.
The user confirmed no known external archive and requested collection.

New collection used the existing [USGS 3DEP service](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer)
through py3dep at 30 m, and native windows from the existing
[ETH 2020 canopy product](https://langnico.github.io/globalcanopyheight/) at 10 m. ETH requires
CC BY 4.0 attribution. The USGS live service is not an immutable release. Source URLs, request
bounds, access dates, sizes and SHA-256 hashes are retained with the downloaded files. Total raw
files are 66,160,188 bytes. Source bbox is `[-122.56, 47.56, -122.50, 47.62]`; acquisition expands
it by 33 km in EPSG:32610 to enclose all source-cell polygons plus the required LOS/margin.
No whole regional tile archive was downloaded.

Raw DEM is retained in the configured `data/tmp/elevation/benchmark-puget-20261104/` cache;
canopy is in `data/raw/canopy/eth_10m/windows/`, relative to the owner checkout. Derived staging
is under the workspace `.marinecast-work/viewshed/covered-benchmark-20261104/`. Existing source
stores and delivered `Data/` products were preserved. Raster warping used bilinear DEM and
maximum canopy height at 30 m. H3 R7, 30 km radius, 1 km margin, full pixels/stride1, adaptive
5–10 land observers, curvature, heights and 14-worker/seven-source batch settings were retained.

## Pixel coverage qualification

Qualification tests the complete source-land polygon buffered by **31 km**, including possible
observer positions, LOS radius and margin. Its footprint must lie inside the prepared raster.
Every nonwater support pixel must have finite, nonnodata DEM and canopy. Native canopy sentinel
255 remains missing. No zero-canopy or opaque-DEM assumption is accepted as observed coverage.
The water exemption is explicitly relative to the current physical-water context and production
water mask, not a claim of perfect coastline accuracy.

| Inputs | Source windows tested | DEM gaps | Canopy gaps | Jointly qualified |
| --- | ---: | --- | --- | ---: |
| Retained San Juan inputs | 45 | 48.4–63.4% of nonwater support | 51.7–65.8% | 0 |
| New Puget collection | 8 | **0 pixels in every window** | 6.3–7.4% | **0** |

The newly collected canopy encloses the full required extent: these are internal nodata/mask
issues, not missing download edges. Cross-checking retained NHDWaterbody lake/pond/reservoir
polygons explains some nodata, but leaves **142,877–152,899 pixels/window** unexplained by that
specific reference (see JSON for exact per-source counts). This is not proof that all remaining
pixels are terrestrial canopy gaps; marine water, other hydrography, shoreline mismatch and
product missingness still need reconciliation. NHD wetland polygons were not treated as open
water. The production water geometry was not changed, and no missing canopy was called observed
zero. More geographic extent alone will not fix these internal gaps.

## Verified terrain-only benchmark

The verified DEM supports **8 sources, 54 observers and 2,348 candidate reporting pairs**. Three
fresh terrain partition executions took **2.537, 1.159 and 1.167 seconds**. Preparation already
existed before timing; these are terrain-runner timings, not cold acquisition/preparation or
complete pipeline times. Every source completed successfully in every trial. All 16 repeated
source-partition comparisons matched exactly on keys, terrain weights and LOS fractions. Sampled
parent RSS peaked near 1.25 GiB; process-tree sampling was partial.

Scaling to the retained 241,307 requested land observers gives **1.44–1.45 hours warm**, or
3.15 hours using the first terrain trial. This is a terrain-only linear projection from one
small Puget area; it cannot establish canopy runtime, cross-region preparation cost or a full
completion ETA. The previous water-kernel timing and incomplete-data paired benchmark remain
historical evidence. No new canopy benchmark was run because no window qualified.

## Full regional collection requirements

A conservative envelope around retained land sources plus 31 km is about **23,867 × 19,475**
pixels at 30 m: **1.73 GiB per Float32 layer**, 3.46 GiB for DEM+CHM, before compression.
A simple six-Float32/four-byte-array planning inventory is about 12.12 GiB, excluding raw inputs,
outputs, HTTP caches and processing overhead. This is a disk/storage envelope, not a runtime RAM
allocation or download-size prediction. Native 10 m canopy requires about 3.90 GiB for a
single-byte uncompressed envelope. Chunked acquisition should reference existing windows and
validate coverage before promotion. About 66.4 GiB remained free after bounded collection.

US terrain can use the existing 3DEP provider. Canadian terrain needs a qualified bare-earth
source and coverage audit. [NRCan HRDEM](https://open.canada.ca/data/en/dataset/957782bf-847c-4644-a757-e383c0057995)
provides separate DTM/DSM products and uses CGVD2013; DTM footprints, source vintages, rights and
vertical-reference compatibility need checking before a cross-border mosaic. A surface model
must not silently replace bare earth. Canadian provider integration was not implemented here.

Broad regional collection was not started: the bounded acquisition exposes a canopy/water-mask
qualification issue that must be resolved before downloading and qualifying the full region.
The next necessary work is to identify the unexplained canopy gaps against appropriate physical
water/land evidence and obtain valid canopy observations for genuine land gaps. Any accepted
imputation policy must remain explicit and cannot be described as complete observed coverage.

## Evidence and validation

Workspace `Reports/viewshed-coverage-benchmark-2026-11-04/` retains collection/audit/replay scripts,
source manifests, per-source gap counts, NHD attribution, three timing trials, validation summary
and storage plan. `validate.py` verifies source hashes, all 24 completion records, exact repeat
partition values, and unchanged physical/sampling/batch settings against the canonical config.
Scripts parse; local documentation links and `git diff --check` were checked. The previous
391-test implementation validation was not rerun for this acquisition/evidence-only work.

Acquisition and scripts used the existing compatible Python3.14/GDAL3.12 environment. Raw windows
are retained for reuse, derived staging is about 76 MiB, and no permanent cleanup, regional
publication, Data promotion or commit occurred. See the
[historical benchmark](regional-benchmark-2026-11-04.md) and
[implementation report](implementation-2026-11-04.md) for preceding evidence.
