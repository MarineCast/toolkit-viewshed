# End-to-End Viewshed Modeling Code Audit

> **Historical OrcaCast record.** This document preserves the July 2026 audit as migration
> provenance. File locations, module names, test counts, and remediation status describe the
> OrcaCast source checkout at that time; they are not a current toolkit architecture report.

**Audit date:** 2026-07-18  
**Repository:** OrcaCast  
**Scope:** current viewshed source, configuration, focused tests, Port Angeles review bundle, direct viewability publisher, and selected existing artifacts  
**Original audit boundary:** documentation only.  
**Remediation update:** production code, configuration, tests, and the Port Angeles review bundle were corrected in a follow-up implementation pass on 2026-07-18. The original finding narratives below are retained as the before-state; the remediation table is authoritative for current status.

## Executive summary

The two release blockers and all ten P1 findings were addressed in the follow-up pass. Cleanup is contained to the exact viewshed root, publisher composition no longer applies distance twice, the pair universe uses a conservative geometry halo and role-aware same-cell policy, reusable rasters are fingerprinted, geometry operations use repaired polygonal helpers, production owns the dual-surface canopy contract, service manifests enumerate exact stage artifacts, path/config loading is deterministic, and composition begins from one authoritative lookup with explicit missingness rules.

Scientific defaults were also tightened: final H3 aggregation is full-pixel rather than stride sampled; the area-derived denominator is named `equivalent_water_pixel_count`; source designs seed disconnected polygon components; aggregate sums are labeled opportunity indices rather than probabilities; and canopy outputs carry a reproducible scenario identifier. The Port Angeles notebook starts a new v008 run so older sampled partitions cannot be reused.

Remaining work is no longer a release-blocking equation defect. It consists of empirical characterization and maintainability: compare the analytic open-water sampler against flat-ocean DEM runs over a broader morphology set, calibrate canopy assumptions across scenarios, migrate existing legacy domain directories, replace wildcard compatibility chains, narrow broad optional fallbacks, and split the large map module after its output contract is frozen.

### Original finding count

| Severity | Count | Interpretation |
|---|---:|---|
| P0 | 2 | Data-loss or materially invalid published scientific output |
| P1 | 10 | High-impact correctness, reproducibility, or workflow risk |
| P2 | 10 | Important limitation, ambiguity, or maintainability issue |
| P3 | 5 | Localized or latent risk |

Current disposition across the 27 findings: **21 resolved**, **2
mitigated/partially resolved with empirical follow-up**, **2 deliberately
deferred separate-pipeline/API refactors**, and **2 open maintainability items**.

No full regional terrain or publishing build was run. After remediation, the focused suite passes: **95 passed**. The 35 warnings are 14 upstream PyProj/NumPy point-transform warnings plus 21 deliberate deprecation warnings while existing inputs are reached through the declared `environment` → `environmental_layer` compatibility alias.

## Scope and method

The audit covered the original 59 Python files under `src/orcacast/domains/human/viewshed` plus the new production `weights/canopy_visibility.py` (60 current files), the canonical CLI and service entry points, four active viewshed configuration documents, all 20 current focused test files, the Port Angeles notebook and local Python review bundle, nine direct downstream publishing files, and selected existing Port Angeles/regional artifacts.

The review followed active calls and persisted contracts rather than module names alone:

1. CLI/service request and config normalization
2. source/target domain and pair lookup construction
3. DEM, CHM, water mask, and batch raster preparation
4. projected source sampling
5. GDAL/xarray LOS execution and open-water prefilter
6. raster-to-H3 aggregation and terrain kernel calculation
7. centroid distance diagnostic calculation
8. vegetation support/path weighting and notebook dual-surface canopy logic
9. compact artifact materialization and target/source aggregation
10. cleanup, mapping, and direct public viewability preparation

Confidence labels mean:

- **High:** directly demonstrated by code, test, schema, or deterministic smoke result.
- **Medium:** code path is clear, but impact depends on data geometry or run configuration.
- **Low:** plausible issue requiring a targeted fixture or regional comparison.

## Remediation status after the implementation pass

Status meanings: **resolved** has a code correction and regression test;
**mitigated** closes the unsafe behavior but retains an empirical follow-up;
**deferred** is deliberately outside the selected canonical pipeline or is a
maintainability refactor with no remaining scientific ambiguity.

