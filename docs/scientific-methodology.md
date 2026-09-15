# Scientific methodology

This page is the numerical source of truth for the static viewability products. The
[methodology guide](methodology.md) is a staged, conceptual overview; where a diagram or shorthand
there differs from the definitions below, this page defines the implemented contract.

These products describe static observation geometry and viewability. They do not represent
observer effort, access, reporting capture, detection probability, whale occurrence, population
abundance, or ecology.

## Pair universe and notation

The durable grain is one unique `source_h3 × target_h3` pair. The area-preparation lookup is the
canonical pair universe: visibility stages may emit sparse rows, but they may not invent pairs
outside that lookup.

For a source cell `s` and target cell `t`:

- `O_s` is the ordered set of actual observer samples in the active part of `s`; `N_s = |O_s|`.
- `P_t` is the target-water population used by the land raster kernel. `M_t` is its persisted
  `target_water_pixel_count` denominator.
- `T_t` is the deterministic target-water sample set used by the water kernel; `Q_t = |T_t|`.
- `L_b(o, p)` is 1 when observer `o` can see target pixel or sample `p` on surface `b`, and 0
  otherwise. The surface is `bare` or `canopy` for land sources.
- `d(o, p)` is projected observer-to-target distance in kilometres.
- `D(d)` is the configured distance-decay function after clipping to `[0, 1]` and application of
  the hard distance cutoff.

All means below include blocked and out-of-range observer/target combinations as zero. They are
not means over only the visible combinations.

## Deterministic observer sampling

Sampling is performed inside the active land or water geometry in the configured projected CRS;
the selected coordinates are then returned in WGS84. In `fixed` mode the requested count is the
configured maximum. In `active_fraction` mode:

```text
requested = clamp(floor(max_samples × active_fraction + 0.5), min_samples, max_samples)
```

The active fraction is the cell's land fraction for a land source and water fraction for a water
source, clipped to `[0, 1]`. A missing fraction or active geometry is an error in adaptive mode.

The spatial design is deterministic:

1. Repair the polygonal geometry and sort disconnected components by descending area, then by
   rounded centroid coordinates.
2. When centroid inclusion is enabled, seed a representative interior point; then seed every
   disconnected component before refinement.
3. Generate a fixed interior candidate grid in projected coordinates, with candidate density
   allocated in proportion to the square root of component area.
4. Select the remaining points by maximin distance. Distances are rounded to six decimal places
   before tie-breaking, and candidate order is stable.
5. Construct the design up to the configured maximum independently of the requested count and
   return a prefix. Smaller adaptive designs are therefore nested within larger designs.

Coordinates are de-duplicated; the sampler never pads a short design with repeated points. The
actual count is recorded as `sample_points_actual`, and that actual count is the production
observer denominator. Compatibility inputs without that field use, in order,
`sample_points_requested`, `sample_points_per_source_cell`, an explicitly supplied observer count,
and finally 1; new artifacts must persist the actual count.

Implementation: [`prepare/area/sampling.py`](../src/viewshed_toolkit/pipeline/prepare/area/sampling.py)
and the source-role overrides in
[`config/loader.py`](../src/viewshed_toolkit/pipeline/config/loader.py). Regression coverage:
[`test_source_sampling.py`](../tests/pipeline/test_source_sampling.py).

## Distance decay

The same configured curve is evaluated in two places: at every observer/target pixel or sample
inside the physical kernel, and at H3-centroid distance for the separately inspectable
`weight_distance` artifact. For distance `d` in kilometres, the available curves are:

```text
logistic:    D(d) = 1 / (1 + exp((d - d50) / slope))
exponential: D(d) = exp(-d / lambda)
piecewise:   D(d) = 1                              when d <= near
                    1 - (d - near) / (far - near)  when near < d < far
                    0                              when d >= far
```

When `normalize_at_zero` is enabled, the logistic curve is divided by its value at zero before
clipping. Every curve is clipped to `[0, 1]`; distances beyond the resolved hard cutoff receive
zero. The cutoff defaults to the viewshed maximum distance when it is not set separately.

`weight_distance` is evaluated at H3-centroid distance and retained with `distance_m` and
`distance_km` for inspection. The reusable pair product records the exact centroid method,
candidate coverage, H3 resolutions, role, and lineage; named profile products record normalized
effective curve parameters and preserve the complete stored pair universe. See
[distance products](distance-products.md) for their schemas and execution contract. The established
compatibility table also retains `distance_model`.

