# San Juan instructional dataset card

This card summarizes the **committed instructional bundle**, not a new source-data audit.
The machine-readable owner is [manifest.json](assets/examples/san-juan/manifest.json), accompanied by
[coverage.json](assets/examples/san-juan/coverage.json) and
[production-evidence.json](assets/examples/san-juan/production-evidence.json).

| Property | Recorded scope |
| --- | --- |
| Purpose | Bounded explanation of modeled static physical viewing support |
| Area | San Juan Islands configuration; not a full Salish Sea acceptance release |
| Elevation | USGS 3DEP inputs; preserve cached bytes and acquisition identity for reproduction |
| Canopy | ETH Global Canopy Height 2020; source year is a product vintage, not a current observation |
| Coastline | Natural Earth 1:10m land and derived water geometry |
| Analysis | EPSG:32610, 100 m analysis grid, H3 resolution 7 in this committed example |
| Heights | Metres; DEM elevation plus canopy height used for canopy surface, with configured observer clearance |
| Vertical reference | Do not infer a harmonized vertical datum from horizontal EPSG or metre units. Independent datum/accuracy qualification is not established by the bundle. Inspect upstream records before new regional acceptance. |
| Missingness | Coverage audit retained; zero-canopy and opaque-DEM-barrier treatments are assumptions, not observations |
| Rights | Existing bundle records USGS/Natural Earth public domain; ETH CC BY 4.0 with Lang, Jetz, Schindler and Wegner attribution |
| Processing | Bounded windows, clipping/reprojection, maximum canopy resampling, observer clearance, LOS and derived display grids |
| Limits | No animal probability, observer activity, access, regional coverage certification or field validation |

Source rights and URLs are retained in the manifest and explained in the
[reproduction guide](documentation-examples.md). This card does not grant new redistribution rights
or independently relicense upstream data. Raw regional rasters are not committed.

The displayed profile is an explanatory sampled ray, not every ray used to aggregate a pair.
The browser reads stored results; it does not rerun LOS. Bundle identity, producer revision and
export revision are separate fields. Preserve the original receipt rather than rewriting it to
suggest a newer run.
