# Distance products

Distance is available as two durable, independently executable products:

1. **Pair distances** record geometry once for a bounded source–target pair universe.
2. **Distance profiles** apply a named logistic, exponential, or piecewise attenuation curve to
   those persisted distances.

Neither product opens, downloads, or prepares DEM/CHM rasters, runs line of sight, composes static
weights, or exports maps. The canonical pair lookup and its land/water geometry lineage are still
required when first building pair distances. After that artifact exists, profiles require only the
pair Parquet file, its sidecar, and profile parameters.

These H3-centroid products are diagnostics and reusable geometric covariates. They are not
calibrated animal-detection probabilities. The current viewshed kernel separately evaluates the
same configured curve at every observer/target sample distance. A centroid profile must never be
multiplied into the static result again.

## Pair universe and metric

`build_pair_distances` promotes distances from the validated source-target lookup; it does not
recalculate them with another metric. Each value is the Haversine great-circle distance between
H3 cell centres in EPSG:4326 using mean Earth radius 6,371.0088 km. The product records source and
target H3 resolutions, source role, method, units, schema/algorithm versions, input lookup
checksum, geometry fingerprints, candidate policy, and output checksum.

Candidate coverage and attenuation are separate contracts. Candidate pairs are enumerated by the
existing H3 grid-disk and geometry-aware projected-polygon cutoff policy. Consequently, a retained
pair's **centroid** distance can be slightly greater than the configured pair-generation limit.
The product is not an unrestricted Cartesian product. A profile cutoff sets weights to zero while
preserving every stored pair; it does not erase pairs or prove anything about pairs outside the
universe. A requested cutoff wider than the pair-generation coverage fails and requires rebuilding
a wider lookup/pair product.

Products are separated by source role. Combine land and water tables only with
`source_type × source_h3 × target_h3` as the key because the same H3 pair may participate in both
roles.

## Outputs and schemas

Products live outside the legacy cleanup tree under:

```text
<paths.final_output_dir>/components/distance/<source_type>/
  pair_distances.parquet
  pair_distances.parquet.json
  profiles/<profile_id>-<scientific-id-prefix>.parquet
  profiles/<profile_id>-<scientific-id-prefix>.parquet.json
```

`paths.final_output_dir` must be outside `paths.output_dir`. The builder rejects an overlapping
layout because the established complete workflow cleans the working output tree after success.

Pair-distance columns, in deterministic key order:

| Column | Type/meaning |
|---|---|
| `source_h3` | Non-null H3 source cell at the declared resolution |
| `target_h3` | Non-null H3 target cell at the declared resolution |
| `source_type` | `land` or `water`; part of the combined key |
| `distance_m` | Finite, nonnegative observed centroid distance in metres |
| `distance_km` | The same distance in kilometres, retained for compatibility |

A profile has the same columns plus finite, bounded `weight_distance` in `[0, 1]`. Empty valid
roles produce an empty Parquet file with the complete schema. Missing, null, invalid, negative, or
nonfinite distance is an error; it is never replaced with zero. Duplicate role/pair keys are an
error and are never silently deduplicated.

Every Parquet file has a checksum-backed JSON sidecar. File existence alone is not a cache hit.
Validation checks containment, metadata and schema versions, contract fingerprint, checksum, H3
resolution, uniqueness, ordering, metre/kilometre consistency, bounds, source lineage, exact pair
preservation, and reproducibility of profile values.

## CLI

Build land pair distances and the existing default compatibility weights through the component
dependency graph:

```bash
viewshed-toolkit build distance \
  --config configs/salish_sea.yaml \
  --source-type land \
  --run-id land-distance
```

Or materialize only the raw product from an already validated lookup:

```bash
viewshed-toolkit build-pair-distances \
  --config configs/salish_sea.yaml \
  --source-type land
```

Generate two coexisting profiles without reopening geometry or rebuilding distances:

```bash
PAIR=data/processed/domain/human/viewshed/RES7/components/distance/land/pair_distances.parquet

viewshed-toolkit build-distance-profile \
  --pair-distances "$PAIR" \
  --profile-id near \
  --model exponential \
  --exponential-lambda-km 5 \
  --hard-cutoff-km 20

viewshed-toolkit build-distance-profile \
  --pair-distances "$PAIR" \
  --profile-id broad \
  --model exponential \
  --exponential-lambda-km 15 \
  --hard-cutoff-km 30

viewshed-toolkit validate-distance-product "$PAIR"
viewshed-toolkit validate-distance-product \
  outputs/final/components/distance/land/profiles/near-<scientific-id-prefix>.parquet
```

Use the path printed by each build command instead of guessing its identity suffix. `--overwrite`
is explicit. Profiles with different effective parameters coexist; equivalent effective profiles
reuse the same scientific identity even when irrelevant parameters differ.

## Python API

```python
from viewshed_toolkit import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    validate_distance_product,
)

pair_path = build_pair_distances(
    "configs/salish_sea.yaml",
    source_type="land",
)

near_path = build_distance_profile(
    pair_path,
    DistanceProfile(
        "near",
        selected_model="logistic",
        logistic_d50_km=7.0,
        logistic_slope_km=2.0,
        hard_cutoff_km=20.0,
    ),
)
broad_path = build_distance_profile(
    pair_path,
    DistanceProfile(
        "broad",
        selected_model="logistic",
        logistic_d50_km=14.0,
        logistic_slope_km=3.5,
        hard_cutoff_km=30.0,
    ),
)

validate_distance_product(pair_path)
validate_distance_product(near_path)
validate_distance_product(broad_path)
```

## Identity, invalidation, and compatibility

Pair scientific identity includes the role, H3 resolutions, distance method, candidate policy,
geometry/lookup lineage, schema, and algorithm version. DEM/CHM paths, settings, or fingerprints
are excluded. Profile scientific identity includes the pair scientific identity, model, normalized
effective parameters, units, cutoff behavior, schema, and algorithm version. Raw YAML fields that
do not affect the selected curve are excluded; inherited defaults are resolved before hashing.

Changing a standalone profile creates or reuses only that profile. It does not rewrite raw
distances, change the integrated LOS configuration, or invalidate terrain/canopy products.
Changing geometry, candidate coverage, H3 resolution, metric/algorithm version, or lookup content
invalidates the pair product and its profiles. Missing/corrupt sidecars or checksums prevent reuse.

The established `distance_weights.parquet` component remains as a compatibility adapter generated
from the raw product plus the integrated model's existing default profile. Existing terrain,
canopy, static formulas, cleanup, and paired promotion are unchanged.

## Interpretation and future work

An H3-centroid curve is not generally interchangeable with a joint observer/target-sample mean:
`D(mean distance)` and `mean(D(sample distance))` need not agree, and neither may replace
`mean(LOS × D(sample distance))`. Distance-free LOS plus late sample-level composition would
require a new scientific model and durable sample-level LOS contract; that is future work.
