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
