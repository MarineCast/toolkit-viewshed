---
hide:
  - navigation
  - toc
---

{{example_tabs}}

<div class="viewshed-examples walkthrough" markdown="1">

# Putting it all together

<p class="example-lead">From one observer area to the whole region: where does at least one modeled land source retain physical viewing support?</p>

This map covers **all {{region_targets}} delivered water target cells** in the Vancouver Island–Columbia study area. It aggregates **{{region_pairs}} candidate pairs from {{region_sources}} land-source cells**, including the three examples in the other tabs.

<figure class="atlas-figure regional-html-map">
<iframe src="../assets/examples/region-overview/map.html" title="Interactive regional viewshed map" loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe>
<figcaption><strong>Strongest valid combined weight per water target.</strong> Pan, zoom or use “Explore” to visit a case-study area. Select a cell to inspect its recorded weight and coverage. The native H3 R7 grid uses a fixed linear 0–1 scale; gray is a computed zero. Unfilled surroundings are outside the delivered target set. Mixed coastal hexagons include land; weights evaluate their water support. Street map: © OpenStreetMap contributors. Local coastline: Natural Earth, public domain.</figcaption>
</figure>

[Open the interactive map full-page](assets/examples/region-overview/map.html){ .md-button .md-button--primary target="_blank" rel="noopener" }

<details markdown="1">
<summary>Static map and offline fallback</summary>

<figure class="atlas-figure">{{region_map}}<figcaption>The same regional weights in a static map. This remains available without JavaScript or online street tiles.</figcaption></figure>

</details>

[Open the full-size map](assets/examples/region-overview/combined.svg){ .md-button }
[Download the target summary](assets/examples/region-overview/targets.json){ .md-button }

**{{region_positive}} targets have positive maximum support.** Each target takes the largest recorded `weight_static_viewability` among its candidate land sources. The winning source can differ between neighboring targets. This is a presentation summary of the delivered results, not a new viewshed calculation.

A high value means **at least one modeled source area** has strong physical support. It does not measure typical visibility, observer numbers, public access or the probability of seeing an animal. A maximum cannot describe what one person can see from a single location across the entire region.

The aggregation uses combined weights directly. Distance is already included in terrain; vegetation has already been applied. It does not multiply independently aggregated components. Land and water observer roles remain separate; this overview continues the land-source examples. The [regional explorer](regional-viewshed-example.md) compares the two roles.

All candidate zeros are retained. Missing candidates would be counted separately and excluded from the maximum; targets with no valid values would stay unavailable. This delivery has **{{region_missing}} missing candidate values** in this summary. Blank space is not converted to zero.

[Regional source credits and qualifications](regional-viewshed-example.md#inputs-and-interpretation) · [Aggregation source receipt](assets/examples/region-overview/source.json) · [Reproduce the walkthroughs](documentation-examples.md#multiple-locations-and-the-regional-overview)

</div>
