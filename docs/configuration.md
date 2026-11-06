# Configuration

The YAML remains one file. Existing `run`, `region`, `viewshed`, `h3`, `batch`, `raster`,
`distance_weight`, `paths`, and visualization sections preserve their established meanings.
This refactor adds strict Pydantic sections for `datasets` and `composition`; it does not claim
that every historical dictionary has been migrated to Pydantic. Unknown fields in these new
sections and unregistered providers are errors.

```yaml
datasets:
  dem:
    provider: usgs_3dep
    enabled: true
    resolution_m: 30
    version: live
    cache: true
  chm:
    provider: global_canopy_height
    enabled: true
    resolution_m: 10
    version: "2020"
    cache: true
composition:
  model: distance_integrated_los
  land: [terrain, canopy]
  water: [terrain]
```

Dataset `resolution_m` describes requested source resolution. `viewshed.dem_resolution_m` defines
the common prepared analysis grid. CHM uses the configured canopy resampling policy, normally
`max`; DEM preparation uses bilinear interpolation. All source tiles in a mosaic must share CRS.
Missing ETH canopy value 255 is explicitly interpreted as nodata during preparation.

## Physical validation and backends

Configuration loading validates the base `viewshed` settings before deriving runtime paths or
starting acquisition. The following values must be finite:

| Constraint | Settings |
|---|---|
| Positive | `dem_resolution_m` (also an integer), `max_distance_m`, `earth_radius_m`, `dem_nodata_barrier_height_m` |
| Nonnegative | `observer_eye_height_m`, `target_height_m`, `aoi_margin_m`, `minimum_canopy_height_m`, `observer_canopy_clearance_radius_m` |
| Finite, with no additional sign restriction | `sea_level_m`, `curvature_coefficient` |

`crs_projected` must be a projected CRS whose horizontal axes use metres. Geographic coordinates
and projected feet are rejected; the toolkit does not silently convert physical parameters into
those units. Zero base observer/target heights and zero curvature remain supported. Applying a
water source policy still requires positive heights for its selected observer class and target.

Use `backend: gdal` for canopy and paired land builds. The experimental `xrspatial` backend does
not implement equivalent observer canopy clearance, so canopy requests are rejected before LOS;
dependency-resolved land canopy builds reject this combination before acquisition. Bare-earth
xrspatial remains experimental and is not a validated interchangeable performance option.

The November 4, 2026 fixes leave the configured raster resolution, source sample counts and
sampling modes unchanged. See [scientific methodology](scientific-methodology.md) for the
corrected multipart ordering, water range boundary and backend alignment contracts. Changing
`batch.max_workers` alone can reuse valid terrain source partitions; other batch settings remain
in partition identity. Run manifests still check their full logical-run identity, so tuning a
configuration can require a new `run_id` even when its terrain partitions remain reusable.

## Acquisition and distance products

### Cache retention and cleanup

`run.cleanup_policy` accepts `retain_reusable` (the default) or `compact`. After a successful
established `run_viewshed`/complete `process` workflow, `retain_reusable` preserves prepared
rasters and canonical domain/pixel indexes, source/target geometry, lookup/area and factor
tables, paired land LOS checkpoints, configured water LOS partitions/manifests and distance
checkpoints. Other disposable scratch under `paths.output_dir` can be cleaned only after final
validation. Retention consumes disk space; it is not a cache-validity claim. Owning stages still
verify source bytes, algorithms, grids, schemas and checksums before reuse.

`compact` preserves durable static/observation-geometry outputs and their companions while
pruning rebuildable work. Both policies protect explicitly configured source inputs, local dataset
assets and their metadata, including sources located within the output root. External source/cache
stores are never cleanup candidates. Neither policy moves existing inputs or performs eviction.
Explicit component builds continue to have no implicit cleanup. A cleanup-policy change does not
invalidate terrain partitions; changing a logical run configuration can still require a new run ID.