| Finding | Status | Implemented correction and evidence |
|---|---|---|
| P0-1 cleanup containment | **Resolved** | Cleanup now resolves and validates the exact configured viewshed root, preserves declared outputs/sidecars, and never traverses sibling domain folders. `test_cleanup_safety.py` covers sibling preservation and escape refusal. |
| P0-2 publisher double distance | **Resolved** | Both model and publisher use `weight_terrain * weight_vegetation`; centroid distance is diagnostic. Publisher schema/manifest declare `distance_integrated_terrain_times_conditional_vegetation_v1`; parity tests protect `.5 * .8 = .4` independently of diagnostic distance. |
| P1-1 centroid-truncated universe | **Resolved** | Lookup construction retains a conservative H3 geometry halo (`area_lookup.py:331-337`), and the hard cutoff is evaluated at observer-to-target-pixel distance inside the terrain kernel. |
| P1-2 mixed same-cell omission | **Resolved** | `allow_active_role_self_pairs` permits distinct active land-source/water-target roles in one H3 cell while generic self-pairs remain configurable; `test_area_lookup_pairs.py` covers the truth table. |
| P1-3 open-water shortcut | **Mitigated; empirical parity remains** | The shortcut uses the exact terrain source samples, deterministic projected maximin samples inside target water, per-sample horizon and distance, persisted numerators/denominators, and DEM fallback near land or horizon. Zero-support rows remain sparse. Unit fixtures cover the kernel, horizon, and land fallback; broader flat-DEM equivalence is still an accuracy study. |
| P1-4 cache identity | **Resolved** | Projected DEM, batch DEM/mask, aligned CHM, canopy surface, and per-observer GDAL outputs now carry input/grid/parameter/observer fingerprints. `test_raster_cache_fingerprints.py` and canopy invalidation tests force cache misses after scientific input changes. |
| P1-5 invalid geometry unions | **Resolved** | Central repaired polygonal union/intersection/difference helpers are used across domain, area, land, water denominator, and vegetation geometry paths. Invalid/self-touching fixtures pass in `test_geometry_normalization.py`. |
| P1-6 notebook-only dual surface | **Resolved** | Production `weights/canopy_visibility.py` runs matched bare-earth and DTM+CHM radius viewsheds and persists `K_bare`, `K_canopy`, and conditional canopy attenuation. Canonical service land runs use this stage; landcover remains separate. |
| P1-7 service manifest/root contract | **Resolved** | Misleading request-root fields were removed. The service records exact stage-owned artifacts, config/stage signature, paths, and checksums; resume requires the same signature and required outputs. `test_service_stage_contract.py` covers inventory and invalidation. |
| P1-8 path/config drift | **Resolved in code; data migration pending** | Repository-root path normalization is deterministic from arbitrary working directories. Canonical `human`/`environment` paths are used; declared existing-input aliases reach legacy `human_layer`/`environmental_layer` artifacts with deprecation warnings. Every shipped config loads in focused tests without mutation. |
| P1-9 sparse join universe | **Resolved** | Canonical lookup keys drive composition. Duplicate keys fail; distance and vegetation require exact coverage; missing sparse terrain is explicitly zero; extra/missing anti-join samples are reported. Model and publisher share the policy and tests. |
| P1-10 boundary tests | **Resolved** | Focused coverage expanded from 52 to 93 tests, including cleanup, publisher parity, pair policy, open water, GDAL alignment/cache invalidation, config purity/path resolution, service resume, geometry repair, dual surfaces, aggregation aliasing, notebook reload ordering, and static-universe missingness. |
| P2-1 sampled aggregation aliasing | **Resolved for final defaults** | Canonical config and dataclass defaults are `aggregation_mode=full`, `pixel_stride=1`; output version names include aggregation contract. A deterministic 20×20 fixture proves a 5% one-pixel LOS stripe disappears under old stride 10 but is retained by full aggregation. |
| P2-2 misleading water pixel count | **Resolved** | Persisted schema now says `equivalent_water_pixel_count`; cache/metadata contract is v3. Internal terrain math aliases it to the denominator name only after validation. |
| P2-3 disconnected sampling | **Resolved** | `component_aware_projected_nested_maximin_v4` seeds every disconnected polygon component, allocates deterministic candidates by component area, and preserves nested prefixes. Tests cover a tiny island and mainland. |
| P2-4 sums presented as probabilities | **Resolved** | Join reports and publisher manifests label sums `additive_source_density_opportunity_index_not_probability`; p98 outputs are labeled clipped display indices, with comparability requirements. |
| P2-5 automatic diagnostic cleanup | **Resolved** | Cleanup is opt-in and preserves the reproducibility bundle unless explicitly requested after successful materialization. |
| P2-6 wildcard module stitching | **Deferred maintainability refactor** | Public behavior is now tested, but compatibility `import *` chains remain. Replacing them requires an API migration and is not coupled to the corrected scientific contract. |
| P2-7 overlapping legacy finalizer | **Resolved** | The manifest-based finalizer is retired and raises unless a caller explicitly passes `allow_legacy=True`; canonical finalization owns current artifacts. |
| P2-8 one-ray landcover model | **Deferred separate pipeline** | The selected canonical model does not use landcover. The explicit ray product remains isolated and must not be substituted for conditional canopy LOS; increasing/characterizing its design belongs to that future pipeline. |
| P2-9 canopy calibration metadata | **Partially resolved** | Every canopy artifact now records a scenario ID derived from CHM path/size/mtime, resampling, nodata, threshold, clearance, heights, resolution, and contract. Empirical parameter calibration/sensitivity remains scientific analysis rather than a hidden default. |
| P2-10 stale headers/equations | **Resolved** | Active terrain headers describe radius execution and `mean(LOS * D(distance))`; factor equation, contract, and canopy scenario are persisted in metadata. Notebook review notes were updated. |
| P3-1 observer bitmask overflow | **Resolved** | Config validation forbids more than 63 observer samples while exact `uint64` union diagnostics are used. |
| P3-2 module/function import collision | **Resolved** | Package export is now `build_artifacts`; the `build_viewability_artifacts` dotted name resolves unambiguously to the module. |
| P3-3 analytic zero-row storage | **Resolved** | Beyond-horizon analytic zeroes are omitted from sparse terrain partitions and restored as zero only against the authoritative lookup. |
| P3-4 broad optional exception fallbacks | **Open maintainability** | Critical scientific alignment/coverage paths now fail closed, but several optional metadata/download/cleanup branches still use broad warning fallbacks. Narrowing all of them is a separate reliability cleanup. |
| P3-5 large map module | **Open maintainability** | Map contracts and interactions are tested, but the module remains monolithic. Split after the export schema and notebook UX are frozen. |

The historical detailed findings below describe the pre-remediation evidence and
recommendations. They are intentionally retained so each change can be traced
back to the observed failure mode.

## Current workflow and scientific contract

```text
CLI/service
  -> normalize/load config (read-only)
  -> explicit runtime initialization
  -> source/target domains + canonical pair lookup
  -> domain-wide target-water denominator
  -> projected nested source samples
  -> per-batch endpoint DEM + water mask
  -> bare-earth or canopy obstacle surface
  -> radius-based GDAL viewshed per observer sample
  -> accumulate visible observer/pixel pairs and D(distance)
  -> summarize to source_h3, target_h3
  -> compact terrain/distance/vegetation factors
  -> static pair weights and target/source summaries
  -> public viewability publisher
```

The production terrain path is **radius-based sparse viewshed execution per observer**, not explicit pairwise ray tracing. Each observer sample generates a GDAL viewshed raster out to `max_distance_m`; visible water pixels are accumulated and then summarized to canonical target H3 cells. The vegetation path package is a separate explicit source-target ray sampler. The Port Angeles notebook instead runs the radius terrain pipeline twice, once against bare earth and once against DEM+CHM, then derives conditional canopy attenuation.

The authoritative terrain equation implemented at `weights/terrain/summarize.py:726-856` is:

```text
weight_terrain(s,t)
  = sum over sampled visible observer/pixel pairs D(distance_i,j) * stride^2
    / (number of actual observer samples * complete target-water pixel equivalent)
```

The unweighted diagnostics are:

```text
any_observer_support_fraction = visible observer count / observer count
union_visible_target_fraction = union visible target area / complete target-water area
joint_los_fraction             = visible observer-pixel pairs / all observer-pixel pairs
```

For the current distance-integrated terrain contract, final physical pair weight should be:

```text
weight_static_viewability = weight_terrain * weight_vegetation
```

`weight_distance` is a centroid-based diagnostic and must not be multiplied again.

The notebook's intended dual-surface semantics are:

```text
K_bare                 = mean(bare_LOS   * D(observer,pixel distance))
K_canopy               = mean(canopy_LOS * D(observer,pixel distance))
weight_terrain         = K_bare
weight_vegetation      = K_canopy / K_bare when K_bare > 0, otherwise 1
weight_static          = K_canopy
```

## Detailed findings

### P0-1 — Cleanup helper can delete sibling domain data

**Category:** implementation reliability / data safety  
**Confidence:** High  
**Evidence:** `utils/final_artifacts.py:448-519`; public call in `finalize/cleanup.py:127-141`

`cleanup_data_contract` sets `data_dir = paths.output_dir.parent` and, when `remove_stage_scratch=True`, recursively deletes every child of that parent except the configured viewshed directory, a narrow raster allowlist, and explicitly preserved paths. With the normal output `data/processed/domain/human/viewshed`, the parent is `data/processed/domain/human`; unrelated human-domain datasets are within the deletion set. This contradicts the function's viewshed-only docstring.

**Impact:** irreversible removal of unrelated processed data when the public cleanup path is invoked.

**Recommended correction:** remove parent-directory traversal entirely. Restrict deletion to a resolved, validated descendant of the exact configured viewshed root. Require an allowlisted scratch directory and refuse roots equal to or above `data/processed/domain/human`. Add dry-run output and a path-containment assertion.