The standalone centroid weight is **not** multiplied into the static weight again because
observer-to-target distance is already integrated below. H3-level summaries cannot generally
replace the joint sample kernel: `D(mean(d))`, `mean(D(d))`, and `mean(LOS × D(d))` are distinct
quantities.

Implementation: [`weights/distance/compute.py`](../src/viewshed_toolkit/pipeline/weights/distance/compute.py)
and the equivalent raster evaluator in
[`weights/terrain/summarize.py`](../src/viewshed_toolkit/pipeline/weights/terrain/summarize.py).
Regression coverage:
[`test_distance_weight_values.py`](../tests/pipeline/test_distance_weight_values.py),
[`test_distance_products.py`](../tests/pipeline/test_distance_products.py), and
[`test_terrain_visibility_support.py`](../tests/pipeline/test_terrain_visibility_support.py).

## Land-source raster kernel

For full-pixel aggregation, the bare-earth and canopy kernels are:

```text
J_b(s,t) = [Σ(o in O_s) Σ(p in P_t) L_b(o,p)] / (N_s × M_t)

K_b(s,t) = [Σ(o in O_s) Σ(p in P_t) L_b(o,p) × D(d(o,p))]
             / (N_s × M_t)
```

`J_b` is the unweighted joint LOS fraction. `K_b` is the distance-integrated physical kernel and
is persisted as `weight_terrain` (and as `distance_weighted_los_fraction`). Thus an observer that
sees 20 of 100 target-water pixels contributes across all 100 pixels, not only across the 20
visible pixels. Multiple observers use the same target denominator and add another factor of
`N_s` to the averaging population.

`M_t` is not the number of pixels available in a batch window. It is the immutable equivalent
water-pixel count derived from the complete target H3 geometry intersected with the canonical
water polygon in the configured equal-area CRS. This makes the denominator independent of batch
extent and scheduling. A non-positive or missing denominator is an error.

With sampled raster aggregation and pixel stride `q > 1`, only every `q`th row and column is
sampled and each sampled numerator represents `q²` pixels:

```text
J_b(s,t) = q² × visible_pair_count / (N_s × M_t)
K_b(s,t) = q² × distance_weight_sum / (N_s × M_t)
```

The sampled route is an approximation and should be checked against full aggregation. Accumulated
distance sums are quantized before cross-observer reduction so completion order does not change
the result. Final support is validated and bounded to `[0, 1]`.

Two additional diagnostics answer different questions:

- `any_observer_support_fraction` is the fraction of source observers that see at least one target
  pixel.
- `union_visible_target_fraction` is the approximate fraction of canonical target-water area seen
  by at least one observer.

They are not interchangeable with `J_b` or `K_b`.

Implementation: [`weights/terrain/summarize.py`](../src/viewshed_toolkit/pipeline/weights/terrain/summarize.py),
including the domain target-water denominator, and
[`weights/terrain/pixel_index.py`](../src/viewshed_toolkit/pipeline/weights/terrain/pixel_index.py).
Regression coverage: [`test_terrain_visibility_support.py`](../tests/pipeline/test_terrain_visibility_support.py)
and [`test_aggregation_accuracy.py`](../tests/pipeline/test_aggregation_accuracy.py).

## Terrain curvature, refraction, and endpoints

Raster LOS uses the GDAL curvature convention. At projected distance `r` metres from an observer,
the equivalent corrected terrain height is:

```text
height_corrected(r) = height_dem(r) - c × r² / (2R)
```

where `c` is `curvature_coefficient` and `R` is `earth_radius_m`. A coefficient of zero is the
configured flat-Earth case. GDAL receives the coefficient directly; the alternate raster backend
applies the equation explicitly.

Observer height is added above repaired bare-earth ground at the source sample. Target height is
added above the endpoint surface. Canonical water-mask pixels in that endpoint surface are set to
the configured sea level even when the projected input DEM has nodata there.

Endpoint repair is deterministic and independent of the observer batch:

1. Prefer the value at the exact source-raster cell containing the endpoint-grid centre.
2. If that source cell is void, use the median of its immediate 3×3 neighbourhood only when at
   least three finite values exist.
