# Reproduce the opt-in building obstruction pilot

## Exact-point resolution and canopy uncertainty

`scripts/compare_obstruction_points.py CONFIG.json` creates a bounded offline comparison from a
retained pilot ZIP, native canopy inventory, checksummed GBA subset/acquisition manifest,
land/water polygons and explicitly named exact viewpoints. Config keys are `pilot_zip`,
`canopy_inventory`, `gba_subset`, `gba_manifest`, `land`, `water`, `output_dir` and `sites`.
Optional `building_inventory_policy` accepts `"assume_complete"` (default) or `"unverified"`.
Each site supplies `name`, `source_h3`, `lat`, `lon`, and its retained **unflattened**
`projected_ground` path. Use a new local output directory. No provider access or promotion runs.

The four comparison steps preserve the same retained 30 m water-target centres inside the
exact 5 km radius: legacy 30 m GDAL Edge; independently grounded 30 m GDAL Edge; direct rays
on the 30 m surface; and direct rays on a nested 10 m surface. The last surface uses nearest
native ETH canopy instead of maximum aggregation and `rasterize_building_footprints(...,
all_touched=False)`. The default footprint rasterizer remains all-touched. `BuildingSurface`
records rasterization and inventory policies. Under `"assume_complete"`, no rasterized footprint
means zero building height. Cell-centre sampling can miss small roofs; the assumption applies
to the rasterized inventory. Under `"unverified"`, absence remains unknown. The retained 30 m
reference models keep their original support masks; revised 10 m models use the selected policy.
The v2 comparison manifest records both policies and the v2 building raster contract. Terrain retains
30 m support even on the finer grid.

The direct model traverses cell interiors along the exact source-target segment. Terrain plus
height is a constant prism within each cell. It evaluates curvature over the intersected
interval and excludes zero-length corner touches; it does not reproduce GDAL Edge or claim
subcell geometry. The source cell is explicitly grounded; a private 30 m canopy-clearance
radius is retained, while neighboring buildings remain intact. Source and target sampling,
clearance, grid/CRS, resampling, ray model and input hashes are part of the comparison provenance.

The additional **canopy envelope** uses zero canopy floor and predicted CHM ceiling, retaining
predicted roofs at the same height in both surfaces. CHM does not supply trunks, crown bases or
gap geometry; a canopy-only crossing cannot establish solid obstruction below a crown.
Clear requires a clear ceiling ray; blocked requires a blocked floor ray; other targets remain
unknown. Missing heights retain unknown upper support; building absence follows the selected
inventory policy. These are model support bounds, not calibrated empirical confidence limits or a porosity estimate.
No arbitrary transmission fraction or midpoint replaces unknown support.

The script writes complete per-target Parquet tables, `comparison.json` provenance and a
self-contained `comparison.html`. All counts shown in HTML are checked against the tables.
The single-point unweighted result is distinct from the original five-sample H3 product and
its distance-integrated kernel. Fine resolution alone may retain zero confirmed-clear targets;
that is not evidence of complete physical obstruction. Field observations remain necessary.

The existing pilot map now defaults to `ui_obstruction_mean` (complete support), with missing
values hatched. Its known-only mean remains a diagnostic in the popup. A zero known-only mean
with unknown support must not be displayed as complete obstruction; the map contract is v2.

This research workflow is supported through the public
`rasterize_building_footprints`, `compose_above_ground_surface` and
`above_ground_visibility` helpers. It is deliberately separate from production
`run_viewshed` and the raster-provider registry: it does not download GBA, register
new production stages, or declare regional readiness. The application owns bounded
input acquisition, footprint inventory containment, fixed source/target sampling,
resource supervision, checksummed manifests and product promotion.

## Cell-score runtime experiment

For a bounded runtime comparison, add `benchmark_cache_dir` to the exact-point config.
The script saves aligned native 10 m surfaces **before** observer clearance and only complete
retained target-cell water-pixel populations within the 5 km window. Boundary-clipped target
cells are excluded rather than reported as whole-cell scores. These local NPZ/JSON caches carry
input hashes and assumptions and are research intermediates, not production artifacts.