**Validation criteria:** a temporary tree containing `human/viewshed`, `human/population`, and `human/daylight` must preserve both siblings; symlink and `..` escape fixtures must fail closed.

### P0-2 — Direct publisher applies distance decay twice

**Category:** scientific correctness / publishing  
**Confidence:** High  
**Evidence:** `publishing/prep/viewability/base.py:22-73`; `viewshed/utils/final_artifacts.py:713-728`; `viewshed/finalize/artifacts.py:367-452`; `tests/viewshed/test_viewability_composition.py:11-35`

The viewshed package explicitly defines `weight_terrain` as distance-integrated and composes static visibility as `weight_terrain × weight_vegetation`. The publisher instead calculates:

```python
weight_distance * weight_terrain * weight_vegetation
```

The focused test protects the model finalizer but does not exercise the publishing package.

**Impact:** published base viewability is systematically suppressed with centroid distance, distorting target rankings, source viewyness, effective radii, p98 normalization, and all downstream dynamic scores.

**Recommended correction:** make `base_source_target_weight = weight_terrain * weight_vegetation`; retain centroid `weight_distance` only as a diagnostic. Put the equation and factor-contract version in the publisher manifest.

**Validation criteria:** publisher unit test with `terrain=.5`, `vegetation=.8`, and any diagnostic distance weight must always produce `.4`; end-to-end model and publisher pair tables must agree exactly.

### P1-1 — The canonical pair universe is truncated by centroid distance

**Category:** scientific correctness  
**Confidence:** High  
**Evidence:** `prepare/area_lookup.py:129-202`; target restriction in `weights/terrain/batches.py:471-499`

Candidate cells are created with an H3 grid disk and then removed when centroid-to-centroid haversine distance exceeds the cutoff. The terrain kernel, however, uses land observer samples and target-water pixels. A target centroid may be outside 30 km while an active source point and near target-water edge are inside it.

**Impact:** valid LOS-distance contributions at the radius boundary are never evaluated; the final support footprint is anisotropically clipped by H3 geometry and coastline placement.

**Recommended correction:** construct a conservative halo using minimum active-geometry distance or the cutoff plus source/target H3 circumradii. Apply the hard cutoff only to observer-pixel distances inside aggregation. Keep centroid distance as a diagnostic.

**Validation criteria:** edge fixtures must retain any pair with at least one observer/target-water pixel combination within the cutoff and contribute zero for all combinations beyond it.

### P1-2 — Same-cell mixed land/water pairs are excluded

**Category:** scientific correctness  
**Confidence:** High  
**Evidence:** `prepare/area_lookup.py:146-150`; `configs/salish_sea.yaml` `source_target_lookup`

The active lookup supports mixed shoreline cells as land sources, water sources, and water targets, but `allow_self_pairs` is false. A same-H3 source/target key is therefore discarded even when its source geometry is land and its target geometry is water.

**Impact:** the closest and often strongest shoreline opportunities are missing for land observers; same-cell water-source visibility is also omitted.

**Recommended correction:** make self-pair policy role-aware. Permit land-active-to-water-active and water-active-to-water-active pairs when both geometries have positive coverage.

**Validation criteria:** a synthetic mixed H3 cell must yield distinct valid land→water and water→water pair rows while invalid empty-role combinations remain absent.

### P1-3 — Open-water shortcut is not equivalent to the DEM observer-pixel kernel

**Category:** scientific correctness  
**Confidence:** High  
**Evidence:** `weights/terrain/gdal.py:533-613,650-700`

The shortcut is much safer than the retired unconditional `1.0`: it computes a refracted horizon, rejects land-near paths, and uses a horizon transition. It still uses one representative source point, a representative target point plus sampled exterior vertices, and the canonical centroid distance for the first horizon gate and distance decay. Target samples are not water-area weighted.

At the horizon, centroid distance can set the entire target to zero even when its near edge is visible. Inside the horizon, boundary sampling does not estimate the mean over target-water pixels. Shortcut rows also do not persist the `los_distance_weight_sum` and complete denominator needed to reproduce their nonzero `weight_terrain`; storage fills absent diagnostics with zero.

**Impact:** water-source weights are discontinuous across shortcut/DEM classification, non-reproducible from their persisted diagnostics, and biased near horizons and irregular target water geometries.

**Recommended correction:** either run the same observer/pixel kernel on open water using analytic curvature LOS per sampled observer and target-water pixel, or persist equivalent weighted numerators/denominators from an area-weighted analytic sampler. Use geometry-level distance, not centroid distance, for both horizon and decay.

**Validation criteria:** flat-ocean analytic and DEM fixtures must agree within declared tolerance across target shapes, horizon margins, source sampling counts, and shortcut transitions.

### P1-4 — Reusable rasters and per-observer LOS outputs are under-fingerprinted

**Category:** reproducibility / cache correctness  
**Confidence:** High  
**Evidence:** `prepare/area.py:829-915`; `prepare/canopy_surface.py:130-183,267-283`; `weights/terrain/los.py:247-304`

Batch names and raster paths do not capture all source content, algorithm version, observer coordinates, CHM nodata policy, resampling, threshold, or clearance inputs. Existing aligned CHM and canopy obstacle surfaces are accepted on grid compatibility alone. Retained per-observer GDAL rasters are accepted as valid rasters without a sidecar proving observer/config/input identity.

**Impact:** a partition may be rebuilt from stale intermediate rasters after source sampling, CHM, or canopy parameters change, producing a metadata-compatible but scientifically stale result.

**Recommended correction:** use immutable content-addressed names or sidecars including input checksums/size+mtime, grid fingerprint, complete relevant config, observer coordinates, algorithm version, and producer version. Reject missing or mismatched metadata.

**Validation criteria:** changing any scientific input or observer coordinate must force a cache miss; unchanged runs must reproduce byte-identical or numerically identical output.

### P1-5 — Active geometry unions can still fail on invalid polygons

**Category:** implementation reliability  
**Confidence:** High  
**Evidence:** `prepare/area.py:621-665`; `prepare/domains.py:147-190`

Active domain/batch code still calls Shapely `unary_union` directly on AOI, clipped water, Natural Earth land, and combined parts without the validity repair used by the domain-wide denominator. This is the same class of operation that produced the observed `TopologyException: side location conflict` in the Port Angeles workflow.

**Impact:** legitimate regional input can fail before LOS, with behavior depending on GEOS version and clipping topology.

**Recommended correction:** centralize polygonal normalization (`make_valid`, polygon extraction, precision strategy, empty filtering, safe union) and use it for every domain union/intersection.

**Validation criteria:** bow-tie, self-touching, sliver, mixed collection, and real failing geometry fixtures must return valid polygonal output or a precise source-feature diagnostic.

### P1-6 — Production does not orchestrate the notebook's dual-surface canopy model

**Category:** scientific contract / workflow drift  
**Confidence:** High  
**Historical evidence:** the audit observed a single `surface_model` in `config/loader.py:244-245,472-478` and notebook orchestration in the former Port Angeles review bundle. The notebook and its local helpers were retired on 2026-07-19 after production orchestration and static-map export replaced them; legacy path vegetation remains under `weights/vegetation/*`.

