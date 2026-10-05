---
hide:
  - navigation
  - toc
---

# From one sightline to regional viewing support

<div class="viewshed-examples walkthrough" markdown="1">

<p class="example-lead">Follow a real San Juan Islands example. Inspect one viewing path, expand to the water around one source area, then summarize modeled support across target cells.</p>

<p class="example-metadata">{{metadata}}</p>

These results describe **modeled physical viewing support**, not animal presence, observer effort,
or sighting probability. Everything below is already computed: no software, downloads or
interactive controls are needed.

<div class="chapter-route" aria-label="The three stages">
<span>01 &nbsp; One pair</span><span>02 &nbsp; One source’s targets</span><span>03 &nbsp; A summary per target</span>
</div>

<section class="chapter" id="one-pair" markdown="1">

## 1. One source and one target

<p class="chapter-question">What happens between these two areas?</p>

**Source area A** is a modeled land area; **target B** is a water area in the central San Juan
Islands. A source area represents several deterministic observer positions, rather than one
public lookout. Keep these same two areas in mind through all three maps.

<figure class="atlas-figure">{{figure_pair-map}}<figcaption><strong>A → B, in context.</strong> The orange line links recorded source sample P to target endpoint Q. Other dots show the committed modeled sample positions; target dots are the exported support for B. The surrounding coastline is the generalized Natural Earth geometry used in the model, not a detailed shoreline survey.</figcaption></figure>

<figure class="profile-figure">{{figure_profile}}<figcaption><strong>The same explanatory P → Q path.</strong> Gray shows ground; green shows ground plus canopy; orange shows the recorded viewing ray with its curvature assumption. The axes use metres and kilometres; vertical exaggeration is stated on the figure. The prepared model grid is {{analysis_resolution}} m, even though the profile is sampled at up to {{profile_step}} m intervals.</figcaption></figure>

The highlighted path explains **one sampled geometry**. The **A → B result** summarizes the
relevant modeled sampling population across the areas. This profile is not a complete engine
trace, and one clear or obstructed path cannot establish the whole pair result.

{{pair_comparison}}

<p class="what-to-notice" markdown="1"><strong>What to notice.</strong> {{pair_reading}}</p>

The ground-only and ground-plus-trees values above are **unweighted support indices**. The
combined score also includes the configured attenuation of individual observer-to-water
distances. The separate centroid-distance diagnostic is not multiplied into it again.

**Input limitation.** {{missing_canopy}} of mapped land pixels in the buffered input grid lack
canopy heights. The configured fallback is zero height, not measured absence of trees. This is
an input-grid fraction, not a count of affected pairs. Observer clearance is {{clearance}} m.

</section>

<section class="chapter" id="one-source" markdown="1">

## 2. One source and its surrounding targets

<p class="chapter-question">What water has modeled viewing support from this source area?</p>

Keep **source A** fixed. The maps below show **every included candidate target cell for A**,
including computed zeros. Target **B** keeps its dark outline. The extent and scale stay the
same as Chapter 1; uncolored surroundings provide context, not extra modeled coverage.

<figure class="atlas-figure">{{figure_ground}}<figcaption><strong>Ground-only support.</strong> The unweighted bare-earth support index for A’s included target cells.</figcaption></figure>

<figure class="atlas-figure">{{figure_canopy}}<figcaption><strong>Ground + trees support.</strong> The directly calculated unweighted canopy support index for the matched population. It is not the ground index multiplied by the conditional weighted vegetation factor.</figcaption></figure>

<figure class="atlas-figure">{{figure_combined}}<figcaption><strong>Combined support.</strong> Canopy and the configured distance attenuation are already included. All three maps use the same fixed linear 0–1 scale; each describes a different quantity.</figcaption></figure>

<p class="what-to-notice" markdown="1"><strong>What to notice.</strong> {{source_reading}}</p>

These are counts of **modeled target cells**, not sightings, people, or guaranteed visible
locations. Gray means a computed zero; pink hatching means unavailable evidence; an unfilled
cell is outside this source’s candidate set. Tiny positive scores retain their small values.
A neutral vegetation factor when baseline support is zero is bookkeeping, not measured clear
vegetation. Water-source vegetation is a separate not-applicable state.

</section>

<section class="chapter" id="target-summary" markdown="1">

## 3. A summary for each target cell

<p class="chapter-question">Across the included source areas, where is modeled viewing support strongest?</p>

Now expand from A to **{{land_source_count}} included land-source areas**. Each target cell shows
the **strongest source-area score among its included, valid source records**: the maximum valid
combined pair weight for that target. Land and water source roles are not pooled.

<figure class="atlas-figure">{{figure_target-summary}}<figcaption><strong>Strongest modeled support from included land-source areas.</strong> This wider view shows the full example region. Amber land areas indicate the included source population; A and B remain orientation anchors. Geographic context does not extend the modeled candidate set.</figcaption></figure>

This is an **example-derived summary**, not a new supported scientific product. It is neither
a probability, a total visibility measure, nor a fraction visible from the union of viewpoints.
The operation summarizes sources separately **for each target**; it does not average all targets
into one score for A.

### Follow target B back to its sources

{{contributors}}

{{target_reading}}

**Coverage stays attached.** {{coverage_reading}} A maximum with missing candidate records is
partial; targets with no valid contributors are unavailable, never filled with zero. Sources
outside a target’s candidate set are outside the calculation, not missing or zero-valued records.
The [downloadable summary](assets/examples/walkthrough/target-summary.json) retains candidate,
valid, missing and outside-candidate counts for each target, along with the contributing sources,
operator, role and underlying generation identity.

</section>

## Provenance and reading limits

<details markdown="1"><summary>Recorded inputs, assumptions and identities</summary>

This is a bounded real-data illustration with {{analysis_resolution}} m analysis, H3 R{{h3_resolution}}
areas and a configured {{cutoff}} km distance limit. Candidate geometry may retain centroids
beyond the attenuation cutoff; those recorded pairs are preserved. The model uses generalized
Natural Earth 1:10m land, USGS 3DEP ground elevation and ETH {{canopy_year}} canopy heights.
The {{clearance}} m observer-clearance policy and missing-height fallback influence the result.
Land samples do not establish public access. Sensitivity to sampling, resolution and clearance
has not been run; empirical field accuracy is not established.

Scientific generation: `{{generation}}`.

The [scientific bundle manifest](assets/examples/san-juan/manifest.json) retains original source
rights, method assumptions and producer identities. The [presentation manifest](assets/examples/walkthrough/manifest.json)
checks these figures separately; new presentation assets do not imply a new model run.
Island labels were checked against the [BLM regional map](https://www.blm.gov/sites/default/files/orwa-rac-sanjuan-map.pdf).

Sources: [USGS 3DEP](https://www.usgs.gov/3d-elevation-program) (public domain),
[ETH canopy, Lang, Jetz, Schindler and Wegner (2023)](https://langnico.github.io/globalcanopyheight/)
([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)),
and [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/) (public domain).
These are clipped, reprojected and modeled derivatives; raw datasets are not bundled.

</details>

[Understand sources, samples and pairs](understand/index.md) ·
[Read the product guide](products/index.md) ·
[Reproduce or validate the example](documentation-examples.md)

</div>
