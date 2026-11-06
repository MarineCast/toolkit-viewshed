# Retained regional-data benchmark — 2026-11-04

**Superseded scheduling estimate.** The [subsequent collection and pixel audit](coverage-collection-2026-11-04.md)
found no jointly gap-free DEM/canopy windows. The 4–8-hour projection below is historical and
should not be used as a reliable completion estimate. A newly acquired, gap-free DEM benchmark
is documented in that follow-up; canopy qualification remains open.

A provisional **4–8-hour compute budget** is reasonable for the retained shared-study workload on
this machine, assuming reusable regional inputs. This is a planning allowance, not a validated
end-to-end ETA or confidence interval. Missing regional rasters prevent a reliable forecast:
**82.6% of nonwater pixels in this benchmark's padded grid lack observed DEM coverage**. The
configured opaque-barrier and zero-canopy gap assumptions were retained. Acquisition and
full-region raster preparation, lookup construction, finalization, and map rendering remain
unmeasured and add time. A completed regional scientific product was not generated.

## Settings and data

The current implementation was exercised using retained USGS 3DEP and ETH 2020 canopy assets
from `data/san_juan_demo/`, regional physical-water context and R7 source/target geometry from the
MarineCast shared-study delivery. Original assets were referenced in place and SHA-256 recorded.
The existing 100 m demo rasters were not used. Native DEM and CHM were warped/aligned to 30 m
using bilinear DEM and maximum-height canopy resampling, as in the production configuration.

Preserved settings: H3 R7, 30 m raster grid, 30 km radius, 1 km preparation margin, full-pixel
aggregation/stride 1, adaptive 5–10 land observers, 3 water observers and 3 target samples,
curvature/refraction, eye/target heights, distance attenuation, and canopy clearance. The land
runner used the configured 14 observer workers and seven-source batch limit. Source cells were
selected at evenly spaced land-fraction ranks among retained cells in central San Juan Islands;
this is a small convenience sample, not a probability sample of the whole coastline.

The bounded lookup includes reporting targets within 30 km centroid distance of the selected
sources, excluding self-pairs. This explicit benchmark selection is not a regenerated complete
canonical pair universe. No weights were delivered or promoted. The exact lookup, sampling
records, configuration and outputs remain in task-owned staging for replay.

The larger regional raster paths in the historical inventory are unavailable in the current
workspace. The retained native DEM spans about 42 × 40 km and the canopy windows about
34 × 32 km, smaller than a 60 km diameter viewshed. Preparation retained the full 30 km windows
and their configured gap policies rather than reducing radius or resolution. Observed DEM covers
15.9% of the padded grid. Timing through missing terrain may differ from fully observed terrain;
this cannot establish scientific coverage or a reliable complete-region runtime.

## Measured results

| Benchmark | Work | Elapsed |
| --- | --- | --- |
| Initial land trial | 8 sources, 62 observers, both surfaces | 5.92 s cold; 3.07 s warm |
| Larger land trial | 24 sources, 189 observers, 13,886 candidate pairs, both surfaces | 9.00 s cold; 7.67 s warm |
| Water kernel | 16 sources, 3 × 3 samples, about 560–600 candidate targets/source | First source 1.206 s including target-cache construction; remaining sources mean 0.0460 s, median 0.0188 s |

Warm land trials reused preparation caches but computed every source partition again in separate
partition directories. All 24 sources succeeded on both surfaces in both trials. Exact physical
weights and LOS fractions matched across all 48 corresponding source/surface partitions. The
larger land trial sampled up to 1.62 GiB parent RSS. Child/process-tree sampling was unavailable,
so this is partial memory evidence, not a hard peak bound. The 768 MiB configured guardrail is a
working-set estimate and does not cap actual process RSS.

Water measurements used the actual opaque-land kernel with retained water/land geometry and
configured sampling and horizon policies. They exclude source sampling, partition I/O, dense
composition and maps; they are not a complete water pipeline timing. Both land trials exclude
initial asset warping; its separate elapsed value is retained in the first result JSON.

## Projection arithmetic and uncertainty

The retained shared-study inventory has 26,573 land source cells, 241,307 requested land observers
under the current adaptive rule, and 20,686 water source IDs. Some sources may have no eligible
reporting pair; population scaling does not prune them. These counts describe the shared study,
which is broader than the standalone canonical `model_area` rectangle.

Land projection in hours is `paired trial seconds × 241307 / trial observer count / 3600`.
The 24-source trials yield **2.72–3.19 hours**. The smaller trial yields **3.32 hours warm** and
**6.40 hours cold**, illustrating how repeatedly charging initialization can distort a forecast.
Serial water-kernel scaling is `mean noninitial source seconds × 20686 / 3600`, about
**0.264 hours (16 minutes)**. Water cost varies by coast complexity and candidate count.

Rounding these observed rates with planning headroom gives the provisional **4–8-hour compute
budget**. It does not bound missing-data effects, acquisition, full-domain preprocessing,
lookup/aggregation/composition/maps, concurrent system load, or cache misses outside this small
area. No whole-region speedup against the previous implementation was measured. More diverse,
fully covered regional source windows are required to turn this into a dependable schedule.

## Reproduce and inspect

Local workspace evidence: `Reports/viewshed-regional-benchmark-2026-11-04/` contains `run.py`,
`water.py`, `summarize.py`, JSON results, sampling CSVs, bounded logs and staging manifest. From
the MarineCast workspace root, with the compatible geospatial environment:

```bash
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-regional-benchmark-2026-11-04/run.py --sources 24
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-regional-benchmark-2026-11-04/water.py
PYTHONPATH=Toolkits/toolkit-viewshed/src python Reports/viewshed-regional-benchmark-2026-11-04/summarize.py
```

The scripts bind the existing local stores; review their resolved paths before replay elsewhere.
Replays recompute source partitions while allowing preparation-cache reuse. To measure cold
preparation again, choose a fresh task-owned staging directory in the script. Replays replace
their task result files; preserve the original evidence separately before rerunning.
`summary.json` records source-code hashes and the extrapolation formula. Runtime: Python 3.14.6,
GDAL 3.12.3, Rasterio 1.5.0, NumPy 2.4.6, Numba 0.65.1 and Polars 1.42.1 on macOS arm64.

Staging is under `.marinecast-work/viewshed/benchmark-20261104/`, approximately 160 MiB retained;
observed sampled disk peaks reached about 314 MiB during preparation. The planned cap was 2 GiB,
and about 66.8 GiB remained free. No acquisition, delivered-Data modification, permanent cleanup,
publication or commit occurred. This benchmark adds runtime evidence to the
[implemented audit fixes](implementation-2026-11-04.md); it does not establish regional readiness.