Production can run a terrain job as either `bare_earth` or `canopy`, but the canonical service does not run both and derive conditional `K_canopy/K_bare`. The Port Angeles notebook implements that logic locally. The production `build-vegetation-path-weights` stage remains a separate CHM+landcover raster-ray model, while the current notebook intentionally excludes landcover.

**Impact:** notebook results and canonical regional outputs can use materially different definitions for `weight_vegetation` while sharing the same column name.

**Recommended correction:** version and name the two vegetation products separately. Promote a canonical dual-surface stage if that is the selected model; keep landcover/path attenuation as a separate optional pipeline and never silently substitute it.

**Validation criteria:** metadata must state `conditional_canopy_los_ratio_no_landcover` or the explicit alternative; canonical and notebook fixtures must match on identical inputs.

### P1-7 — Canonical service roots and manifests do not control or identify the run

**Category:** orchestration / reproducibility  
**Confidence:** High  
**Evidence:** `service.py:23-40,43-110`; canonical CLI request at `src/orcacast/cli/__main__.py:683-701`

`ViewshedRequest` accepts `data_root`, `artifact_root`, and `output_root`, but `_stage_arguments` forwards only config, stage, source type, and overwrite. Stage output remains config-driven. After execution, the service recursively inventories every Parquet/TIFF under two broad roots and records all of them as outputs for this run. Resume skips when those broad references still checksum-match.

**Impact:** isolation flags are misleading; manifests can claim stale or unrelated artifacts; resume can skip a configuration whose actual required outputs were never generated.

**Recommended correction:** pass explicit runtime roots into normalized config or remove the fields. Have each stage return exact artifact refs and merge only those into the run manifest. Resume must validate required artifacts for the active stage/config contract.

**Validation criteria:** isolated temporary roots receive all writes, unrelated pre-existing files never enter the manifest, and deleting one required output invalidates resume.

### P1-8 — Config/path contracts drift across active entry points and existing artifacts — resolved 2026-07-19

**Category:** configuration / reproducibility  
**Confidence:** High  
**Evidence:** `configs/salish_sea.yaml`; config smoke test; selected artifact inspection

Viewshed modeling and publishing now share one canonical repository-root-relative config at `configs/salish_sea.yaml`. The prior human-data, standalone project, experiment, and viewability configs were removed, and runtime defaults now point to the canonical file.

Existing regional artifacts are under `data/processed/domain/human_layer/viewshed`; the current compact contract and publisher target `data/processed/domain/human/viewshed`. The publisher's configured `LAND_STATIC_WEIGHTS_R7.parquet` does not exist at audit time.

**Impact:** the nominal canonical configs are not interchangeable or independently runnable, and publishing can fail or consume stale files from a different tree.

**Recommended correction:** establish one repository-root-relative path resolver and one canonical domain naming contract; validate every shipped config in CI. Add a deliberate migration/compatibility layer rather than implicit fallback search.

**Validation criteria:** all shipped configs load from repository root and arbitrary working directories, resolve the same canonical data, and report missing paths before any mutation.

### P1-9 — Sparse joins can silently redefine the modeled pair universe

**Category:** schema/artifact correctness  
**Confidence:** High  
**Evidence:** `weights/view_score.py:24-67,71-130`; `utils/final_artifacts.py:758-816`; publisher left joins at `publishing/prep/viewability/base.py:47-67`

Model finalizers use inner joins between terrain, vegetation, and distance. Existing inspected artifacts have 1,732,112 terrain/distance rows but 4,471,688 vegetation rows; the join report records counts but does not fail on loss. The publisher instead starts from lookup and left joins, filling missing vegetation with `0.0`, whereas model composition treats a present null vegetation factor as neutral `1.0` but requires the pair to exist.

**Impact:** missingness has three incompatible meanings: row removal, full obstruction, or neutral attenuation. Target sums and candidate counts depend on which consumer is used.

**Recommended correction:** define one authoritative pair universe and per-factor missingness policy. Validate key uniqueness and exact or declared coverage before composition; persist anti-join samples and fail above zero/tolerance.

**Validation criteria:** empty, zero, neutral, and missing factor fixtures produce identical results in model finalizers and publisher; duplicate keys are hard failures.

### P1-10 — Test suite does not exercise the highest-risk workflow boundaries

**Category:** test coverage  
**Confidence:** High  
**Evidence:** eight files under `tests/viewshed`; 52 passing tests

Current tests cover canopy surface arrays, distance evaluator parity, map helpers, projected/nested source sampling, terrain composition, empty/nonempty terrain schema, terrain aggregation math, and the no-double-distance model finalizer. There are no focused tests for service/CLI orchestration, shipped configs/path resolution, pair lookup/self pairs, open-water shortcut, GDAL grid offset failure, cache invalidation, cleanup containment, full vegetation path workflow, final join coverage, or the direct publisher.

**Impact:** the P0 findings and most P1 findings can pass the entire focused suite.

**Recommended correction:** add contract tests around boundaries rather than more isolated helper tests; make destructive-cleanup and publisher-equation tests mandatory.

**Validation criteria:** each P0/P1 remediation below has a failing-before/passing-after regression test.

### P2 findings

#### P2-1 — Sampled terrain aggregation can alias narrow coastal visibility

`summarize.py:517-533` uses one deterministic global stride phase and the default config uses `pixel_stride: 10`. Hits are expanded by `stride²`. Narrow target slivers or LOS corridors can be entirely missed or overrepresented. Use full aggregation for final products or stratified/multi-phase sampling with uncertainty and convergence testing.

#### P2-2 — `total_water_pixel_count` is an equivalent count, not a canonical raster count

`summarize.py:422-455` divides equal-area polygon intersection area by nominal DEM pixel area and rounds. The numerator comes from raster samples on a specific grid. Keep the area denominator, but rename this field to `equivalent_water_pixel_count` or compute an immutable canonical raster count on the exact aggregation grid.

#### P2-3 — Fixed 32×32 sampling candidates can miss small disconnected components

`prepare/area.py:242-337` is metric and nested, but its candidate lattice is bbox-based and not component- or area-stratified. A thin island/sliver can receive no candidate. Seed each polygon component and allocate candidates by active area, preserving a deterministic nested global order.

#### P2-4 — Target/source sums are source-density metrics, not probabilities

`weights/view_score.py:43-55,106-118` sums pair weights across source H3 cells. Values can exceed 1 (an inspected existing target score reaches 13.80) and scale with source universe density and H3 resolution. Document them as exposure/opportunity indices, not visibility probabilities; if a probability is desired, define a sampling/population measure or saturating union model.

#### P2-5 — Finalization command always deletes diagnostic artifacts

`finalize/cleanup.py:143-166` accepts `--clean-intermediates` but always calls `cleanup_viewshed_dir_to_static_outputs`, which retains only two static Parquets. This is narrower than P0-1 but removes lookup, factor tables, sidecars, manifests, and diagnostics needed to audit the final result. Make cleanup opt-in and preserve a reproducibility bundle.

#### P2-6 — Wildcard module stitching creates hidden APIs and import fragility