3. Leave any unresolved endpoint for the configured DEM-gap policy. An unresolved observer
   endpoint fails rather than being replaced by a barrier.

Implementation: [`weights/terrain/los.py`](../src/viewshed_toolkit/pipeline/weights/terrain/los.py)
and [`prepare/elevation/terrain.py`](../src/viewshed_toolkit/pipeline/prepare/elevation/terrain.py).
Regression coverage: [`test_gdal_inprocess_viewshed.py`](../tests/pipeline/test_gdal_inprocess_viewshed.py),
[`test_open_water_kernel.py`](../tests/pipeline/test_open_water_kernel.py), and
[`test_raster_cache_fingerprints.py`](../tests/pipeline/test_raster_cache_fingerprints.py).

## Canopy surface and observer clearance

Land sources are evaluated on matched surfaces:

```text
bare surface:   endpoint DEM
canopy surface: endpoint DEM + max(CHM, 0) on DEM-defined land
water pixels:   endpoint DEM on both surfaces
```

The CHM is aligned exactly to the endpoint DEM using the configured resampling method. Heights
below `minimum_canopy_height_m` are set to zero. Before each canopy LOS call, a private surface
overlays bare-earth endpoint elevation at the observer pixel and at grid-cell centres within
`observer_canopy_clearance_radius_m` of that observer. This makes eye height relative to ground
and prevents the observer's own canopy cell from trapping the ray. The shared canopy base is not
mutated, so one observer's clearance cannot affect another observer.

Let `K_bare` and `K_canopy` be the distance-integrated kernels above. The conditional canopy
factor and final land weight are:

```text
C(s,t) = min(K_canopy, K_bare) / K_bare   when K_bare > 0
C(s,t) = 1                               when K_bare = 0

K_static(s,t) = K_bare × C
```

The minimum prevents numerical or surface anomalies from making canopy improve bare-earth
support. `C = 1` for a terrain-blocked pair is a neutral factor with
`canopy_support=terrain_blocked_neutral`, not evidence of clear canopy. `canopy_los_raw` retains
`K_canopy` as an intermediate diagnostic; `weight_vegetation` is `C`, not a final static product.

Implementation: [`prepare/elevation/canopy.py`](../src/viewshed_toolkit/pipeline/prepare/elevation/canopy.py),
[`weights/canopy_visibility.py`](../src/viewshed_toolkit/pipeline/weights/canopy_visibility.py),
and [`weights/components.py`](../src/viewshed_toolkit/pipeline/weights/components.py). Regression
coverage: [`test_canopy_surface.py`](../tests/pipeline/test_canopy_surface.py),
[`test_gdal_inprocess_viewshed.py`](../tests/pipeline/test_gdal_inprocess_viewshed.py), and
[`test_terrain_factor_composition.py`](../tests/pipeline/test_terrain_factor_composition.py).

## Water-source opaque-land kernel

Water sources use the fixed `water_viewing.source_samples_per_cell` source-role policy and
deterministic interior target-water samples, not the land raster population. For every source
observer and target sample, the kernel tests the straight segment against the canonical
mapped-land geometry plus the configured land-clearance buffer. An intersection is opaque. It
also tests the configured two-ended refracted horizon:

```text
R_effective = R / c
horizon(h)  = sqrt(2 × R_effective × h + h²)
maximum geometric range = horizon(observer_height) + horizon(target_height)
```

For `c <= 0`, the flat-Earth configuration has no geometric horizon. The horizon and opaque-land
tests are applied to each observer/target sample pair. Conservative cell-level prefilters may
skip work only when they prove the complete target is beyond the horizon; they do not declare
visibility.

The water denominators are exact for the sampled design:

```text
J_water(s,t) = [Σ(o in O_s) Σ(p in T_t) L(o,p)] / (N_s × Q_t)

K_water(s,t) = [Σ(o in O_s) Σ(p in T_t) L(o,p) × D(d(o,p))]
                 / (N_s × Q_t)
```

The water terrain weight is `K_water`. Its canopy factor is explicitly 1 with
`canopy_support=not_applicable`; that is a policy state, not measured canopy transparency.

