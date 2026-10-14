# Retained regional weight diagnostics

`scripts/regional_weight_diagnostics.py CONFIG.json` adds separate inspectable factors to a
completed `bounded_regional_classified_viewability_pilot_v1` run. It reuses the exact saved
observers, prepared arrays, water population and accepted stride-2 target design. It does not
change the parent scores, acquire inputs, invoke the canonical workflow, or promote a release.

The JSON configuration requires `pilot`, `output_dir`, `distance_config`, `workers` and
`memory_budget_gib`. Paths are explicit caller-owned paths. The output directory must be new.
The configured worker and memory limits are enforced by the retained pilot's process supervisor.
An interrupted run leaves diagnostic partitions and logs but no completed manifest. No cleanup
or overwrite is performed. Only a complete, exact pair-universe match is materialized as
`weights.parquet` with `manifest.json`; generated artifacts remain untracked.

The grain is one `source_type × source_h3 × target_h3` row, matching the parent exactly. The
three main fields are:

- `weight_terrain`: mean of bare-earth clear LOS multiplied by the developed distance curve at
  each observer–water sample, over the parent pilot's complete observer/sample denominator.
- `weight_above_ground`: conditional roof and WorldCover/CHM transmission, weighted by the same
  observer–sample distance curve, over terrain-clear weighted support only. It is null where no
  terrain-clear support exists (`not_applicable`), or where relevant inputs remain unresolved
  (`unknown`). Known roof/canopy blockage is a resolved zero. `above_ground_lower` and
  `above_ground_upper` retain uncertainty bounds.
- `weight_distance`: the configured toolkit curve evaluated at the canonical spherical
  H3-centroid distance; `distance_km` is also retained. This is a diagnostic, not another multiplier
  for terrain. The Salish Sea profile is normalized logistic decay, d50 9 km, slope 2.7 km, cutoff
  30 km. The actual observer rays retain the pilot's stricter 15-mile reach.

For terrain-clear indicator L, transmission T, distance decay D, observer/sample weights w:
`terrain = sum(w L D) / sum(w)` and `above_ground = sum(w L T D) / sum(w L D)`.
Their per-pair product reconstructs the distance-weighted transmission bound; the original pilot
score remains the unweighted transmission mean. Component target means cannot generally be
multiplied to reconstruct mean viewability. The canonical GDAL land kernel uses different
surfaces and full-cell water support; these direct-ray research diagnostics are not a canonical
production rebuild or the canonical vegetation factor.

The independent bare-earth pass prevents missing roofs/canopy from being interpreted as missing
terrain. Joint rays for target cells with an original zero upper bound may be skipped: that bound
proves all the same sampled comparisons have zero transmission. All terrain rays are evaluated.
Accumulations use float64 consistently even when distance evaluations return float32.

The manifest pins the original pair checksum, retained band/metadata checksums, implementation
checksums, distance configuration, process measurements and field definitions. Inputs and code
are checked again before completion. The parent pilot's sampling checks do not establish accuracy
of the new conditional ratio, which can be sensitive to very little terrain-clear support.
These are uncalibrated research scenario factors, not empirical detection probabilities.

The Data Explorer accepts the completed folder through its consumer-owned `--components`
argument. It validates checksums, exact pair identity, bounds, statuses and lineage, then builds
a new immutable browser snapshot. Terrain/distance target maps retain the all-source denominator
with absent out-of-reach pairs contributing zero. Above-ground target maps use the mean over
terrain-clear source pairs, keep any unresolved contributor as a null complete mean, and expose
applicable, unknown and not-applicable source counts. The map preserves the original pilot layers.

## V2 supported successor

New supported generations use [direct transmission](direct-transmission.md), which produces
arithmetic transmission and matched distance-integrated terrain/conditional above-ground factors
in one package-owned workflow. The retained diagnostic adapter snapshots its distance YAML before
worker dispatch and rejects an edited configuration before completion. Existing v1 browser
artifacts remain unchanged; new source-ground/ray identities require fresh computation, not
receipt backfilling.