Large modules repeatedly `import *` from a predecessor and then copy its globals. `utils/utils.py` is effectively a re-export chain from config through DEM/area. This hides ownership, permits private reach-through, inflates import side effects, and makes a file-by-file audit misleading. Replace it with explicit imports and small typed interfaces.

#### P2-7 — Legacy and current finalizers coexist with overlapping artifact names

`finalize/artifacts.py:466-630` retains a legacy manifest finalizer while `utils/final_artifacts.py` owns current compact paths and `weights/view_score.py` creates another summary family. Deprecation is logged rather than enforced. Consolidate one canonical finalization API and give legacy outputs an explicit schema/version namespace.

#### P2-8 — Vegetation ray model uses a single paired ray in the shipped config — superseded 2026-07-19

The canonical config uses matched bare-earth and canopy radius viewsheds for the production vegetation factor and explicitly keeps land-cover attenuation separate. Any future land-cover ray pipeline should use deterministic nested designs and publish sampling uncertainty.

#### P2-9 — Canopy assumptions need calibration metadata

The canopy surface correctly uses endpoint DEM at observer pixels and water, with DEM+CHM on intervening land (`prepare/canopy_surface.py:297-338`). Results remain sensitive to CHM maximum resampling, minimum canopy threshold, zero/error nodata policy, and observer clearance. These need scenario identifiers, empirical justification, and sensitivity outputs; they must not be hidden behind a generic vegetation label.

#### P2-10 — Existing documentation and module headers lag current equations

Several terrain module headers still call `weight_terrain` a bare-earth visibility-support factor even though it may use a canopy surface and always integrates distance. The July 13 review describes superseded behavior. Update documentation only after contract selection, and encode equation/version in artifact metadata.

### P3 findings

1. **Observer mask limit:** `summarize.py:870-885` allocates a unique observer bitmask only for at most 63 observers. The fallback cannot recover a union of distinct observers from max count. Current 5/10-sample configs are unaffected; validate or forbid higher counts.
2. **Import namespace collision:** `import orcacast.publishing.prep.viewability.build_viewability_artifacts as x` can bind the package-exported function rather than the module because `__init__.py` exports the same name. `importlib.import_module` works. Avoid module/function name collision.
3. **Water shortcut zero rows reduce sparsity:** beyond-horizon pairs are materialized as zero-support rows. This is correct numerically but wastes storage and alters candidate-row counts unless downstream semantics explicitly include zeros.
4. **Exception fallbacks are broad:** optional metadata, geometry, and cleanup helpers frequently catch `Exception` and warn/continue. Narrow exceptions and attach structured degraded-mode metadata.
5. **Map code is production-scale notebook logic:** `map_artifacts.py` is 1,496 lines and mixes aggregation, smoothing, HTML interaction, exports, and metadata. Split only after map contracts are frozen; current focused tests cover key interaction helpers.

## Scientific contract verification

| Contract | Status | Evidence and qualification |
|---|---|---|
| Projected source sampling | Verified | Active geometries are transformed to the configured projected CRS and returned to WGS84 only after selection. |
| Nested/component-aware sample designs | Verified | Component seeds and the ordered maximin sequence are independent of requested prefix; tests cover adaptive ⊂ fixed and tiny-island coverage. |
| Radius-based terrain LOS | Verified | One GDAL viewshed per observer sample, aggregated to H3; not explicit pair rays. |
| Curvature/refraction | Verified for GDAL inputs and shortcut horizon | Coefficient passed to GDAL; analytic horizon uses the same coefficient/radius convention. Cross-backend numerical parity is untested. |
| Observer/target heights | Verified | Config values pass to GDAL; canopy observers are grounded to endpoint DEM before observer height is added. |
| DEM+CHM obstacle semantics | Verified | Land obstacle is endpoint DEM + aligned CHM; observer and water pixels use endpoint DEM. |
| Raster grid alignment | Resolved | `los.py:643-687,729-764` requires CRS, signed grid/orientation, dimensions/window containment, and integer-pixel origin alignment. |
| Nodata | Explicit but scenario-sensitive | CHM error/zero policies are explicit; zero is only scientifically safe for datasets whose missing values mean no canopy. |
| Complete target-water area | Resolved | Equal-area full H3-water intersection is immutable by batch; the area-derived count is explicitly named `equivalent_water_pixel_count`. |
| Observer-pixel distance integration | Verified | DEM aggregation and analytic open-water samples apply the same distance evaluator per source/target sample; broader shortcut-versus-flat-DEM error characterization remains. |
| Joint observer-pixel denominator | Verified for DEM path | Required numerators/denominators, positive checks, and actual sample denominator are tested. |
| Bare versus canopy monotonicity | Production verified | Canonical dual-surface composition caps canopy at bare support and persists conditional `K_canopy/K_bare`; scalable and in-memory fixtures agree. |
| Vegetation/landcover separation | Canonical | Dual-surface canopy explicitly excludes landcover; the ray/landcover workflow remains a separately named pipeline. |
| Final distance interpretation | Resolved end to end | Model and publisher treat centroid distance as diagnostic and do not multiply it after the distance-integrated terrain kernel. |
| Source-density aggregation | Explicit opportunity semantics | Sum, mean, max, and count persist; reports/manifests state that sums and p98 display indices are not probabilities. |

## Re-evaluation of the July 13 review

| Older issue | Current status | Current evidence |
|---|---|---|
| Direct `utils.final_artifacts` import circularity | **Resolved** | Direct imports pass; loader no longer imports final artifacts through the old cycle. |
| Config loading creates directories/metadata | **Resolved** | `load_app_config` is read-only; `initialize_app_config` owns mutations (`loader.py:439-445,611-632`). |
| Open water automatically receives `1.0` | **Resolved; characterization remains** | Source/target samples receive per-pair horizon and distance evaluation; ambiguous land/horizon cases use DEM LOS and zero rows stay sparse. |
| Batch-clipped target-water denominator | **Resolved** | Complete equal-area H3-water table is computed once and required by batches. |
| Same-shape GDAL transform mismatch accepted | **Resolved** | Hard grid validation now rejects CRS, orientation, pixel-size, and origin mismatch. |
| Terrain weight used any-observer/union support | **Resolved** | Primary factor is distance-weighted joint observer/pixel fraction; older values remain diagnostics. |
| Distance used only H3 centroids | **Resolved for physical weights** | DEM and analytic kernels integrate sample-level distance. Lookup/publisher retain centroid distance only as candidate/diagnostic metadata. |
| Sampling performed in longitude/latitude | **Resolved** | Source design is built in an estimated/configured projected CRS. |
| Sample sets not nested | **Resolved** | Component-aware projected design is deterministic and nested through the configured maximum. |
| Vegetation path mistaken for radius viewshed | **Resolved contract split** | Canonical canopy is a second radius viewshed; landcover/path attenuation remains an explicitly separate ray pipeline. |

## Validation performed

### Focused tests

Command:

```bash
python -m pytest tests/viewshed -q
```

Result after remediation: **95 passed**. There are 14 upstream PyProj/NumPy point-transform warnings and 21 deliberate compatibility-alias deprecation warnings while current local inputs still live under legacy domain directory names.

