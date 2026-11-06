# Data acquisition

`RasterProvider` exposes typed `discover(bbox, config)` and `download(assets, destination)` methods.
The registry supplies `usgs_3dep`, `global_canopy_height`, and `local`; `register_provider(name,
provider)` installs another implementation and registers its configuration name.

- USGS delegates chunk retrieval to the existing py3dep implementation, backed by the
  [3DEP elevation service](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer).
- CHM uses the existing deterministic ETH 2020 tile naming and URLs from the
  [global canopy-height project](https://langnico.github.io/globalcanopyheight/).

Acquisition expands the modeling bbox by the target-distance and AOI margin in projected meters.
Downloads write unique temporary files, validate single-band georeferencing, verify optional
source checksums, then atomically replace the cache entry. Cache hits require matching asset
identity and output SHA-256. Mutated local inputs change cache identity; corrupt downloads are
retrieved again. Failed downloads never expose partial final rasters.

A supplied `Asset.checksum` is the SHA-256 of the source file's raw bytes; temporary filenames
are excluded. `prepare-dem` and `prepare-chm` compare the download manifest's normalized dataset
settings and buffered acquisition bounds with the current request. Changed assets, provider
settings or extent require a fresh download manifest before preparation proceeds.

Each source asset retains its URL/ID, observed download time, and checksum sidecar. The download
manifest records provider settings, requested/source bounds, toolkit config hash, paths, and input
checksums. Prepared and weight outputs record software/algorithm versions, configuration hash,
CRS, analysis resolution, bbox, H3 resolutions, input hashes, and output hash.

`version: live` is not an immutable upstream USGS release. Reproduction requires preserving the
cached source bytes and manifests, or replacing live acquisition with pinned local assets and
checksums. Local checksum validation cannot prove that an unpinned remote service has not changed.

The refactor's automated integration uses synthetic local rasters. The bounded
[San Juan Islands demo](san-juan-demo.md) additionally exercises live USGS acquisition and ETH
COG window retrieval, with an explicit coverage audit. This does not establish Canadian-domain
coverage or validate the full Salish Sea study. Inspect source coverage, vertical units/datums,
vintages, and licenses before regional promotion. General workflows require configured land/water
geometry; the demo uses the existing case-study geometry helper.

## Source checksum compatibility

New download manifests declare `manifest_schema_version: 2` and
`observed_checksum_algorithm: sha256_raw_bytes`. Each asset's `observed_sha256` is the raw file
SHA-256 and can be reused as a pinned provider checksum. `artifact_checksum` separately retains
`sha256_filename_then_bytes`, the repository artifact identity used in `checksums` and existing
preparation/cache contracts.

Historical unversioned download manifests used filename-plus-bytes for `observed_sha256`. Do not
copy that value into `datasets.*.checksums` or relabel it as raw SHA-256. Existing preparation
checksums remain compatible; rerun acquisition against preserved local/cached bytes to produce a
versioned manifest. This does not require redownloading a valid cached raster. Historical example
bundles are not retroactively rewritten or certified by this change.

With `land_tiles_only`, canopy selection transforms the acquisition bbox into the land file's
CRS before its spatial read. A missing land CRS is rejected. Geographic and projected land
geometries should select the same tiles.

## Bounded canopy reconciliation evidence

ETH 2020 masks built-up land as nodata as well as some nonland classes. Its missing values do not
establish zero canopy. A [bounded2026-11-04 reconciliation](reports/canopy-resolution-2026-11-04.md)
uses retained NHD open-water evidence and published WRI/Meta heights only for genuine missing land
support, preserving valid ETH values and recording per-pixel provenance/date information. Native
height maxima are reduced to the configured 30 m grid. This is a mixed-source/mixed-vintage local
benchmark recipe; it does not add a registered provider, migrate the pinned study, or qualify a
uniform 2020 regional product. Source availability, nominal water mapping and field accuracy remain
distinct checks. See that report for auxiliary-mask ambiguity, withheld source cells and replay.

## Regional US/Canadian qualification recipe

The [2026-11-04 regional qualification](reports/regional-qualification-2026-11-04.md) retains
NRCan MRDEM30 **DTM** and native source classes for Canadian coverage, with USGS bare-earth
NAVD88 heights explicitly converted to CGVD2013 using pinned geodetic grids. Canadian fallback
from unqualified USGS border pixels is rejected. The local recipe supplements masked ETH canopy
with native WRI/Meta maxima only inside conservative independently dated imagery support, and
records mixed source/vintage and auxiliary-mask limitations. Independent BC/NHD nominal water
separates shoreline omissions from missing land heights. This is a retained local acquisition
recipe, not a newly registered provider or a uniform-date/field-calibrated terrain-canopy product.

Use `datasets.dem/chm.provider: local`, explicit `assets`, an honest mixed-source `version`, and
source checksums when reusing these prepared inputs through the component acquisition workflow.
Keep source/cache receipts and categorical provenance companions with them. The analysis still
uses the configured 30 m grid and existing observer sampling; native retrieval and work tiling
do not change these scientific settings. Coverage qualifications and benchmark eligibility must
be checked before full execution; the dated report records the actually executed scope.
