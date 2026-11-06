# Observer/water reconciliation and eight-cell benchmark — 2026-11-04

All eight bounded Puget source cells now have validated source geometries and **zero
observer/water conflicts**, including the five withheld in the
[preceding canopy benchmark](canopy-resolution-2026-11-04.md). The paired bare-earth/canopy
benchmark ran all eight cells with **53 observers**, at the unchanged **30 m / H3 R7** resolution,
full-pixel aggregation and deterministic adaptive sampling policy.

## Source correction

The retained source-land geometry was reconciled with both independently supported vector water
and the actual production raster mask. In the existing metric area CRS, **EPSG:32610**, the method is:

1. Subtract the retained corrected physical-water polygons from the original source land.
2. Intersect with polygonized nonwater pixels from the existing 30 m, all-touched production mask.
3. Intersect with the recorded full native H3 cell polygon.
4. Recalculate land area, land fraction and representative coordinates; serialize and reread the
   corrected source product before applying the existing sampler.

The raster intersection preserves consistency with the water surface actually used by LOS. It
conservatively removes shoreline portions in pixels classified as water under the existing
all-touched policy; it does not claim a newly surveyed shoreline. No source geometry was expanded,
simplified, buffered or discarded. Native H3 identities, full-cell geometry, area denominators and
parent study/configuration ancestry are retained. No unsupported complement water fraction is
invented. Corrected geometry is also recorded as the `land_geometry` alias.

Validation of the first attempt found roughly 28 m² of parent-geometry slivers outside one recorded
H3 polygon. Explicit native-cell clipping corrected this support error before the final benchmark;
the failed validation and pre-clipping timings are retained separately and excluded below.

The sampling policy remains `active_fraction`, minimum 5 / maximum 10, component-aware projected
nested maximin v5. The corrected fraction of `8728d50b6ffffff` changes its requested count from
6 to 5 under that same policy, yielding **54 → 53 total observers**. This is not a sampling-budget
reduction. Observer coordinates are deterministically regenerated from corrected land; retaining
old coordinates would preserve the conflicts. All five previously withheld cells are included.

| Native source H3 | Land fraction, original → corrected | Observers, original → corrected | Corrected water conflicts |
| --- | ---: | ---: | ---: |
| `8728d5094ffffff` | 1.000000 → 0.996840 | 10 → 10 | 0 |
| `8728d50b2ffffff` | 0.445384 → 0.395971 | 5 → 5 | 0 |
| `8728d50b6ffffff` | 0.625159 → 0.488860 | 6 → 5 | 0 |
| `8728d5548ffffff` | 0.301577 → 0.253102 | 5 → 5 | 0 |
| `8728d5549ffffff` | 0.975440 → 0.957647 | 10 → 10 | 0 |
| `8728d554bffffff` | 0.372057 → 0.241811 | 5 → 5 | 0 |
| `8728d554dffffff` | 0.833861 → 0.758203 | 8 → 8 | 0 |
| `8728d556bffffff` | 0.147784 → 0.104085 | 5 → 5 | 0 |

The formerly withheld IDs are `8728d50b6ffffff`, `8728d5548ffffff`, `8728d554bffffff`,
`8728d554dffffff` and `8728d556bffffff`. The three earlier accepted cells are reconciled with the
same rule so all eight use one coherent source definition. The candidate lookup remains exactly
the original **2,348 pairs**, including the existing self-pair policy.

## Validation performed

Independent validation reread the serialized Parquet and checked:

- Eight unique R7 IDs; valid, nonempty Polygon/MultiPolygon geometry; retained full H3 geometry,
  cell areas and ancestry; consistent geometry alias and representative coordinates.
- Containment in original land and native H3; exclusion of corrected vector water and raster-water
  pixels; no positive-area cross-cell overlap. Metric tolerance is `max(1e-4 m², area × 1e-10)`.
  Observed projection/serialization residual areas were below 0.000002 m².
- Recomputed EPSG:32610 area/fraction consistency and minimum source-area eligibility for every cell.
- All 53 unique observers lie within their own corrected geometry and outside vector/raster water;
  reversing source-row order produces exactly the same observer design.
- Corrected source-land support plus 31 km (30 km range + 1 km margin) is inside the prepared grid,
  with **zero missing required DEM or accepted canopy pixels**. Grid resolution/alignment remains
  exact, and physical, H3/sampling and batching configuration equals the original eight-cell run.