### Import smoke tests

The following imported through `importlib` without launching terrain work:

- `orcacast.domains.human.viewshed`
- `orcacast.domains.human.viewshed.viewshed_pipeline`
- `orcacast.domains.human.viewshed.utils.final_artifacts`
- `orcacast.publishing.prep.viewability.build_viewability_artifacts`

CLI `main` and publisher `build_viewability_artifacts` were callable.

### Config smoke tests

- `configs/salish_sea.yaml`: loaded to the Fort Worden bbox at H3 R7, 30 km, bare earth, with isolated output under `data/processed/domain/human/viewshed/experiments`.
- The canonical config loads from both repository root and an arbitrary working directory.
- Config loading is a pure read: a fixture with a nonexistent output tree creates no directories or metadata.
- Existing `environmental_layer` inputs are reached only through a declared deprecation-warning alias while canonical paths use `environment`.
- The canonical human config uses full aggregation (`pixel_stride=1`).

### Empty/nonempty schema validation

Focused tests verify empty and nonempty terrain partitions both persist string H3 keys. Terrain aggregation requires all joint numerator/denominator fields and rejects invalid observer/pixel denominators. Compact factor schemas are exact-order validated; canonical composition tests cover duplicate keys, exact distance/vegetation coverage, sparse terrain zero-fill, anti-join reporting, and shortcut diagnostic reproducibility.

### Existing artifact sample

Existing files under `data/processed/domain/human_layer/viewshed` were inspected as historical/runtime evidence, not assumed current:

| Artifact | Rows | Observed schema/range |
|---|---:|---|
| `TERRAIN_WEIGHTS_H3R7.parquet` | 1,732,112 | compact keys + `weight_terrain`, range 0–1 |
| `DISTANCE_WEIGHTS_H3R7.parquet` | 1,732,112 | distance 2.0695–29.99997 km; weight 0.000419–0.928697 |
| `VEGETATION_WEIGHTS_H3R7.parquet` | 4,471,688 | compact keys + weight, range 0–1 |
| `LAND_PHYSICAL_VIEW_SCORE_H3R7.parquet` | 7,062 | historical three-column target summary; score 0–13.8018 |

The row-count mismatch demonstrates that join coverage must be explicit. The configured current publisher input `data/processed/domain/human/viewshed/LAND_STATIC_WEIGHTS_R7.parquet` was absent.

### Numerical fixtures represented by tests

- NumPy and Polars distance curves agree.
- Terrain accumulation uses projected observer-to-pixel distance.
- The distance-weighted joint fraction is primary and clipped to `[0,1]`.
- Actual observer count has denominator priority.
- Invalid numerator/denominator combinations fail.
- Canopy surfaces use DTM at observer/water pixels and DTM+CHM over intervening land.
- Conditional canopy composition is monotonic.
- Model final composition does not multiply centroid distance twice.
- Publisher composition exactly matches the model equation.
- Full aggregation retains a narrow 5% LOS stripe that stride 10 misses.
- Invalid polygonal inputs repair or fail with a precise diagnostic.
- CHM, DEM, water, source design, and observer changes invalidate their caches.
- Canonical dual-surface composition preserves the complete lookup and monotonic canopy contract.

Not validated without a targeted follow-up fixture or regional comparison:

- GDAL versus analytic flat-ocean parity
- regional full-versus-sampled convergence by coastal morphology (the deterministic alias failure is tested)
- cross-backend GDAL/xarray equivalence
- cross-source canopy parameter sensitivity on field-validated scenarios
- a full regional publisher rebuild from the new canonical artifact tree

## Port Angeles notebook and local bundle comparison

The review bundle is intentionally importable beside the notebook. Nine copied production modules were compared line-by-line:

- `map_artifacts.py` and `prepare/canopy_surface.py` are byte-equivalent to production.
- terrain `batches.py`, `cleanup.py`, `gdal.py`, `los.py`, and `runner.py` differ only in import paths.
- `prepare/area.py` differs in import paths and formatting-equivalent expressions.
- terrain `summarize.py` differs in import paths and removal of a comment, not behavior.

The nine copied implementation modules were resynchronized after remediation and mechanically verified to be semantically identical after applying only the documented local import adaptations. Notebook-specific `runtime_support.py` and `terrain_factors.py` remain deliberate additions. Production now also owns canonical dual-surface composition in `weights/canopy_visibility.py`; the notebook helper adds interactive experiment orchestration and grid audit output.

The bundle is reviewable but not self-contained: it still imports the installed OrcaCast package, core H3/config helpers, and external Python/GDAL dependencies; it references repository data and runtime-generated configs/artifacts. `__pycache__` entries are generated runtime debris, not source. The notebook deliberately starts a new `v008` experiment using component-aware sampling and full aggregation. Its older embedded v005 outputs remain historical and were not treated as current evidence.

## Remediation roadmap execution record

| Work package | Status | Exit evidence / remaining action |
|---|---|---|
| A — Safety stop | **Complete** | Root containment, sibling preservation, opt-in cleanup, and reproducibility preservation are regression tested. |
| B — Publishing equation | **Complete** | Model and publisher pair fixtures are equal and the factor contract is in the manifest. A regional publish rebuild is intentionally not part of this code pass. |
| C — Pair universe | **Complete** | Geometry halo, role-aware same-cell policy, and sparse-zero semantics have deterministic fixtures. |
| D — Water kernel | **Implementation complete; error study open** | Common projected source/target design, per-sample horizon/distance, numerators, denominators, land/horizon fallback, and sparse zeroes are implemented. Compare against a flat DEM over a morphology/distance matrix before claiming a regional error bound. |
| E — Cache/provenance | **Complete for active terrain path** | Input/parameter/grid/observer changes invalidate the relevant DEM, CHM, surface, and GDAL caches. |
| F — Canopy semantics | **Complete** | Production owns versioned dual-surface canopy LOS; landcover is a separate pipeline; notebook and production composition fixtures agree. |
| G — Config/service/artifacts | **Complete in code; directory migration open** | Configs load purely from arbitrary CWD, service outputs/resume are exact, and factor coverage is enforced. Existing legacy directories should be migrated after old artifacts are archived. |
| H — Accuracy characterization | **Partially complete** | Full aggregation, component-aware sampling, honest aggregate semantics, and scenario metadata are complete. Regional canopy sensitivity, cross-backend parity, and empirical error bounds remain analysis tasks. |

## File-by-file appendix

Legend: **T** focused test exists; **I** indirectly tested/imported; **U** no focused coverage found; **D** duplicated in the Port Angeles bundle.

