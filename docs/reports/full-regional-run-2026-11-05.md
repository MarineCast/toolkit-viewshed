# Full Vancouver Island–Columbia viewshed run — November 5, 2026

The optimized workflow completed and validated the full eligible regional study in **3h 4m 9s**.
Timing began with retained-input preparation at 08:20:36 Eastern and ended after independent
validation at 11:24:45. It includes the observer census, one pre-LOS retry, both source roles,
finalization and map export. Input acquisition was completed during the preceding qualification.
The run used the 30 m EPSG:32610 inputs, native H3 R7, land pixel stride 1, adaptive 5–10 land
observers, three water observers per source and three samples per water target.

| Source role | Active source cells | Actual observers | Target cells | Validated pair rows |
|---|---:|---:|---:|---:|
| Land | 26,094 | 232,558 | 13,461 | 5,555,318 |
| Water | 20,323 | 60,969 | 13,461 | 7,026,773 |

The run driver supplied the pinned Vancouver Island–Columbia membership to the toolkit's lookup
builder and used the retained qualified shoreline and marine-only elevation policy. Reconciliation
clipped original land footprints against vector and raster water and native H3 cells. Of 26,573
pinned land cells, 328 fell below the existing dry-land eligibility threshold. Of 20,686 pinned
water cells, 342 lacked positive water geometry under the qualified shoreline. The pair lookup
found no eligible target within range for another 151 land and 21 water sources. The evidence
retains these membership decisions. All pinned reporting targets had eligible water support.

The first land sample check identified 45 points in 40 cells that landed on wet raster pixels
after reprojection. Each was within 1.4 nanometers of a pixel edge. The run stopped before LOS,
then inset the 40 affected footprints by 1 μm, removing 0.2714 m² in total. The same sampling
policy returned all requested points, with zero land observers on either water mask.

| Stage | Elapsed |
|---|---:|
| Paired land sampling, LOS and factor composition | 2h 36m 57s |
| Water sampling, LOS and combination | 11m 51s |
| Water vegetation factor and final tables | 23s |
| Map export | 3m 20s |
| Independent validation | 1m 9s |

The measured stages, including preparation and lookup, sum to 2h 56m 16s. The end-to-end wall
time also includes the first sampling attempt, edge correction and transitions.

Validation checked every final table against the role-specific pair universe, verified unique
keys, finite bounded weights, the static composition formula, and water vegetation's
not-applicable state. Both land-surface manifests and the water manifest cover the active
source membership with complete observer counts. Checks also covered 20 GeoTIFF and PNG layer
pairs, three HTML map exports and their assets, artifact and input lineage checksums, and 2,014
previously retained input files.

The regional outputs remain in the task-owned `.marinecast-work/viewshed/full-regional-20261105/`
directory. Its `final/` folder contains both static weight and both observation geometry tables.
The `maps/h3r7/static/` folder contains the selected-source map and the two regional aggregates.
The MarineCast workspace report directory `Reports/viewshed-full-regional-2026-11-05/` contains the
run and validation scripts, journal, stage timings, checksums, source exclusions and inventory.
Later on November 5, the four final tables and two producer metadata sidecars were delivered
byte for byte to the MarineCast workspace's `Data/Viewshed` directory. The local
`regional-delivery.json` binds the generation to its exact files and records the validation
evidence path. Observation geometry metadata is embedded in the two Parquets. The Explorer
uses the manifest to read this generation, with role-specific overview and pair queries.

Related methods and bounded evidence are recorded in the [inland-water correction](inland-water-elevation-2026-11-04.md)
and [performance guide](../performance.md).
