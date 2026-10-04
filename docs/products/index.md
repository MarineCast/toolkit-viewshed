# Product guide

Choose the quantity that answers your question before selecting a file or an API. All products
describe static modeled geometry or an assumed distance rule, rather than animal occurrence or
observer activity.

| You need to… | Product or diagnostic | Read next |
| --- | --- | --- |
| Represent physical viewing support from a source to water | Compact static pair weights; distance is already integrated | [Scientific contracts](../scientific-methodology.md), [pipelines](../pipelines.md) |
| Separate unweighted LOS from distance attenuation | Observation geometry with directly calculated unweighted diagnostics and recorded states | [Direct unweighted diagnostics](../scientific-methodology.md#direct-unweighted-canopy-diagnostics-and-migration) |
| Inspect ground and canopy contributions | Matched terrain/canopy kernels and conditional canopy factors | [Land-source kernel](../scientific-methodology.md#land-source-raster-kernel), [canopy](../scientific-methodology.md#canopy-surface-and-observer-clearance) |
| Reuse geometry or explore an attenuation curve | Standalone pair distances and named distance profiles | [Distance products](../distance-products.md) |
| Explore results spatially | Static maps or the committed instructional explorer | [Pipeline map export](../pipelines.md), [guided example](../examples.md) |

## Keep the pair and its evidence together

Within a source role, the key is `source_h3 × target_h3`. A table combining roles must retain
`source_type`. The candidate lookup defines the modeled universe; a missing candidate is not
an observed zero. Preserve units, support, source coverage and metadata alongside numeric values.

Artifact validation has different scopes. `validate(artifacts)` checks referenced paths exist;
it is not a schema or scientific validation. The workflow's publication checks and
`validate_distance_product` have their own contracts, described in the [API](../api.md) and
[distance guide](../distance-products.md).

## Choose the producing workflow

The established paired workflow retains compact outputs after successful cleanup. The explicit
component workflow retains a separate component namespace and has no implicit cleanup. Its
per-role outputs do not promise a combined atomic generation pointer.

[Compare workflow contracts](../pipelines.md#workflow-contracts) before choosing paths or retention
rules. For a concrete result you can inspect now, open the [San Juan example](../examples.md).