### Viewshed package — root, config, service, finalization, utilities

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `viewshed/__init__.py` | Lazy package exports | Public compatibility surface; I; keeps import light. |
| `analysis.py` | Analysis helpers for viewshed artifacts | Private/interactive; U; legacy naming risk. |
| `config/__init__.py` | Explicit config exports | Public package API; import smoke passes; I. |
| `config/loader.py` | Typed config, validation, runtime initialization | Public; T/I; pure load, full aggregation default, observer-mask cap, and canopy contract validation. |
| `config/paths.py` | path resolution, metadata naming, config hashes | Public/shared; T/I; canonical roots plus explicit warned legacy-input aliases. |
| `config/schema.py` | compact/nested normalization and YAML load | Public; I; multiple config shapes increase drift risk. |
| `finalize/__init__.py` | finalization exports | Public compatibility surface; U. |
| `finalize/artifacts.py` | retired manifest merge/composition | Public legacy; T; disabled unless `allow_legacy=True`. |
| `finalize/cleanup.py` | static materialization and opt-in cleanup CLI | Public; T through cleanup contract; exact-root containment. |
| `map_artifacts.py` | H3 aggregation, smoothing, map/HTML/TIFF/PNG exports | Public notebook helper; T, D; large mixed responsibility P3-5. |
| `service.py` | canonical workflow service and run manifest | Public service; T; exact stage inventory, signatures, checksums, and deterministic resume. |
| `utils/__init__.py` | utility exports | Public compatibility surface; I. |
| `utils/config.py` | config compatibility exports | Public legacy shim; U; duplication/import indirection. |
| `utils/final_artifacts.py` | canonical paths/schemas, pair coverage, atomic Parquet, safe cleanup | Public; T/I; authoritative universe and explicit factor missingness. |
| `utils/source_cell_audit.py` | traces one source across lookup/factors/final static output | Public CLI diagnostic; U; useful but depends on stable contract. |
| `utils/utils.py` | wildcard compatibility chain over config/DEM/area | Public legacy shim; U; hidden API P2-6. |
| `viewshed_pipeline.py` | canonical subcommand router | Public CLI; import smoke only; U for command dispatch/end-to-end behavior. |
| `weights/view_score.py` | canonical pair composition and target summaries | Public CLI; T/I; no double distance, lookup-owned sparse policy, opportunity-index metadata. |
| `weights/water_los.py` | retired analytic water LOS implementation | Deprecated public file; U; command errors intentionally, remove/version archive. |

### Viewshed package — preparation

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `prepare/area.py` | source samples, AOIs, water masks, batch context | Core; T, D; projected nested component-aware sampling and repaired geometry operations. |
| `prepare/area_cli.py` | area/source-target CLI | Public; U; compatibility imports. |
| `prepare/area_config.py` | lookup config dataclasses/validation | Public; T/I; role-aware same-cell and resolution contracts. |
| `prepare/area_lookup.py` | H3 source-target universe and centroid diagnostic | Core; T; conservative geometry halo and active-role same-cell policy. |
| `prepare/canopy_surface.py` | aligned CHM and DEM+CHM obstacle surface | Core; T, D; endpoint semantics and full cache fingerprinting. |
| `prepare/dem.py` | shared DEM/raster helpers and compatibility chain | Core/shared; I; broad module stitching. |
| `prepare/domains.py` | source/target active domains and H3 geometry | Core; T/I; repaired polygonal union/intersection paths. |
| `prepare/prepare_area.py` | compatibility exports for area prep | Public shim; U; duplication. |
| `prepare/prepare_dem.py` | DEM download/mosaic/warp | Public stage; U; network/cache/concurrency not fixture-tested. |
| `prepare/prepare_land.py` | Natural Earth land and land H3 build | Public stage; U; external-data provenance and geometry validity. |
| `prepare/prepare_vegetation.py` | compatibility vegetation-prep exports | Public shim; U. |
| `prepare/source_universe.py` | source cell role/universe construction | Core; T/I; mixed-role contract exercised through lookup fixtures. |
| `prepare/vegetation_cli.py` | CHM/landcover preparation CLI | Public; U; separate pipeline should be explicit. |
| `prepare/vegetation_common.py` | raster download/warp/mosaic utilities | Shared internal; U; broad exception fallbacks. |
| `prepare/vegetation_rasters.py` | CHM/landcover regional raster preparation | Public stage; U; cache/provenance coverage absent. |
| `prepare/vegetation_sources.py` | remote tile/source discovery and downloads | Internal; U; external schema and exception fallback risk. |

### Viewshed package — distance

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `weights/distance/__init__.py` | distance exports | Public; I. |
| `weights/distance/__main__.py` | module CLI entry | Public; U. |
| `weights/distance/cli.py` | partitioned distance factor stage/materialization | Public; U; uses canonical centroid lookup diagnostic. |
| `weights/distance/compute.py` | Polars expressions, NumPy evaluator, models | Core; T; evaluator parity verified; zero normalization supported. |
| `weights/distance/config.py` | strict distance config schema | Public/core; T/I; unknown-field rejection is good; version contract needed. |

### Viewshed package — canonical canopy composition

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `weights/canopy_visibility.py` | runs matched bare/canopy radius kernels and persists conditional attenuation | Public stage; T; production dual-surface contract, full lookup preservation, monotonic cap, sidecars, and canopy scenario ID. |

### Viewshed package — terrain

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `weights/terrain/__init__.py` | stitched terrain exports | Public; I, D equivalent init; hidden API P2-6. |
| `weights/terrain/__main__.py` | module CLI entry | Public; U. |
| `weights/terrain/batches.py` | batch context execution and source results | Core; I, D; lookup-bounded execution with accurate surface/distance header. |
| `weights/terrain/cleanup.py` | partition combination, compact materialization, batch cleanup | Core/public helpers; T/I, D; sparse schemas and constrained cleanup. |
| `weights/terrain/cli.py` | land/water terrain commands and height classes | Public; I; production dual-surface stage is exposed by the parent router/service. |
| `weights/terrain/gdal.py` | analytic water kernel, partition schema, batch alignment | Core; T, D; per-sample horizon/distance, DEM ambiguity fallback, reproducible diagnostics, sparse zeroes. |
| `weights/terrain/los.py` | GDAL/xarray per-observer execution and grid validation | Core; T, D; hard alignment gate and observer/input cache fingerprints. |
| `weights/terrain/runner.py` | source sampling, batching, concurrency, manifests | Core; T/I, D; sampling and partition metadata invalidate stale runs. |
| `weights/terrain/summarize.py` | immutable water denominator, raster→H3, joint/distance kernel | Core; T, D; full default, equivalent-count schema, primary math and denominator validation. |

### Viewshed package — vegetation

| File | Role; inputs → outputs / side effects | Interface, coverage, findings |
|---|---|---|
| `weights/vegetation/__init__.py` | stitched vegetation exports | Public; I; hidden API P2-6. |
| `weights/vegetation/__main__.py` | module CLI entry | Public; U. |
| `weights/vegetation/cli.py` | source support and pair-path orchestration | Public; U; legacy landcover/CHM definition; chunk reuse metadata. |
| `weights/vegetation/defaults.py` | built-in class weights, nodata, sampling defaults | Core config; U; large hidden scientific config surface. |
| `weights/vegetation/pair_chunks.py` | filter pairs, sample geometries, score rays, chunks | Core; U; explicit ray model, not radius LOS; one-ray shipped config. |
| `weights/vegetation/path_config.py` | typed path config and raster alignment | Core/public; U; area/path resolution previously error-prone. |
| `weights/vegetation/ray_sampling.py` | polygon point sampling, Bresenham rays, transmission scores | Core; U; separate science from dual-surface canopy. |
| `weights/vegetation/source_h3.py` | raster-to-source-H3 vegetation summaries | Core/public; U; source accessibility is separate from pair visibility. |
| `weights/vegetation/summarize.py` | pair/source/target summaries and final factor materialization | Core; U; combines landcover×CHM legacy factors; neutral water factor. |
| `weights/vegetation/support_rasters.py` | CHM/landcover obstruction/transmission rasters | Core; U; nodata validation useful; reuse/content fingerprint missing. |