Run `PYTHONPATH=src python scripts/benchmark_cell_scores.py CONFIG.json` with `input_dir`,
`output_dir`, `sites` (retained cache names), and optional positive integer `repetitions` (default 3).
Use a dedicated output directory. No acquisition, provider access, cleanup or production
promotion runs. Outputs are `cell_scores.parquet`, `benchmark.json` and a self-contained
`benchmark.html` with paired reference/candidate cell maps, scores, support bounds and timings.
`view_scores.parquet` contains one row per source/target pair for the fastest candidate meeting
the tolerance at both sites (or the reference if none passes). It keeps conditional
`terrain_building_score` separate from `combined_score` and its canopy support bounds.
The report also compares the historical exact viewpoint on the same target populations,
so a shoreline point is not confused with an average across inland source-cell locations.

The score is the unweighted mean visible target-water pixel fraction across deterministic
ground-level observer locations in the source cell's land geometry, excluding rasterized roof
cells. It is conditional on the declared 30 m canopy clearance. A 0.7 score means 70% visible
on average across those locations; union-visible and best-observer support are different metrics.
All blocked and out-of-range pairs remain in the denominator. Unknown support retains null
complete scores and lower/upper bounds. The denominator is the complete **retained raster water
population**, not the production equivalent-area denominator; this experiment does not relabel
the canonical `physical_viewability` product or alter distance-integrated weights.

The reference uses 15 nested maximin observer samples, 10 m direct grid rays and every retained
target pixel. Candidates compare 5/10/15 observers, GDAL Edge versus compiled direct rays, and
10/30 m obstacle grids. Direct-ray candidates choose one actual water point per target-cell
2 × 2 pixel block and weight it by the represented population. Cells with fewer than 1,000
target pixels receive a complete census to prevent sparse estimates of small water fragments.
The 30 m candidate conservatively takes maximum obstacle height and any unknown support within
each 3 × 3 native block; terrain retains its existing 30 m support in every case.

Runtime medians include private surface construction, all three obstruction components and
cell aggregation. Input loading/sampling and warmup are recorded separately. Acquisition,
raw cache preparation, cold Python/Numba startup and regional execution are outside the timing.
The GDAL candidate uses floor and finite high unknown ceilings; it is qualified only below
10,000 m supported elevation. Height-monotonic conflicts become unknown and reject the candidate.
The exploratory numerical tolerance is p95 absolute difference ≤0.05 and maximum ≤0.10 across
cell clear bounds and unknown fractions, with no native consistency conflicts. This tests
agreement with the model reference at the two sites; it does not establish source completeness,
observer-sampling convergence everywhere, canopy transmission, or empirical viewshed accuracy.

## WorldCover plus canopy transmission trial

`PYTHONPATH=src python scripts/compare_classified_canopy.py CONFIG.json` consumes the saved
cell-score `benchmark.json` and a completed retained WorldCover classification inventory.
Required config fields are `benchmark_report`, `worldcover_manifest` and a dedicated
`output_dir`. Optional `beta_per_m` is a list of unique positive finite attenuation settings
including `0.002` (default `[0.001, 0.002, 0.004]`); `repetitions` defaults to 3.
`water_class_policy` accepts `zero_if_missing` (trial default) or `retain_unknown`.
All retained WorldCover assets and prepared caches are verified against their recorded hashes.
No download, cleanup or canonical promotion runs.

The reusable experimental calculation is
[`weights/vegetation/classified.py`](../src/viewshed_toolkit/pipeline/weights/vegetation/classified.py),
with contract `worldcover_chm_intersection_transmission_v1`. It uses the historical OrcaCast
WorldCover multipliers/floors and CHM obstruction bins: `<2 m: 0`, `2–<5 m: 0.25`,
`5–<15 m: 0.65`, `≥15 m: 1`. Per-cell coefficient is
`max(height_obstruction × class_multiplier, class_floor)`. These are dimensionless assumptions,
not measured transparency. Floors do not invent an obstacle height.

