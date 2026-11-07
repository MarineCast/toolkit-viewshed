{{example_tabs}}

<div class="viewshed-examples walkthrough place-walkthrough" markdown="1">
<div class="example-hero" markdown="1">
<div class="hero-copy" markdown="1">

# {{place_title}}

<p class="example-lead">{{place_lead}}</p>

<p class="example-metadata">November 2026 regional results · land sources · H3 R7 · 30 m analysis · mixed canopy vintages · 30 km distance limit</p>

{{place_text}}

{{place_lesson}}

</div>
{{hero_photo}}
</div>

<nav class="chapter-route" aria-label="Location walkthrough">
<a href="#study-area">01 Study area</a><a href="#distance">02 Distance</a><a href="#terrain">03 Terrain</a><a href="#vegetation">04 Vegetation</a><a href="#combined">05 Combined</a><a href="#shoreline">06 Shoreline views</a><a href="#use-the-result">07 Use the result</a>
</nav>

<section class="chapter" id="study-area" markdown="1">

## 1. The study area

<p class="chapter-question">Where is the observer, and what water is being evaluated?</p>

{{place_intro}}

<figure class="atlas-figure">{{lime_study}}<figcaption><strong>One fixed source, {{lime_count}} candidate water targets.</strong> The same extent, source, observer dots and target population remain on all four weight maps. H3 outlines identify areas; mixed coastal target cells contain land as well as modeled water. Only their water support is evaluated.</figcaption></figure>

