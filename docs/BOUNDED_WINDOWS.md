# Bounded native raster windows

`batch.raster_stack_mode: windowed` opts the existing source runner into a bounded canonical raster stack. The default remains `global`. Each source-owned batch retains the unchanged observer sample design and the complete observer LOS buffer plus margin. Do not partition observers or clip candidate targets to a tile core. Keep each `(source_h3, target_h3, source_type)` once, and retain complete reporting-water H3 area denominators. Native/source water masks continue to control endpoint elevations; a separate reporting mask controls aggregation.

Aligned source DEMs on the configured north-up projected grid are copied exactly. Cross-CRS or resolution-changing DEMs can instead replay a pinned producer receipt of the **original global GDAL processing chunks**:

```yaml
batch:
  raster_stack_mode: windowed
  warp_chunk_plan_path: /absolute/path/to/reviewed-global-warp-chunks.json
  warp_chunk_plan_sha256: <SHA256 of exact receipt bytes>
```

A relative receipt path resolves from the toolkit YAML directory. The receipt must match the exact source checksum and size, calculated global destination transform/shape/CRS, destination block size, GDAL/Rasterio versions and established method options. Its chunks must cover the complete global grid exactly once. Arbitrary administrative tile rectangles cannot replace a producer chunk partition. The producer's reference output checksum is retained as lineage. Pinning and structural validation cannot independently prove that a receipt came from the stated producer: inspect the native trace and reference parity before approving it.

For a requested LOS window the engine computes only intersecting original warp chunks, retains the entire original source dataset as context, and crops those chunk results into the requested window. It preserves bilinear resampling, the existing 0.125-source-pixel approximate transformer tolerance, one worker and the original 64 MiB warp-memory limit. It does not introduce a fixed resampling scale, coordinate rounding, exact-transformer substitution or new nodata policy. Only a bounded window is written; no new full regional DEM is materialized. Source/grid/receipt/runtime identities and computed chunks enter the cache fingerprint. Stale or incomplete outputs are preserved and rejected. Context-chunk allocation is checked before any output creation, including on cache reuse.

