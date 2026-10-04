# Artifact dictionary

A product is its data **and** validation evidence. Keep Parquet metadata, JSON sidecars and
receipts with the table. A filename alone does not establish model validity. The canonical column
lists live in `pipeline/contracts/artifacts.py`, `pairs.py` and `distance.py`; this page explains
how to use them.

## Pair grain and distance products

Within a source role, the key is `source_h3 × target_h3`. Mixed-role tables also require
`source_type`. H3 IDs are strings. One coastal H3 cell may participate as land and water; those
are different modeled roles. Candidate coverage is recorded rather than inferred from absent rows.

| Product / column | Unit and meaning | Validation |
| --- | --- | --- |
| `pair_distances.parquet` | Reusable candidate universe, schema `distance_products_v1` | `validate_distance_product(path)` |
| `source_h3`, `target_h3`, `source_type` | Unique non-null pair and explicit role | H3 resolution, key and role checks |
| `distance_m`, `distance_km` | Great-circle H3-centroid separation; metres and kilometres | Finite nonnegative values, method and coverage contract |
| `profiles/<id>-<identity>.parquet` | Same pairs and distances plus one configured attenuation profile | Product checksum, contract, pair coverage and curve validation |
| `weight_distance` | Dimensionless assumed attenuation in [0,1] | Not an animal detection probability |

The [offline quick start](../getting-started/quick-start.md) produces real files with synthetic
geometry and checks the exponential formula. Profile identity includes resolved parameters and
upstream pair identity. A new profile does not change integrated terrain calculations.

## Compact static products

The paired workflow publishes land/water tables with schema `viewshed_static_pair_v2`. Their
source role is encoded by the product path and metadata. The component workflow has its own
namespace and per-role publication contract; compare [pipelines](../pipelines.md).

| Column | Meaning |
| --- | --- |
| `source_h3`, `target_h3` | Pair identity within the product's source role |
| `weight_terrain` | Distance-integrated bare-earth kernel for land; opaque-land kernel for water |
| `weight_vegetation` | Conditional canopy factor for land; neutral multiplicative value for water, whose canopy is not applicable |
| `weight_distance` | Centroid attenuation diagnostic; do not multiply it into the final result again |
| `weight_static_viewability` | `weight_terrain × weight_vegetation`, dimensionless [0,1] |

For an **illustrative**, non-geographic row with terrain 0.50, conditional canopy 0.90 and centroid
diagnostic 0.80, static support is **0.45**. It is not 0.36 and not a 45% chance of seeing an animal.

Validate retained paired products through the generation-aware workflow. The advanced read-only
`pipeline.finalize.final_artifacts.validate_static_viewability_outputs(config)` checks the paired
set. `validate(artifacts)` at the package root checks path existence only.

## Observation geometry

Schema `4.0.0-research` retains source role, diagnostic values, matching state fields and lineage.
See the [scientific contracts](../scientific-methodology.md) for exact definitions.

| Field family | Interpretation |
| --- | --- |
| `line_of_sight_support` / state | Direct unweighted bare-earth LOS support for land; water LOS for water |
| `distance_weighted_los_support` / state | Integrated terrain support |
| `vegetation_attenuation` / `vegetation_state` | Conditional canopy attenuation and whether the quantity applies |
| `physical_viewability` / state | Direct unweighted canopy joint LOS for land; water LOS for water |
| `distance_adjusted_viewability` / state | Final integrated physical support |
| `DATA_COVERAGE_STATE`, `SOURCE_COVERAGE_STATE` | Coverage evidence; unknown/partial must remain visible |
| `GENERATION_ID`, `CONFIG_HASH`, `SOURCE_HASHES_JSON` | Product, scientific configuration and input identity |
| `COMPONENT_PROVENANCE_JSON`, `SOURCE_VINTAGES_JSON` | Producer evidence and source vintage |

Read values together with their states. Null/unavailable is not observed zero; not-applicable
canopy is not evidence that a water cell was measured to contain no trees. A missing candidate
pair is outside the evaluated universe, not a zero-valued observation.

## Evidence that travels with an artifact

- Paired outputs: both static tables, both observation-geometry tables, supported sidecars and
  `viewshed-generation.json` (`viewshed_output_set_v3`). Retained producer evidence supports
  documented pruning; present changed inputs invalidate reuse.
- Components: embedded contract and checksum/provenance sidecar, with per-role manifests. There is
  no combined atomic land/water generation pointer.
- Distance products: `.parquet.json` sidecar with schema, algorithm, method, coverage, effective
  profile parameters where applicable, output checksum and fingerprint.
- Acquisition: version 2 download manifests distinguish raw-byte `observed_sha256` from
  filename-inclusive artifact checksums. See [compatibility](../data-acquisition.md#source-checksum-compatibility).

Do not rename or edit artifacts and then update hashes to manufacture acceptance. Rebuild through
the producing API when scientific inputs or schema meaning change.
