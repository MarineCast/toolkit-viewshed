# Read a result

The [Lime Kiln walkthrough](../examples.md#follow-one-result-back-to-its-components)
follows land source `{{lime_source}}` to water target `{{lime_target}}`, with
**{{lime_km}} km** between their cell centers. The compact weights for that pair are below.

## Read one result

| Field | Recorded value | What it measures |
| --- | ---: | --- |
| `weight_terrain` | {{example_terrain}} | Bare-earth viewing support with distance attenuation included |
| `weight_vegetation` | {{example_vegetation}} | Fraction of that terrain support retained after canopy |
| `weight_distance` | {{example_distance}} | Distance diagnostic based on the two cell centers |
| `weight_static_viewability` | **{{example_combined}}** | Combined physical viewing support |

## Understand the multiplication

```text
terrain support × vegetation factor ≈ combined support
{{example_terrain}}        × {{example_vegetation}}          ≈ {{example_combined}}
```

The displayed inputs are rounded. Use the terrain and vegetation fields for this product.
The distance diagnostic helps explain separation, while terrain already includes attenuation
along the sampled sightlines. The combined score is a dimensionless model index.
Estimating detection probability requires observation data and a separate model.

The observation-geometry records call this same final weight `distance_adjusted_viewability`.
The [Lime Kiln code example](../examples.md#use-the-result) reads that field for this pair.

## Keep the pair and its evidence together

| State | How to read it |
| --- | --- |
| Computed zero | The model evaluated the candidate and returned zero support under its settings. |
| Missing or unavailable | The pair is in the candidate set and lacks a valid result. Keep the value missing. |
| Outside the candidate set | The candidate lookup did not select this pair for evaluation. |

Within a source role, the key is `source_h3 × target_h3`. A table combining roles also needs
`source_type`. Keep units, spatial support, source coverage and provenance alongside the values.

## Choose a product

| You need to… | Product or diagnostic | Read next |
| --- | --- | --- |
| Represent physical viewing support from a source to water | Compact static pair weights; distance is already integrated | [Scientific contracts](../scientific-methodology.md), [pipelines](../pipelines.md) |
| Separate unweighted LOS from distance attenuation | Observation geometry with directly calculated unweighted diagnostics and recorded states | [Direct unweighted diagnostics](../scientific-methodology.md#direct-unweighted-canopy-diagnostics-and-migration) |
| Inspect ground and canopy contributions | Matched terrain/canopy kernels and conditional canopy factors | [Land-source kernel](../scientific-methodology.md#land-source-raster-kernel), [canopy](../scientific-methodology.md#canopy-surface-and-observer-clearance) |
| Reuse geometry or explore an attenuation curve | Standalone pair distances and named distance profiles | [Distance products](../distance-products.md) |
| Explore results spatially | Static maps or the committed visual walkthrough | [Pipeline map export](../pipelines.md), [guided example](../examples.md) |

The [regional map](../examples-region.md) displays the highest available land-source weight
for each water target. The source supplying that maximum can vary across targets and layers.
The [completed regional example](../regional-viewshed-example.md) compares the two source roles.

Artifact checks have different scopes. `validate(artifacts)` checks that referenced paths
exist. Workflow publication checks and `validate_distance_product` apply their own contracts.

## Choose the producing workflow

The established paired workflow retains compact outputs after successful cleanup. The explicit
component workflow retains a separate component namespace and has no implicit cleanup. Its
per-role outputs do not promise a combined atomic generation pointer.

[Compare workflow contracts](../pipelines.md#workflow-contracts) before choosing paths or retention
rules. For a concrete result you can inspect now, open the [San Juan example](../examples.md).

Use the [artifact dictionary](artifacts.md) for columns, units, state meanings and validation evidence.