The retained Dungeness diagnosis isolates the earlier 6.2440643 m error. The destination transform and bilinear resampler were already identical. The global GeoTIFF warp used destination chunks `(0,0,1316,512)` and `(0,512,1316,817)`. A small VRT read used three `512 x 128` chunks, and a full VRT read used `1316 x 664` and `1316 x 665` chunks. GDAL computes sampling scales and approximate coordinate interpolation in each processing context. Its [warp options](https://gdal.org/en/stable/doxygen/structGDALWarpOptions.html) document per-chunk scale effects, and the [installed-version kernel source](https://github.com/OSGeo/gdal/blob/v3.12.3/alg/gdalwarpkernel.cpp#L967) shows those scales controlling the resampling footprint. Increasing `SOURCE_EXTRA` to 32 did not reduce the error, and replacing the approximate transformer did not eliminate it. With the original chunk held fixed, a near-exact-transform diagnostic differed by up to 0.3071156 m; an arbitrary crop context still differed by 2.7452221 m with that near-exact transform. Matching the original complete processing chunks and unchanged tolerance with full source context produced exact pixels/nodata across both complete chunks and five retained small windows, including the chunk boundary and source edges. No science settings changed to obtain that parity.

An optional native helper can now generate a producer chunk partition without decoding source raster pixels or storing a full regional destination. Build it explicitly against the same GDAL release as Rasterio:

```bash
python scripts/build_native_warp_helper.py --output /absolute/task/path/native-warp-plan --gdal-config /absolute/environment/bin/gdal-config
```

Pin the returned binary checksum using `batch.native_warp_helper_path` and `batch.native_warp_helper_sha256`. The build is never implicit. The helper opens a single affine source for metadata, rejects external/alpha masks and non-affine georeferencing, clones its band/grid/nodata metadata, closes the actual source, and calls the installed GDAL warp operation on synthetic nodata buffers. The synthetic destination discards pixels and records the native write windows. Sampling values do not select the processing chunks. The helper preserves native bilinear/maximum kernels, the original approximate transformer and default 64 MiB warp memory. Schema v2 receipts bind source bytes, dtype/nodata/mask flags, destination dtype/nodata, runtime, helper source and executable identities. Raw file bytes are still read separately to establish source checksums.

The helper child is monitored at 512 MiB RSS and 60 seconds; combined calling-process plus helper RSS is monitored at 768 MiB. Requests, logs and failures remain in the owned cache. These helper limits do not supervise a complete LOS run. A pinned canopy plan can alternatively be supplied using `batch.canopy_warp_chunk_plan_path` and `batch.canopy_warp_chunk_plan_sha256`. Canopy replay uses the unchanged maximum resampler on the DEM producer's full destination grid, Float32 output and NaN destination nodata; a missing source canopy pixel is not converted to zero. Already aligned canopy is read with its source mask and normalizes missing values to NaN. Co-registration of source DEM/CHM is no longer required when their individual original warp plans are qualified.

Both cached regional grids produced 64 metadata-clone chunks on a 17,771 × 17,413 projected grid. The integrated helper runs took approximately 2.6 seconds for DEM and 6.4 seconds for canopy, with combined RSS below 455 MiB. Synthetic native trace comparisons include a multi-chunk canopy case and boundary-crossing crops. A retained 512 × 512 cached canopy subset has exact parity across three windows, including an all-nodata window. These are software checks only; no full regional reference or real LOS pilot ran. The cached rasters remain unqualified for source dates, vertical reference and expanded coastal coverage. ETH canopy represents annual 2020 conditions, not a reconstruction of 2009; canopy `source_year` metadata now retains its annual interval with null exact date. No January 1 observation date is invented.

Set one source per batch, one worker and explicit pixel/memory guardrails. Allocation guards reject oversized windows before output allocation. The advanced `pipeline.api.pilot.run_bounded_land_pilot` API now supervises one selected land R7 source on matched bare-earth/canopy surfaces. It retains 5–10 observer samples, 30 m resolution, 30 km LOS plus 1 km margin and up to 6,000 complete selected candidate pairs. The runner accepts an explicit source selection without changing that source's target universe.

The POSIX supervisor enforces fail-stop sampled ceilings of 768 MiB combined parent/descendant RSS, 2 GiB total retained task staging, 45 minutes and 20 native attempts (including fallback retries). Admission is locked and recorded before each GDAL call; cap rejection cannot fall through to another backend. A process group owns native descendants, including ones left after normal root exit. Timeouts, monitoring denial and interruptions preserve checkpoints and partial evidence; no automatic file deletion occurs. Polling can overshoot a ceiling between samples and is not an operating-system quota. Only trusted commands and contained mutable paths are supported. Process completion is engineering evidence, not pair completeness or scientific qualification: independently check all expected pairs, statuses, denominators, nulls, provenance and parity before any promotion.

Current land policy requests 5–10 samples per R7 source cell; water policy uses 3. Eight land sources can require up to 160 matched bare-earth/canopy GDAL calls, rather than the earlier estimate of 48. Preserve this policy. Actual source counts and timing must come from approved geometry, qualified data and a reviewed capped pilot. The old 5,424-pair demo is not regional performance or completeness evidence.

Memory-capped synthetic warp and resolution-changing H3 parity fixtures run in fresh processes so unrelated GDAL/Numba suite caches do not consume the production parent-plus-helper RSS budget. Production caps remain unchanged.

Synthetic integration tests compare observer frames and all scientific H3 kernel fields exactly for land bare-earth, land canopy and water, using both aligned and resolution-changing stacks. Additional tests compare native GDAL visibility and absolute pixel offsets, preserve stale/incomplete cache evidence, reject absent/changed producer receipts, invalid chunk partitions/method changes and oversized windows or original context chunks, and independently count every required rotated/sheared mapped-land pixel. The retained mismatch is frozen separately with input/output checksums. Neither test class qualifies real source provenance, historical vintages, coastal coverage or final null completeness.

Cross-CRS synthetic tests use both geographic and projected inputs with absent, finite-sentinel and NaN nodata, and compare replayed windows exactly to the unchanged global producer. The retained Dungeness test records zero remaining pixel/nodata/transform differences and about 403 MiB peak RSS for its five-window cold qualification run. These fixture timings do not predict regional throughput.

## Regional release planning and higher-resolution TODO

Policy approval alone does not materialize the reporting mask or owner-pinned R7 registry. Run
`plan-study` first, inspect domain/mask/registry status and exact membership identities, then
qualify retained sources before acquisition. Do not substitute a planning rectangle, a different
resolution's reporting grid, or a historical demo's pair universe. Source eligibility extends
outside reporting water; preserve all selected land/water roles and full target-water denominators.
The path-support contract buffers reporting water by 31 km. A source's complete observer LOS AOI
also extends 31 km from its sampled points; outer observers can therefore require computation
beyond the reporting path-support envelope. Estimate both extents before provision or execution.

A first real pilot should select one land R7 source and retain normal 5-10 observer sampling,
30 m raster resolution, 30 km LOS plus 1 km margin, and all its selected candidate pairs. Its
matched bare-earth/canopy work needs at most 20 native calls before any retries. Use one worker,
no more than 6,000 role pairs, and stop ceilings of 45 minutes, 768 MiB RSS and 2 GiB total task
staging. A 62 km square contains approximately 4.27 million 30 m pixels, or 17 MiB per Float32
plane; original context chunks, several aligned surfaces, indexing and checkpoints increase this.
Budget 0.25-2 GiB of staging and measure real RSS/runtime before scaling. These are planning
bounds, not measured throughput or proof that inputs qualify. Use the run-level pilot supervisor
in addition to the native helper monitor; the latter alone does not cover a complete LOS run.

Missing raster pixels over mapped land remain missing. Assess them against the explicitly qualified source-relative
land/water partition; physical-perfect or legal coastline certification is not required for a
research product. Retain generalized shoreline, incomplete island/freshwater and outer-halo
limitations. Do not fill missing pixels with zero canopy or move an observer to a convenient cell. Pin upstream source bytes, native masks, projection/warp settings,
vertical references, source-date precision and licences. An unverified DEM datum blocks accuracy
claims; any source-relative engineering experiment needs explicit recorded qualification and
cannot be relabelled a regional scientific release. Annual 2020 CHM and current reference geometry
are static references, not daily reconstructions over a requested historical window.

Higher-resolution follow-up requires qualified island/shoreline detail, upstream DEM tile
provenance and datum reconciliation, complete above-ground canopy support, native pixel/observer
parity against the established producer, and a fresh resource estimate. Record coverage/status and
unknown values in pair and per-target products. Sums express aggregate static support, never
probability, observer activity or daily historical availability. Preserve earlier local releases;
promote a new immutable generation under `Data/viewshed` only after independent completeness,
null, denominator, checksum and provenance checks.