The regional model uses 30 m ground elevation, qualified canopy heights and reconciled land/water
support. The maps show the **full recorded candidate set around this source**. Natural Earth supplies the generalized background coast; it is context,
not the more detailed water mask used for LOS. The 30 m analysis still cannot resolve individual
viewing platforms or guarantee access. [Regional inputs and interpretation](regional-viewshed-example.md#inputs-and-interpretation)
record the source qualifications. Map labels do not change the modeled observer coordinates.

</section>

<section class="chapter" id="distance" markdown="1">

## 2. Distance: how far is the target?

<p class="chapter-question">What would the distance rule contribute before considering obstructions?</p>

<figure class="atlas-figure">{{lime_distance}}<figcaption><strong><code>weight_distance</code>: the centroid-distance diagnostic.</strong> Darker water cells have a larger assumed distance weight. All maps use a fixed linear 0–1 scale, with gray for a computed zero; the quantities differ between panels.</figcaption></figure>

The diagnostic compares cell centers. It declines with distance and reaches zero beyond 30 km.

{{distance_detail_start}}

This example uses a normalized logistic curve: midpoint parameter **9 km**, slope **2.7 km**,
and a **30 km** hard cutoff. The curve gives progressively less support to distant water. It is a
configured assumption, not a fitted probability that a person will detect a whale. At 5 km the regional curve retains about **0.84**, at 10 km **0.42**, and at 20 km **0.017**.
The 30 km limit defines the evaluation/attenuation range; it is **not a 30 km geometric horizon**.
Curvature, observer elevation, target height and obstruction still constrain actual sightlines.

The map evaluates the curve between **H3 centroids**. {{center_note}} Dashed 10, 20 and 30 km rings give approximate
projected distance from that center; the weights use recorded great-circle centroid distances.
The orange outline leaves the source cell's distance color visible. A mixed source/target self-pair has centroid
distance zero even though the modeled person and water pixels are apart. Candidate selection can
also retain cells whose centroids exceed 30 km; their diagnostic is zero. The physical calculation
uses each observer-to-water distance instead. [See the illustrated decay curve](understand/index.md#distance).

{{secondary_detail_end}}

</section>

<section class="chapter" id="terrain" markdown="1">

## 3. Terrain: is there a path to the water?

<p class="chapter-question">Which directions retain support when the ground blocks sightlines?</p>

<figure class="atlas-figure">{{lime_terrain}}<figcaption><strong><code>weight_terrain</code>: bare-earth support with distance already integrated.</strong> This is the matched ground-only calculation, before the canopy factor. It is not a distance-free percentage of visible water.</figcaption></figure>

A nearby target can receive little support when the ground blocks the sampled sightlines.

{{terrain_detail_start}}

For each modeled observer, the toolkit evaluates water pixels on the ground surface,
weights supported paths by distance, and aggregates over the recorded source–target population.
Blocked and out-of-range contributions remain zero in that population.

Read this beside the distance map: high distance weight alone cannot make an obstructed path
visible. **Do not multiply these two maps together.** Distance has already entered the terrain
weight at the observer-to-water level. The [detailed sightline example](sightline-details.md#one-pair)
shows a recorded ground/canopy profile from the separate, older 100 m San Juan demonstration.
That profile is conceptual supporting material, not a trace of these regional results.

{{secondary_detail_end}}

</section>

<section class="chapter" id="vegetation" markdown="1">

## 4. Vegetation: how much support survives the canopy?

<p class="chapter-question">What changes when tree height is added above the ground?</p>

<figure class="atlas-figure">{{lime_vegetation}}<figcaption><strong><code>weight_vegetation</code>: conditional canopy factor.</strong> Among pairs with positive baseline terrain support, 1 retains that support and 0 removes it. Beige hatching identifies a neutral factor of 1 where baseline support is already zero; it does not mean a clear view.</figcaption></figure>

The model repeats the calculation on a matched ground-plus-canopy surface. The vegetation factor
is the capped ratio of its distance-integrated support to the ground-only support. It describes
what remains after canopy is considered—not the fraction of the target covered in trees.

For this source, **{{lime_neutral}} targets have no baseline support** and use the neutral
bookkeeping factor. Their combined weight stays zero. The configured **30 m observer clearance**
removes local canopy around each sample before evaluation; that is a model assumption, not evidence
of a clearing. Regional input qualification and these rows report complete source/data coverage
under the recorded acceptance policy; that does not establish field accuracy or uniform canopy
vintage. The configured zero-height fallback remains an assumption, not proof of no trees.

<details markdown="1">
<summary>How neutral vegetation is recorded</summary>

The regional output records `vegetation_state=positive` for neutral values of 1 as well. Hatching
is therefore derived from **terrain = 0 and vegetation = 1**, with the original producer state
preserved in the downloadable records. It prevents neutral bookkeeping from appearing as strong
viewability.

</details>

</section>

<section class="chapter" id="combined" markdown="1">

## 5. Combined: the physical viewing support

<p class="chapter-question">What remains after terrain, canopy and distance are considered together?</p>

<figure class="atlas-figure">{{lime_combined}}<figcaption><strong><code>weight_static_viewability = weight_terrain × weight_vegetation</code>.</strong> All {{lime_count}} candidate targets remain present, including computed zeros. Unfilled surroundings are outside this source's candidate set.</figcaption></figure>

**{{lime_positive}} of these {{lime_count}} targets have positive combined support.** Positive values indicate
some modeled support remains under these inputs and settings. It does not count sightings,
people or guaranteed visible locations. A higher score describes stronger physical support for
this modeled source–target relationship; it is not the probability of seeing an animal.

### Follow one result back to its components

{{selection_note}}
`{{lime_target}}`, with **{{lime_km}} km centroid separation**. This declared selection makes the
example easy to trace; it is not a recommended wildlife-watching destination.

{{lime_result}}

Multiply the terrain and vegetation values to recover the combined score (allowing for displayed
rounding). The distance diagnostic is shown for interpretation and is **not multiplied again**.

</section>

{{shoreline_content}}

<section class="chapter" id="use-the-result" markdown="1">

## 7. Take the result into your work

The observation-geometry records call the final integrated weight
`distance_adjusted_viewability`. It corresponds to `weight_static_viewability` in the compact
weights. From the repository root, use Python's standard library to read the pair shown above.

```python
import json
from pathlib import Path

example = json.loads(
    Path("docs/assets/examples/{{place_key}}/source-pairs.json").read_text()
)
row = next(row for row in example["rows"] if row["target_h3"] == "{{lime_target}}")
print(row["source_h3"], row["target_h3"], f"{row['distance_adjusted_viewability']:.6f}")
```

Expected output is the source ID, target ID and combined weight.

```text
{{lime_source}} {{lime_target}} {{example_combined}}
```

The [downloadable records](assets/examples/{{place_key}}/source-pairs.json) contain the saved
weights and states for all candidate pairs. The [artifact dictionary](products/artifacts.md)
maps observation-geometry names to compact weights.

<details markdown="1">
<summary>Where to find the metadata</summary>

The excerpt retains the land source role, pair IDs and original generation identity.
Shared coverage, lineage and vintage fields appear once in `common_metadata`.

</details>

For **sighting-bias research**, join the physical layer to independently documented observation
locations and effort. For **land-based lookout selection**, compare publicly accessible source
locations against the same target-water area and field-check the view. Both applications need
more than a high map value. [So what can this help us do?](understand/index.md#so-what)

[Reproduce this regional excerpt](documentation-examples.md#lime-kiln-presentation){ .md-button }
[Explore the completed regional delivery](regional-viewshed-example.md){ .md-button }

</section>

## Evidence and limits

These are **November 5, 2026 regional delivery** results at 30 m / native H3 R7. All five maps,
the component table and the explanation use the same land-source population and generation.
Rendering this walkthrough does not rerun the model or alter delivered weights.
Scientific generation: `{{lime_generation}}`.

Ground: USGS 3DEP and NRCan MRDEM; canopy: ETH 2020 with WRI/Meta v6 supplementation and
mixed imagery dates. The exported `canopy_product_year=2020` field is retained for traceability,
but does not describe the whole mixed-source regional canopy. The
[regional source credits and licenses](regional-viewshed-example.md#inputs-and-interpretation)
identify terrain, canopy and nominal water sources. Background coast: [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/), public domain.
See the [regional excerpt evidence](assets/examples/{{place_key}}/regional-source.json) and
[Presentation and photograph receipt](assets/examples/{{place_key}}/manifest.json).

Sampling, resolution and clearance sensitivity have not been run for this example; field accuracy
is not established. Weather, glare, sea state, visual acuity, animal behavior and actual observer
presence are outside this static calculation. [Rebuild and validate the presentation](documentation-examples.md#lime-kiln-presentation).

For earlier walkthrough links: [one pair](sightline-details.md#one-pair),
[one source's targets](sightline-details.md#one-source), and
[a summary per target](sightline-details.md#target-summary).
<span id="one-pair"></span><span id="one-source"></span><span id="target-summary"></span>

</div>