Implementation: the water kernel and horizon in
[`weights/terrain/gdal.py`](../src/viewshed_toolkit/pipeline/weights/terrain/gdal.py), with the
shared sampler in [`prepare/area/sampling.py`](../src/viewshed_toolkit/pipeline/prepare/area/sampling.py).
Regression coverage: [`test_open_water_kernel.py`](../tests/pipeline/test_open_water_kernel.py).

## Raster gaps, sparse rows, and missing artifacts

“Missing is not zero” applies to artifacts, pair coverage, and unknown model state. It does not
forbid an explicitly configured scientific assumption about nodata pixels inside an otherwise
validated raster. These cases are intentionally different:

| Case | Meaning | Numerical treatment |
| --- | --- | --- |
| Missing or stale component artifact, incomplete source completion, missing canonical pair, or invalid provenance | The workflow cannot establish the model result | Fail; never substitute zero or a neutral factor |
| Absent row in a complete, successfully validated sparse LOS partition | The modeled pair had no visible support | Densify against the canonical pair universe and assign terrain support zero |
| Missing optional unweighted LOS diagnostic | The backend did not provide that diagnostic | Preserve null; do not report zero |
| Water canopy component | Canopy is outside the water-source model | Assign neutral 1 and record `canopy_support=not_applicable` |
| CHM nodata over DEM-defined land with policy `error` | Canopy obstruction is unknown | Fail |
| CHM nodata over DEM-defined land with policy `zero` | The configuration assumes no canopy height at those pixels | Use zero CHM and record/hash the policy; this is an assumption, not observed zero |
| Remaining non-observer DEM nodata with policy `error` | Terrain elevation is unknown | Fail |
| Remaining non-observer DEM nodata with policy `opaque_barrier` | Unknown terrain must not create false visibility | Replace it with the configured high barrier for LOS and record/hash the policy; this is conservative obstruction, not a known elevation |

Sparse-to-zero conversion is allowed only after every expected source has exactly one successful
completion record and observed sparse rows have valid non-null weights and unique keys. A failed
source, absent partition, duplicate key, null observed weight, stale hash, or pair-universe
mismatch prevents densification and composition. An empty but validated canonical universe stays
empty.

The canonical Salish Sea configuration deliberately chooses `canopy_nodata_policy: zero` and
`dem_nodata_policy: opaque_barrier`. Those regional assumptions are documented in the
[Salish Sea case study](salish-sea-case-study.md); they are not universal defaults to apply to
other regions without scientific justification.

Implementation: nodata policies in
[`prepare/elevation/terrain.py`](../src/viewshed_toolkit/pipeline/prepare/elevation/terrain.py) and
[`prepare/elevation/canopy.py`](../src/viewshed_toolkit/pipeline/prepare/elevation/canopy.py);
sparse completion and densification in
[`weights/components.py`](../src/viewshed_toolkit/pipeline/weights/components.py); pair validation
and composition in [`finalize/`](../src/viewshed_toolkit/pipeline/finalize/). Regression coverage:
[`test_raster_cache_fingerprints.py`](../tests/pipeline/test_raster_cache_fingerprints.py),
[`test_static_pair_universe.py`](../tests/pipeline/test_static_pair_universe.py),
[`test_terrain_factor_composition.py`](../tests/pipeline/test_terrain_factor_composition.py), and
[`test_component_workflow.py`](../tests/pipeline/test_component_workflow.py).

## Reproducibility boundary

Sampling algorithm versions, observer designs, source fingerprints, grid identity, configuration
hashes, and scientific hashes are part of cache and provenance validity. Integrated-model distance
changes invalidate the integrated terrain calculation even though the centroid-distance artifact
is not an upstream input to that calculation. Standalone profile changes invalidate only their
content-addressed profile outputs and do not alter integrated LOS. Separate component execution
and paired execution use the same LOS and sampling implementations; fixture parity is tested at
absolute tolerance `1e-7`.
Those fixtures establish software regression parity, not regional ecological or field-observation
validity.

The current regional parameter values and version names live in
[`configs/salish_sea.yaml`](../configs/salish_sea.yaml). The code and regression links above are
the authoritative owners for algorithm details and edge-case behavior.

Distance-free LOS and late composition from persisted sample-level LOS are possible future model
designs. They are not implemented by the standalone centroid products and would require a new
scientific contract and validation evidence.