Clipped vector unions and land/water domains are cached under `geometry_cache/` in the owning work
directory. Reuse binds source bytes (including shapefile companions), exact domain extent, GEOS
and algorithm identity, plus the cached WKB checksum. Changed inputs invalidate reuse; corrupt
caches rebuild. Unconfigured/fallback sources retain the original preparation path.
The existing `water_viewing.land_mask.land_buffer_m` key sets the exact planar clearance
radius; the default remains 30 m. See [water-source methodology](scientific-methodology.md#water-source-opaque-land-kernel).

### Marine sea-level assignment

`paths.marine_water_mask_path` is optional. Supply a qualified single-band raster on the exact
projected DEM grid: values must be 0 or 1, nodata must be absent or 0, and every positive pixel
must also be in the canonical all-water mask. Only 1 receives `viewshed.sea_level_m`. Zero means
no marine evidence; finite DEM heights are preserved. Missing or invalid configured masks fail
before land LOS. Water-source opaque-land preparation does not require this land endpoint input.

Without this setting, all DEM elevations are preserved before the normal endpoint repair and
DEM-gap policy. Generic water polygons no longer flatten lakes or rivers to sea level. Qualify
marine evidence against inland lakes/rivers at coast and river-mouth overlaps; altitude thresholds
and connectivity to the ocean do not establish a marine classification. The
[regional correction report](reports/inland-water-elevation-2026-11-04.md) records the retained
GSHHG/NHD/BC recipe used for the November 4 benchmark; it is not an automatic acquisition provider.

Marine mask bytes and the v4 canonical-stack/water-elevation algorithms participate in land
cache identity, including when the optional path is absent. Earlier land endpoints, partitions
and completed stages must be recomputed. Resolution, observer sampling, native H3 support,
all-water target geometry and canopy exclusion retain their configured meanings.

For pinned/offline inputs use `provider: local`, `assets: [path/to/raster.tif]`, a descriptive
`version`, and optionally one lowercase SHA-256 `checksums` entry per asset. HTTP asset URLs can
also be supplied to the real providers. `enabled: false` prevents an explicit download or prepare
stage; it does not invent neutral physical support.

USGS discovery uses the existing py3dep WCS implementation and rejects unsupported endpoint
replacement. ETH's optional `endpoint` replaces its download base URL. New provider registration
can add other discovery/download behavior without changing orchestration.

Distance retains the existing `selected_model`, logistic parameters, exponential scale, piecewise
thresholds, normalization, and hard cutoff. Unsupported formulas/factor selections are rejected;
arbitrary mathematical strings are not evaluated. Gaussian decay has not been added.

That YAML `distance_weight` block remains the integrated LOS/default-component configuration.
Standalone sensitivity profiles are passed explicitly as `DistanceProfile` objects or
`build-distance-profile` CLI arguments; they are not written back into YAML and cannot silently
replace the integrated settings. Effective defaults, including an inherited cutoff, are resolved
before a profile identity is calculated. See [distance products](distance-products.md).

Use one bbox definition. `area: model_area` resolves the shared packaged OrcaCast SRKW domain.
A custom region instead supplies a mapping at `region.bbox_wgs84`. Configure all input/output
paths, including land and water polygons. Keep durable output separate from the work directory.
Loading configuration remains read-only. For released study geometry, set `shared_study.config_path`
to the owner's checksum-pinned registry config. It selects the R7 `water_reporting`, `land_source`,
and `water_source` memberships and verifies their physical context. Use `--study-config` or
`MARINECAST_STUDY_CONFIG` for the separate [proposed-study reporting adapter](shared-study.md).
Select one mode per run. A [component dry run](api.md) performs metadata inspection after
configuration and membership validation.

Standalone distance products require `paths.final_output_dir` outside `paths.output_dir`. This
prevents the established complete-workflow cleanup from silently deleting durable pair distances
or profiles.