Terrain and buildings first resolve baseline rays. For a baseline-clear ray, the exact raster
traversal integrates weighted **horizontal** metres beneath the canopy top, including curvature
and partial cell intersections. Modeled transmission is `exp(-beta × weighted_metres)`.
Rays above the CHM and zero-height columns retain transmission one. Known terrain/roof blockers
retain zero. Buildings are never cleared by a vegetation class or observer canopy clearance.
The ground-to-CHM columns do not describe actual crown bases, trunks, or undergrowth. This is a
class-aware transmission scenario, not a calibrated geometric visibility fraction or detection
probability. Its cell denominator and observer design match the bounded runtime experiment;
there is no distance attenuation or observer access factor.

The explicit `zero_if_missing` policy assumes zero vegetation where WorldCover class 80
(permanent water) coincides with missing CHM and a zero lower height. It does not change terrain,
building support, target-water geometry or the original missing-height mask. Positive-height
water conflicts remain unknown. Missing land canopy and intersected unrecognized classes remain
unknown. The reusable calculation defaults to `retain_unknown`; the trial also exports a
`fast_strict` comparison retaining all missing heights. Classification is a model assumption,
not evidence that shoreline geometry or canopy has been field verified.

Outputs are `worldcover_canopy_scores.parquet`, `worldcover_view_scores.parquet`, `report.json`
and self-contained `report.html`. Open the generated HTML, not the `.html.in` template in
`scripts/`. Each row retains the model, policy, beta, source/target identity, support bounds and
missing-input fraction. The selected lookup uses the central beta and fast target sampling only
if it passes against full-target calculations at both sites; otherwise it uses the reference.
All estimates must remain within the original canopy envelope. One ray evaluation supplies all
beta scenarios. Timings include the baseline rays, private clearance, three scenarios and cell
aggregation; input alignment, warmup and acquisition are excluded. The JSON records source/code
hashes, parameters, class context and a scientific signature. Empirical accuracy and regional
observer convergence remain unqualified.