- **48 successful source/surface completions** across three fresh trials and **32 exact repeated
  physical-partition comparisons**, covering pair keys, terrain weights and LOS fractions.
- Independent cache reuse validates all 16 partitions and makes **zero native LOS calls**.

These checks validate support, data availability and repeatability; nominal static water mapping
and modeled canopy heights have not been field calibrated. The preceding report's mixed
ETH 2020 / WRI-Meta 2017–2019 source/vintage and auxiliary-mask qualifications still apply.
Existing valid ETH heights and the resolved canopy composite are unchanged. Water-surface,
observer-clearance, curvature/range and aggregation settings are unchanged.

## Measured paired execution

| Trial | Sources / observers | Wall seconds | Native LOS calls |
| --- | --- | ---: | ---: |
| First process/setup, retained prepared inputs | 8 / 53 | 20.813 | 106 |
| Fresh compute, warmed process | 8 / 53 | 2.324 | 106 |
| Fresh compute, warmed process | 8 / 53 | 2.300 | 106 |
| Verified partition reuse | 8 / 53 | 0.122 | **0** |

Fresh trials wrote independent partition directories and invoked native GDAL once per observer
per surface; they did not reuse completed LOS partitions. Existing raster preparations and Numba
cache were retained, so the first row is not a completely empty-cache acquisition/build timing.
The timed section includes the owner paired runner and telemetry, not source correction or data
acquisition. The largest sampled parent RSS was about 1.84 GiB; process-tree sampling was partial,
so this is not a total-memory limit.

Scaling the two warm times by the earlier nominal **241,307 requested land observers** gives
**2.91–2.94 hours for steady paired land LOS**. This remains a convenience-sample extrapolation,
not a confidence interval or full wall-clock ETA. Correcting the full regional source census,
regional data acquisition, Canadian DTM/vertical-reference qualification, all-region raster and
geometry/lookup preparation, composition, water-source computation and maps remain unmeasured.
The first-process setup cost must not be charged repeatedly to each 53-observer group.

## Retained artifacts and replay

Workspace `Reports/viewshed-source-reconciliation-2026-11-04/` contains `prepare_sources.py`,
`benchmark.py`, `validate.py`, per-cell comparison CSVs, input/output hashes, execution logs,
`summary.json` and `staging-manifest.json`. Corrected sources, observer records, configuration and
native result partitions are retained under
`.marinecast-work/viewshed/source-reconciliation-20261104/`.

The run references the original bounded DEM/lookup under `covered-benchmark-20261104/` and the
resolved canopy, corrected water, projected DEM and canonical mask under `canopy-resolution-20261104/`.
The current mask is selected through the owner canonical-stack API, rather than an arbitrary cache
file. No bulk inputs were copied or downloaded. Neither delivered `Data/` products nor the pinned
shared-study registry were overwritten or promoted; source correction remains benchmark-scoped.

From the MarineCast workspace root, with the compatible owner environment:

```bash
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-source-reconciliation-2026-11-04/prepare_sources.py
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-source-reconciliation-2026-11-04/benchmark.py
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-source-reconciliation-2026-11-04/validate.py
```

Replay overwrites this task's corrected source/configuration and trial evidence; retain any earlier
results before rerunning. The existing inputs are required. Execution used Python 3.14.6,
GDAL Python 3.12.3, Rasterio 1.5.0, NumPy 2.4.6, Numba 0.65.1, Polars 1.42.1,
GeoPandas 1.1.4 and PyArrow 24.0.0 on macOS/arm64. Repository baseline is `5d1d2b6` with the
preceding uncommitted fixes; this task changes benchmark artifacts and documentation, not toolkit
source. The preceding 391-test package run was not rerun or presented as current data validation.
Replay scripts pass scoped Ruff and Black checks, and native validation passes on the final data.
No docs build is configured. Task staging is about 24.3 MiB against a 1 GiB cap, with about 64 GiB
free. Sources, checkpoints and evidence are retained; no permanent cleanup, publication or commit
was performed.

The [regional US/Canadian qualification follow-up](regional-qualification-2026-11-04.md) expands
input coverage and the benchmark design. Its executed results supersede this eight-cell timing
for regional planning; this report remains evidence of the original bounded reconciliation.