### Shipped configuration

| File/section | Role | Findings |
|---|---|---|
| `configs/salish_sea.yaml` | sole viewshed modeling and publishing source | Fort Worden bbox; R7, 30 km, one-to-ten active-fraction observer samples, full aggregation, logistic distance kernel, conditional canopy LOS, isolated artifact root, and derived publisher factor paths. |

### Focused tests

| File | Coverage | Important omissions |
|---|---|---|
| `test_aggregation_accuracy.py` | full-versus-stride narrow-feature alias and equivalent-count schema | regional morphology convergence |
| `test_area_lookup_pairs.py` | geometry halo and role-aware same-cell truth table | regional lookup scale |
| `test_canopy_surface.py` | DTM/water/CHM semantics, nodata, and CHM/surface cache invalidation | empirical resampling sensitivity |
| `test_cleanup_safety.py` | exact-root containment, sibling/sidecar preservation | platform-specific symlink behavior |
| `test_config_contract.py` | arbitrary-CWD loads, pure read, canonical defaults, legacy aliases | migration execution |
| `test_distance_weight_values.py` | NumPy/Polars evaluator parity across models | empirical parameter selection |
| `test_geometry_normalization.py` | invalid polygon repair and safe polygonal operations | full external dataset corpus |
| `test_legacy_finalizer.py` | retired finalizer opt-in gate | historical reproduction run |
| `test_map_artifacts.py` | map aggregation, selection, classes, click-copy HTML | full render/export and raster smoothing |
| `test_notebook_reload_contract.py` | core geometry reload precedes notebook-local area reload | other interactive kernel state outside the import cell |
| `test_open_water_kernel.py` | sample numerators, horizon, land ambiguity fallback | flat-ocean GDAL error matrix |
| `test_raster_cache_fingerprints.py` | DEM, water, endpoint, and batch cache invalidation | filesystem timestamp edge cases |
| `test_service_stage_contract.py` | exact inventory, stage signature, checksummed resume | subprocess regional run |
| `test_source_sampling.py` | adaptive counts, projected/nested/component-aware design, metadata and cap | morphology sensitivity beyond fixtures |
| `test_static_pair_universe.py` | duplicate keys, exact coverage, sparse terrain zero-fill and reports | very-large lazy-plan performance |
| `test_terrain_factor_composition.py` | production in-memory/scalable dual-surface monotonic composition | regional paired run |
| `test_terrain_partition_schema.py` | empty/nonempty H3 type parity | all compact final schemas/metadata |
| `test_terrain_visibility_support.py` | observer-pixel distance, joint denominator, actual observer count, clipping | cross-backend parity |
| `test_viewability_composition.py` | model/publisher equation parity and missing-factor policy | full dynamic publish rebuild |
| `test_viewability_import_contract.py` | unambiguous dotted module import | external deprecated import callers |

### Port Angeles notebook and local review bundle

| File | Role | Duplication/status |
|---|---|---|
| `Port_Angeles_H3R8_Source_Target_Viewshed_Prototype.ipynb` | historical experiment config, source selection, dual terrain runs, maps/exports | retired 2026-07-19; production code and canonical config now own this workflow |
| `port_angeles_viewshed_review/README.md` | historical bundle instructions | retired with the notebook 2026-07-19 |
| `port_angeles_viewshed_review/REVIEW_NOTES.md` | historical scientific equations and review notes | retired with the notebook 2026-07-19 |
| `port_angeles_viewshed_review/__init__.py` | historical bundle exports | retired with the notebook 2026-07-19 |
| `runtime_support.py` | notebook config/runtime helpers | local addition; required because bundle is not standalone |
| `terrain_factors.py` | dual-surface experiment orchestration/grid audit | intentional notebook helper; core composition is now production-owned |
| `map_artifacts.py` | map/export copy | byte-equivalent to production |
| `prepare/__init__.py` | local import package | adaptation |
| `prepare/area.py` | area/source sampling copy | import adaptation only; semantic identity mechanically verified |
| `prepare/canopy_surface.py` | canopy surface copy | byte-equivalent |
| `terrain/__init__.py` | local terrain stitching | import adaptation |
| `terrain/batches.py` | batch copy | import-only drift |
| `terrain/cleanup.py` | terrain cleanup/materialization copy | import-only drift |
| `terrain/gdal.py` | GDAL/shortcut copy | import-only drift |
| `terrain/los.py` | observer LOS copy | import-only drift |
| `terrain/runner.py` | runner copy | import-only drift |
| `terrain/summarize.py` | aggregation copy | import adaptation only; semantic identity mechanically verified |

Generated `__pycache__` entries were inventoried but are not source files and should be excluded from a portable review package.

### Direct downstream viewability publisher

| File | Role | Coverage/findings |
|---|---|---|
| `publishing/prep/viewability/__init__.py` | public build exports | T; `build_artifacts` avoids the former module/function collision. |
| `area_conditions.py` | base-weighted daily area summaries | I; now inherits corrected pair weights. |
| `base.py` | authoritative lookup join, pair weight, target/source aggregation | T; corrected equation, coverage gates, and explicit p98 display semantics. |
| `build_viewability_artifacts.py` | orchestrates base/dynamic exports and manifest | T/I; versioned factor equation and non-probability normalization semantics. |
| `cli.py` | publisher command | U; config-driven. |
| `dynamic.py` | weather/daylight/lunar modifiers and source scores | U; multiplicative modifier logic, lunar intentionally omitted from one condition product. |
| `export.py` | GeoJSON/Parquet bundle exports | U; threaded writes and geometry match warnings. |
| `io.py` | schema-drift-tolerant readers and H3 harmonization | U; permissiveness can hide upstream contract drift. |
| `schemas.py` | publisher dataclass/column requirements | T/I; schema 1.3 and versioned base-pair factor contract. |

## Audit conclusion

The end-to-end code contract is now internally consistent: component-aware metric observers feed radius LOS; DEM and analytic water paths evaluate horizon/distance at sample level; target denominators are immutable and honestly named; full aggregation is the final default; bare and canopy surfaces are composed in production; centroid distance is diagnostic; canonical lookup keys govern missingness; cleanup and resume fail closed; and publisher/model equations agree.

The corrected code is ready for a new isolated Port Angeles v008 validation run. Existing regional and embedded notebook artifacts predate these contracts and must not be relabeled as current. Before a regional publication build, complete the remaining empirical work: analytic-versus-flat-DEM error characterization, canopy parameter sensitivity using defensible CHM nodata semantics, and—if any non-GDAL backend will be supported—cross-backend parity. The remaining wildcard-import and map-module findings are maintainability work, not unresolved weight math.