The retained trial uses [ESA WorldCover 2020 v100](https://doi.org/10.5281/zenodo.5571936),
aligned categorically with nearest sampling onto the same 10 m grid as ETH 2020. Retain
`© ESA WorldCover project 2020 / Contains modified Copernicus Sentinel data (2020) processed by
ESA WorldCover consortium`, CC BY 4.0, input acquisition records, hashes and source ETags.
WorldCover class 50 is built-up land, not a building-footprint inventory or proof of no trees.

### Additional average-viewability maps

`PYTHONPATH=src python scripts/map_average_viewability.py CONFIG.json` adds plots for every
target water cell within 5 km of at least one saved source observer. Config contains exactly
`trial_report` (the saved WorldCover trial `report.json`) and a new dedicated `output_dir`.
It retains the same 15 observers, 10 m obstacle grid, 30 m terrain and water-centre lattice,
central attenuation coefficient and explicit water-class policy. Retained inputs are checked
against their hashes. It performs no acquisition or canonical promotion.

Unlike the original complete-cell benchmark, these maps include boundary cells. A score describes
the **in-range water portion** of an H3 resolution 7 cell. All observer/target comparisons remain
in the denominator; comparisons beyond 5 km from an individual observer contribute zero.
Targets beyond the retained obstacle raster remain in the pair universe with unknown in-range
comparisons and explicit coverage fractions. They are not dropped or counted as blocked.
The changed target population makes these maps distinct from whole-cell benchmark scores.

Outputs include `average_viewability.parquet`, `average_viewability.json`, scientific map images
and a canonical portable-report `artifact.json`. Fast sampling is checked against every retained
target before selection; full-target reference bounds remain on every row. The supplemental
`average-viewability.html` is packaged from that artifact using the Data Analytics portable builder.
The copied detailed `report.html` links to the additional page; the original report is preserved.
Map colours share a 0–100% scale and separate lower/upper estimates. The HTML payload can be
structurally checked when browser navigation is unavailable; that is not browser interaction QA.

## Required retained inputs

Use the same north-up 30 m EPSG:32610 grid for bare terrain, ETH 2020 maximum canopy,
canopy unknown mask and full-supported-water mask. Check all shapes, affine transforms
and CRS before reading arrays; do not silently warp or resize masks. Use a qualified
GBA SQLite subset with original EPSG:3857 WKB and official `source + id + region`
height linkage, preserving the reader's height state and separate footprint license.
The immutable full-source hashes and subset SHA256 must match the acquisition manifest.
The entire source-relative 31 km ray-grid perimeter, densified before transformation,
must be contained in the filtered study bbox. A regional bbox inventory is not a
claim of regional source completeness.

The qualified research pilot pins GBA.LoD1 revision
`9da24b3a8dce436a7420d5c3589de718d7ba14d6` and GBA.ODbLPolygon revision
`ddf775455af217f9909593c090a52e67be887e7a`. Its bbox is
`[-129.7, 45.9, -121.5, 51.5]`. Local source vectors are not distributed with code.
GBA primarily represents 2019 imagery with 2018 fill; ETH canopy represents 2020.
Neither this workflow nor their combination reconstructs annual 2009–2026 conditions.

## Portable caller recipe

The following recipe consumes caller-qualified inputs. `stack` is an existing raster
with authoritative `shape`, `transform`, `crs` and `bounds`. `ground`, `canopy`,
`canopy_unknown`, `qualified_water`, and integer `target_codes` are already aligned
arrays. `targets` is the canonical target-code lookup. `observers` is the retained,
ordered list of actual projected land-source samples; do not substitute cell centroids.
Run a single site per resource-supervised process. No source data or replay copies are
required in the repository.

```python
from pathlib import Path
import math
import numpy as np
from pyproj import Transformer
from rasterio.transform import xy
from viewshed_toolkit import above_ground_visibility, rasterize_building_footprints
from viewshed_toolkit.pipeline.providers.global_building_atlas import iter_gba_subset

# Caller verifies the manifest hashes and complete ray-grid containment first.
# Derive a conservative EPSG:3857 candidate bbox by densifying all grid edges.
bounds_3857 = Transformer.from_crs(stack.crs, 3857, always_xy=True).transform_bounds(
    *stack.bounds, densify_pts=101
)
records = iter_gba_subset(Path(subset_path), bounds_3857)
buildings = rasterize_building_footprints(
    ((record.geometry_wkb, record.height_m) for record in records),
    shape=stack.shape, transform=stack.transform, crs=stack.crs,
    inventory_policy="assume_complete",  # Explicit complete-inventory model assumption.
)
def clearance_mask_for(observer, stack, radius_m):
    row, col = stack.index(observer.x, observer.y)
    radius_cells = math.ceil(radius_m / stack.transform.a)
    mask = np.zeros(stack.shape, dtype=bool)
    mask[row, col] = True
    for r in range(max(0, row-radius_cells), min(stack.height, row+radius_cells+1)):
        for c in range(max(0, col-radius_cells), min(stack.width, col+radius_cells+1)):
            x, y = xy(stack.transform, r, c, offset="center")
            if math.hypot(x-observer.x, y-observer.y) <= radius_m:
                mask[r, c] = True
    return mask

counts = np.zeros((3, len(targets)), dtype=np.int64)  # unknown, blocked, clear
for observer in observers:
    row, col = stack.index(observer.x, observer.y)
    # Construct the retained private 30 m canopy-clearance mask in projected metres.
    # This is a declared source model assumption, not a building absence certificate.
    canopy_clearance = clearance_mask_for(observer, stack, radius_m=30)
    states = above_ground_visibility(
        ground, canopy, canopy_unknown, buildings,
        qualified_water=qualified_water, observer_row=row, observer_col=col,
        pixel_size_m=30, canopy_clearance_mask=canopy_clearance,
        grounded_observer=True, observer_height_m=1.7, target_height_m=1.0,
        curvature_coefficient=0.85714, earth_radius_m=6378137,
        max_distance_m=30000,
    )
    selected = target_codes >= 0
    np.add.at(counts, (states[selected] + 1, target_codes[selected]), 1)
```

Production callers can process target rows in strips to bound temporary allocations.
The retained two-site pilot uses five fixed land-source points per H3 source cell and
all actual 30 m water target centers (stride one), including blocked and out-of-range
comparisons. Exact-point diagnostics use one observer and remain secondary. Preserve
unique source/target keys and the exact target-code lookup before joining counts.
Arithmetic visibility is `clear/(clear+blocked+unknown)`, null if the population is
empty or unknown is nonzero. The known-only diagnostic is `clear/(clear+blocked)`;
full-population bounds are `clear/total` to `(clear+unknown)/total`. Do not replace
unknown with zero or reweight these comparisons to create positives.

The package rasterizer projects with `always_xy=True`, records projected polygon
repairs, burns every touched pixel at the maximum overlapping valid AGL prediction,
and ORs overlapping missing-height support. Feature order cannot change heights or
support. Outside published footprints on land, the lower height is zero; it is known zero under
`assume_complete` and retains unknown upper support under `unverified`. A predicted zero is a
value; inventory completeness remains an explicit model assumption.
Composition is `max(canopy_lower, building_lower)` above terrain; upper uncertainty is
the union of component unknown masks. Full-water qualification may resolve zero,
except building/water contradictions remain unknown. The LOS helper retains known
height floors beneath uncertain components. Its central-cell grounding is explicit;
private clearance changes canopy only and never neighboring building heights.

Retain terrain-only, canopy-only and building-only diagnostics. The existing
`weight_vegetation` stays canopy-only. The new `weight_obstruction` conditional factor
contains canopy and buildings once. Its distance-integrated combined kernel uses the
retained equivalent-water-area denominator and a bare-terrain upper bound; never
multiply distance or a separate canopy factor again. Missingness intervals condition
on supported predicted heights; provider variance is not a calibrated confidence
interval. Include model/grid/source/geometry hashes in a new scientific signature.

## Rights and verification boundary

[GBA](https://github.com/zhu-xlab/GlobalBuildingAtlas) heights and non-ODbL footprints
carry [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/). ODbL footprint
geometry retains [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Preserve
per-feature source identity and license split, including the one non-ODbL retained
Victoria footprint. Existing ETH/terrain/FABDEM attribution travels with their inputs;
GBA does not replace those terms. Source vectors remain outside Git and the pilot ZIP.

Raster and caller regressions verify max instead of sum, feature-order independence,
reprojection, missing overlaps, empty inventories, zero predictions, grid rejection,
explicit grounding and preservation of adjacent roofs under canopy clearance.
The published pilot is a conditional model, not field verification of buildings,
canopy porosity, open ground, tidal heights, or urban completeness.

## Inspect the actual pilot input grids

The comparison exporter can include `raster_overlays` metadata for shared offline
`raster_inputs/<source>.js` assets created by `export_raster_overlay_asset`. Extract
those assets beside all three HTML files. Select a raster site and layer, then use
**Zoom to source samples**. Red pins identify the actual land samples; amber pins
identify the separate exact diagnostics. All overlays share the retained native
EPSG:32610 30 m grid. The WGS84 overview PNG is a nearest-neighbor display preview;
it never enters LOS. Close views draw native cell footprints, and pixel inspection
always decodes exact native float32/uint8 values. No browser fetch or server is needed.

Inspect model DEM elevation and original projected DEM separately: the sparse
original-minus-model delta exposes water flattening without copying a second
terrain replay. CHM/building/joint heights are metres **above ground**, unlike DEM
terrain elevations. Raw CHM255 is no data. The historical pilot used unverified building
absence; new caller-created surfaces default to assumed-complete inventory. A zero building
lower floor is conditional on that policy; missing-height overlap and building/water conflict
masks remain explicit. The qualified joint lower surface can differ from raw footprint
predictions at contradictory water cells. The unknown-upper overlay is distinct
from the supported lower surface and from the full-water qualification mask.

The base grids precede each observer's private30m canopy clearance and central-cell
grounding. Neighboring building heights are never cleared. Native values, grid
metadata, source hashes, immutable pins and attribution travel with each asset.
This inspection makes modeling assumptions visible; it is not field validation.
Do not alter clearance, weights or unknown support to reproduce an expected view.

## Extend the retained demo to 15 miles

`scripts/extend_viewability_demo.py CONFIG.json` rebuilds the two saved source-cell demos
from retained regional terrain, native ETH canopy tiles, WorldCover clips and the validated
GlobalBuildingAtlas subset. Configuration has exactly `trial_report`, `terrain_raster`,
`output_dir` and `radius_miles`, with optional `sites` selecting unique retained site names;
the bounded helper accepts up to 15 statute miles
(24,140.16 m). Use a new empty output directory. No download, production promotion or cleanup
of prior artifacts occurs.

The raster extent encloses the union of all saved observers' reach, with padding on the
original 30 m terrain / nested 10 m obstacle lattice. The prior cache is retained exactly
inside its original window; outer terrain is nearest-aligned at 30 m and repeated to 10 m.
Native canopy and categorical WorldCover alignment use a near-exact reprojection tolerance
of 1e-8 source pixels to prevent approximate-transform overlap at tile seams. Building
footprints use the same cell-center maximum-height rule and assumed-complete inventory.
The existing declared 0 m water convention remains in force. Missing land terrain fails;
missing canopy or roof heights remain unknown. Full-footprint qualified water and the
explicit WorldCover-water zero-canopy assumption remain separate from missing land inputs.

Outputs include georeferenced terrain, canopy, building and qualification/missingness rasters
in `rasters/`, H3 R7 target-pair Parquet/JSON, scientific PNG maps and a canonical `artifact.json`
for the portable report builder. Every 30 m water centre within reach of at least one observer
enters the target population. Individual observers keep their own distance cutoff; boundary
cells describe their in-range water portions. Full-population and weighted stride-2 calculations
are compared before selecting the sampled result, with the existing 10 percentage-point maximum
and 5 percentage-point p95 error gates. Actual errors and runtimes are recorded. Earth curvature
is active, so targets inside the requested radius can still be below the modeled horizon.
This trial measures geometric/canopy transmission without an additional distance-decay factor;
it does not change the production distance-integrated kernel or establish field calibration.

### Parallel source processing

`scripts/parallel_viewability_demo.py CONFIG.json` accepts the same four required demo fields
plus `workers` (1–4) and `memory_budget_gb` (GiB, using 1024³ bytes). Each retained source is a separate Python process
with one native thread per library, its own output directory and a captured log. Start with
two workers on a 16 GB machine. The sampled memory guard counts this parent and the workers
it launched; these audited workers use native threads and do not launch descendant processes.
Live unreadable memory, an exceeded budget, or a failed worker stops the remaining workers.
Partial per-source files remain diagnostic evidence; no combined pair table is written until
all sources finish and their scientific inputs, bounds, missingness and pair identities validate.
The combined rows sort by source role, source H3 and target H3 independently of completion order.

Only the declared retained sources are processed. This does not launch a full-region run or
change production scheduling. The runner still prepares each source window independently;
shared regional raster preparation and reusable spatial tiles are separate future improvements.
Memory measurements sample every 0.1 s and are safeguards, not operating-system reservations.
