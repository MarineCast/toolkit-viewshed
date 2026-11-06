---
hide:
  - navigation
  - toc
---

{{example_tabs}}

<div class="viewshed-examples walkthrough" markdown="1">

# Putting it all together

<p class="example-lead">Compare how terrain, vegetation and distance shape viewing support across the full region.</p>

This map covers **all {{region_targets}} delivered water target cells** in the Vancouver Island–Columbia study area. It aggregates **{{region_pairs}} candidate pairs from {{region_sources}} land-source cells**, including the three examples in the other tabs.

<figure class="atlas-figure regional-html-map">
<iframe src="../assets/examples/region-overview/map.html" title="Interactive regional viewshed map" loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe>
<figcaption><strong>Highest available weight per water target.</strong> Choose a weight layer and color palette in the legend. Pan, zoom or use “Explore” to visit a case-study area. Select a cell to see its weight and source coverage. All palettes use the same linear 0–1 scale. The grid shows native H3 R7 cells and evaluates the water within each cell. Street map: © OpenStreetMap contributors. Local coastline: Natural Earth, public domain.</figcaption>
</figure>

[Open the interactive map full-page](assets/examples/region-overview/map.html){ .md-button .md-button--primary target="_blank" rel="noopener" }

<details markdown="1">
<summary>Static map and offline fallback</summary>

<figure class="atlas-figure">{{region_map}}<figcaption>The default combined weights in a static map, available without JavaScript or online street tiles.</figcaption></figure>

</details>

[Open the full-size map](assets/examples/region-overview/combined.svg){ .md-button }
[Download combined weights](assets/examples/region-overview/targets.json){ .md-button }
[Download component weights](assets/examples/region-overview/map-components.json){ .md-button }

## Reading the four layers

- **Combined** shows the maximum physical viewing weight after terrain, distance and canopy. **{{region_positive}} targets** have positive combined support.
- **Terrain** shows the maximum bare-earth viewing weight with distance attenuation already included.
- **Vegetation** shows the largest fraction of terrain support retained after canopy, among candidates with positive terrain support. Cells where no candidate has positive terrain have their own legend swatch.
- **Distance** shows the maximum distance weight between source and target cell centers. It isolates the effect of distance from obstructions.

Different sources can supply the highest weight in different cells or layers. Read each layer on its own and use the Combined layer for modeled viewing support. A high combined value means at least one land source has strong modeled support. The [regional explorer](regional-viewshed-example.md) compares land and water observer roles.

The legend distinguishes computed zero, unavailable weight and areas outside the target set. Each maximum uses the available values, and the popup reports how many sources contributed valid values. The combined layer has **{{region_missing}} missing candidate values**.

[Regional source credits and qualifications](regional-viewshed-example.md#inputs-and-interpretation) · [Aggregation source receipt](assets/examples/region-overview/source.json) · [Reproduce the walkthroughs](documentation-examples.md#multiple-locations-and-the-regional-overview)

</div>
