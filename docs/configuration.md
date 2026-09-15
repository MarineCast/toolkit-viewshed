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
Loading configuration remains read-only.

Standalone distance products require `paths.final_output_dir` outside `paths.output_dir`. This
prevents the established complete-workflow cleanup from silently deleting durable pair distances
or profiles.
