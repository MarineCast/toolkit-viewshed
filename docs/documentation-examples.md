# Reproducing the instructional bundle

The new bundle in `assets/examples/san-juan/` contains validated real San Juan derivatives, all
5,424 modeled candidate pairs, actual deterministic observer samples, active source geometry,
curated target support, prepared-ground/canopy display grids, coverage and explanatory profiles.
The bundle uses columnar JSON to avoid repeating field names; its `columns` map each row to the
schema. `row_trace` identifies authoritative artifacts and role/source/target keys. Forward and
inverse indexes refer to the same pair IDs. Target support is a clearly declared curated subset;
land pixel centers and water-kernel samples are separate populations. Display grids use every
fourth 100 m pixel and retain null missing values; this does not alter modeled weights.

Acquisition/model execution, scientific export and presentation are separate operations:

```bash
# Explicit bounded real rebuild. Requires compatible GDAL/Rasterio and source availability.
PYTHONPATH=src python scripts/run_san_juan_demo.py \
  --config configs/san_juan_demo.yaml --rebuild --model-only

# Export existing validated outputs. Never downloads missing inputs.
PYTHONPATH=src python scripts/build_documentation_examples.py \
  --config configs/san_juan_demo.yaml --output docs/assets/examples/san-juan

# Read-only, offline check. No source data, GDAL or site packages are needed.
python -S scripts/build_documentation_examples.py \
  --check --output docs/assets/examples/san-juan
```

Export validates the four-file generation receipt, typed geometry/compact parity and current
source partition identities before using prepared surfaces. It stages and checks a complete bundle
before replacing the previous bundle. Profiles use actual source samples, actual water pixel
centers and the production observer-specific canopy-clearance helper. They are sampled explanatory
profiles, not GDAL engine diagnostics or explanations of every ray in a cell aggregate.

The `san_juan_lessons_v2` export seals a canonical `semantic_contract` and payload checksums.
Canonical JSON uses sorted keys, compact separators, finite numbers, UTF-8 and a trailing newline;
`identity_json` retains these exact bytes for browser hashing. Scientific generation, original
producer revision, assumptions, grids, native resolutions/vintages, support scope, curve and pair
references travel with checksummed `production-evidence.json`. Export-time `code_revision` and
render timestamps are informational and excluded from bundle identity. Preview byte changes can
change bundle identity while keeping science unchanged. Metadata must also agree with retained
producer/grid/curve evidence, even after an envelope is rehashed. This unsigned contract proves
internal integrity and consistency, not provider authenticity or protection against replacing all
evidence and hashes together. Repeated export of this generation produced the same bundle identity. Source paths are
kept in ignored run manifests; published metadata uses stable identities and source rights.

On October 3, 2026, the bounded rebuild used checksum-validated cached real USGS elevation,
ETH 2020 canopy windows and Natural Earth geometry. The full model plus legacy rendering took
15.44 seconds; scientific export took about 2 seconds. Initial bundle measurements are recorded
by the checker (see the current checker output; historical measurements do not describe the revised projected previews).
These timings are local measurements, not performance guarantees. Sampling/resolution/clearance
sensitivity has not yet been run; this is a recorded robustness gap, not field qualification.

The small derived grids, sampled profiles and geometry retain source attribution and rights in
`manifest.json`: USGS/Natural Earth public domain; ETH canopy CC BY 4.0, credited to Lang, Jetz,
Schindler and Wegner (2023). These derivatives include clipping, reprojection, maximum canopy
resampling and modeled LOS. Raw regional assets are not committed. The scientific bundle contains no third-party photographs; the separately credited
Lime Kiln presentation below includes a CC BY 2.0 photograph and two CC BY-SA 4.0 shoreline views. Offline checks validate bundled integrity and scientific assertions; they do not
independently reproduce the real model from raw data.

## Static walkthrough presentation

The [detailed sightline page](sightline-details.md) preserves three chapters: one pair, one source’s candidate targets, and a
per-target maximum across included **land** sources. All six principal figures, comparisons
and contributor records are visible without JavaScript. The original scientific bundle and its
`san_juan_lessons_v2` identity remain unchanged; the historical lessons/indexes remain archived
canonical evidence, not the current reader journey.

After exporting the scientific bundle, generate the presentation separately:

```bash
python -S scripts/render_documentation_walkthrough.py
python -S scripts/render_documentation_walkthrough.py --check
```

The renderer needs only the committed bundle and the Python standard library. It checks the
scientific bundle before reading it. `assets/examples/walkthrough/` contains six full-resolution
SVG figures in desktop/mobile and light/dark variants, a per-target summary, and a separate
checksummed presentation manifest. The read-only check regenerates expected bytes in memory;
it rejects edited values, stale figures, changed source identities, and stale summaries.
The strict MkDocs hook runs both checks.

`target-summary.json` records the maximum valid combined pair weight for each included target,
source role, complete included-source population, original scientific generation and pair checksum.
It verifies unique role/source/target keys before aggregation. Candidate, valid, missing and
outside-candidate counts remain separate. No-valid-contributor results stay null, computed zeros
stay zero, and partial maxima retain their missing counts. This is an example-derived summary,
not a new supported product. It does not pool roles, compute union visibility, or multiply the
centroid diagnostic into the combined value.

The maps use the recorded projected coastline/cells and uniform metres-to-pixels transforms.
A shared linear 0–1 scale is used for all three source comparisons and the target maximum.
Source A’s local candidate footprint defines the shared first/second-chapter extent with extra
coastal context; the final map fits all recorded cell corners with 1.2 km of geographic padding, including candidates beyond the prepared-raster footprint. Static profile exaggeration
is calculated from the physical axis ranges and drawing dimensions. Light/dark variants and
mobile layouts change only rendering. Island-label reference: the
[BLM regional map](https://www.blm.gov/sites/default/files/orwa-rac-sanjuan-map.pdf).

## Browser checks

The explorer filters the committed pair records. It never recalculates LOS in the browser.
Install documentation and browser tools separately from the geospatial runtime:

```bash
python -m pip install -r requirements-docs.txt -r requirements-browser.txt
python -m playwright install chromium webkit
python -m mkdocs build --strict
python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa
```

The checker serves the built site locally and blocks third-party requests. It verifies the
homepage and all three chapters at desktop/mobile widths in light/dark themes, loaded figure
assets, visible results/contributors, absence of required controls, 200% CSS zoom reflow,
compatibility URLs under the GitHub Pages base path, and the complete JavaScript-disabled page.
Screenshots and a machine-readable receipt are saved in the output directory. Screenshots
still need visual inspection; automated assertions are not a human novice study.

The [executed validation and limitations report](reports/real-data-examples-validation.md)
records full model/export operations, both supported Python checks, browser evidence and the
remaining sensitivity and human-review gaps.

## Projected display and reading convention

Plan maps and input previews use `inputs/display-geometry.json`, projected to the recorded analysis
CRS. The original raster affine transform and full raster footprint determine the viewport.
One metres-to-pixels scale applies to both axes, including rotated/non-square affine pixels;
nearest decimation uses the first pixel of each display block and clips partial edge blocks.
It performs no averaging, interpolation or display smoothing. Heights have fixed ranges and units;
pink is categorical missingness. Changing input layers preserves the selected pair and viewport.
The static and interactive transforms agree within one rendered pixel after coordinate rounding.
Profiles retain physical distance and elevation axes with explicit vertical exaggeration.

The fixed walkthrough carries the same manifest-selected land pair A → B through all three
chapters. Historical seven-lesson references in the scientific bundle preserve recorded curated
evidence; the detail page does not require the old explorer. All displayed values are inserted
from canonical records at build time, including the product guide’s “Read one result.”

The retired `--render-only` command errors with a migration message. Explicit `--legacy-render`
uses existing validated results and writes to ignored `work/legacy-demo/`; `--legacy-output`
cannot target documentation or a tracked directory. Ambiguous mode flags error before producers run.
The supported default and `--model-only` never replace the compatibility redirect.

## Manual reader checklist

- Locate A and B within recognizable island geography.
- Match P/Q on the map and profile; distinguish one explanatory path from the pair population.
- Read the ground-only and ground-plus-trees comparison without toggling anything.
- Follow all A’s candidate targets, including zeros and the surrounding noncandidate cells.
- Explain the per-target maximum across included land sources and find A in B’s contributor table.
- Distinguish a valid zero, missing candidate evidence and sources outside the candidate set.
- Confirm that complete coverage means the recorded candidate population, not all viewpoints.


## Lime Kiln presentation

The main [example](examples.md) uses the **30 m regional delivery**, independently of the older
100 m San Juan scientific bundle retained for [A sightline in detail](sightline-details.md).
All 661 candidate pairs for land source `8728d1061ffffff` come from the same delivered static
and observation tables. The five retained regional observer samples replace the demo's two.
All original numeric values and producer states are preserved. Shared lineage/coverage fields
are stored once in `source-pairs.json` under `common_metadata`.

To rebuild the small excerpt with access to the validated delivery and retained run inputs:

```bash
python scripts/export_lime_kiln_regional.py /path/to/MarineCast/Data/Viewshed \
  --observer-validation /path/to/MarineCast/Reports/viewshed-full-regional-2026-11-05/land-observer-validation.json
```

Add `--check` for exact read-only reproduction. This needs the toolkit's core geospatial packages.
The exporter verifies the delivery checksums for both land tables and their metadata, exact
pair/weight parity, unique keys, source role, composition, configuration identity and the retained
observer-file checksum. It reads the source geometry and coast paths recorded in delivery metadata;
those retained inputs must be available. It does not acquire data or run LOS.

Only the small excerpt, projected cell/context geometry and evidence are written under
`assets/examples/lime-kiln/`. Natural Earth context is clipped and simplified to 30 m for display;
it does not replace the regional model's reconciled water mask. Native H3 boundaries and the
retained observer coordinates are unchanged. `regional-source.json` records the authoritative
file hashes, recorded assumptions, source credits and excerpt payload checksums. The photograph
remains unmodified, credited to Dave Sizer, CC BY 2.0, with its own checksum.

Rebuild the presentation using only committed assets and Python's standard library:

```bash
python -S scripts/render_lime_kiln.py
python -S scripts/render_lime_kiln.py --check
```

The renderer produces five maps in desktop/mobile and light/dark variants, plus the regional
distance-decay illustration. All maps retain the complete candidate population and one extent.
Distance rings show approximate projected 10/20/30 km radii around the source centroid; recorded
weights use great-circle centroid distance. Numeric terrain/canopy integration is untouched.

For vegetation, hatching is a **derived display classification** where terrain equals zero and
the vegetation factor equals one. The regional producer records `vegetation_state=positive`
for these neutral values; that original state is preserved. The presentation never interprets
these values as an unobstructed view or replaces them with missingness.

`manifest.json` binds the regional excerpt evidence and every rendered output. The MkDocs hook
checks it on every build. The sightline and tree SVG schematics in `assets/explain/` remain
educational illustrations; the plotted decay now uses the regional 9 km / 2.7 km / 30 km settings.
The older demonstration's stored assets and scientific identity remain intact.

[November 6 documentation validation](development/lime-kiln-validation.md) records checks and
unrun paths. Browser checks cover the regional Lime Kiln maps, How it works, the retained detailed
sightline page and the interactive regional target example.


The shoreline section adds one CC BY-SA 4.0 panorama by Chris Light (2024) and one
CC BY-SA 4.0 photograph by Chiwauk (2021), retained as uncropped Commons thumbnails.
`shoreline-sources.json` records source links, licenses, dates, downloads and checksums.
The camera geotag for photo A is projected to EPSG:32610 and checked against the native R7
source cell; a regression check independently verifies both. The panorama has no published
camera coordinate and receives no pin. The map uses retained active land/source/observer
geometry; it is not a trail map. The magnetic photo heading is recorded but not drawn as a
true-north arrow. The Google Maps suggestion remains a credited outbound link only.
The same renderer builds `shoreline.svg`; the manifest and build hook check the two photo
hashes and source metadata as well as the existing maps. Photos provide context, not numerical
or field validation, and neither source-cell weights nor the model are changed.

## Multiple locations and the regional overview

The visual walkthrough uses one shared authoring template, `scripts/templates/source-walkthrough.md`,
with place-specific narrative/photo metadata in `docs/assets/examples/places.json`. The four tabs
are ordinary links with an active-page state: they work with browser history, deep links and no
JavaScript, and load only the selected example's assets. The lead photo sits beside the introduction
on desktop and stacks below it on narrow screens. Lime Kiln retains its existing source selection.
Everett selects land source `8728d0a74ffffff`; Larrabee selects inland source `8728d1473ffffff`.
The new place markers are H3 centroids, not surveyed camera positions. The same seven chapters,
five weight/study maps, component table, photo context and record reader appear for each place.

Larrabee's selected source has zero combined support for all 285 candidate targets, despite 201
positive terrain weights. Its component table therefore selects the highest-terrain non-self pair
rather than an arbitrary zero-combined winner. This illustrates the retained inland samples and
canopy assumptions; it does not assert that the park's coastal viewpoints are obstructed.
Everett's buildings and vessels are not explicitly represented by the terrain/canopy inputs.

Reuse the retained regional inputs and observer-validation receipt described above:

```bash
python scripts/export_lime_kiln_regional.py /path/to/Data/Viewshed \
  --observer-validation /path/to/land-observer-validation.json \
  --source 8728d0a74ffffff --output docs/assets/examples/everett
python scripts/export_lime_kiln_regional.py /path/to/Data/Viewshed \
  --observer-validation /path/to/land-observer-validation.json \
  --source 8728d1473ffffff --output docs/assets/examples/larrabee
python scripts/build_regional_overview.py /path/to/Data/Viewshed
python -S scripts/render_source_examples.py
python -S scripts/render_regional_overview.py
```

The exporters accept `--check` for deterministic comparison with the retained data. Ordinary
MkDocs builds run only the standard-library render checks, not acquisition or data extraction.
The source excerpts preserve every candidate pair, original weights and states. The overview
aggregates all delivered land pairs by target using the **maximum valid combined weight**, counts
missing and valid candidates separately, and never pools land/water roles. It covers all 13,461
water targets from 26,094 land sources and 5,555,318 candidate pairs. It is a presentation summary,
not an effort-weighted mean, a sighting probability or a new delivered scientific product.

The additional photos are credited beside the images and checksummed in their source presentation
manifests. Emersb and Cody Logan: CC BY-SA 3.0; Joe Mabel: CC BY-SA 3.0; SounderBruce: CC BY-SA 4.0.
Exact camera coordinates are unverified, so no photo pins are invented for Everett or Larrabee.
The Larrabee coastal and amphitheater photos provide wider park context, not evidence from the
selected inland source cell. All photo dates and modifications remain explicit.

### Navigating the full regional summary

The fourth example embeds `assets/examples/region-overview/map.html` in an iframe. This is a
standalone Leaflet map with pan, zoom, keyboard navigation, cell popups, layer controls, case-study
area shortcuts and a full-region reset. The full-page link opens the same HTML document. Shortcuts
move the map; every displayed weight continues to summarize all candidate land sources.

Leaflet 1.9.4 is bundled locally under `assets/vendor/leaflet-1.9.4`, with its BSD-2-Clause license
and source/checksum receipt. The [Leaflet reference](https://leafletjs.com/reference-1.9.4.html)
documents the Canvas renderer and GeoJSON layers used here. OpenStreetMap street tiles are requested
only for the current view, use normal browser caching and carry visible attribution, following the
[OSM tile policy](https://operations.osmfoundation.org/policies/tiles/). The local Natural Earth
coastline and weight layers also work without online tiles; select **Coastline (offline)**.
The page retains an expandable static fallback and direct SVG/summary links.

The GeoJSON export uses longitude/latitude coordinates for the same native H3 R7 boundaries and
copies each original target ID, maximum combined weight, candidate counts and coverage state.
Leaflet projects the display into Web Mercator. The legend offers four palettes on a fixed 0–1
scale at 85% fill opacity. The browser uses the precomputed component maxima in `map-components.json`
and joins them to the same H3 IDs. The display receipt binds the map payloads to their inputs
with SHA-256 checksums.

The component exporter validates the original land artifact checksum and reproduces the
combined maximum and coverage for every target before calculating independent terrain and
distance maxima. The vegetation maximum uses only candidates with positive terrain support.
The export keeps valid counts for every component and eligible counts for vegetation.
Missing terrain leaves vegetation eligibility unknown. Known zero terrain makes a pair
ineligible for the vegetation maximum. These maxima can come from different sources.

Regenerate or verify the component values and geographic display from the retained delivery:

```bash
python scripts/build_regional_overview.py /path/to/Data/Viewshed --components-only
python scripts/build_regional_overview.py /path/to/Data/Viewshed --components-only --check
python scripts/export_regional_html_map.py
python scripts/export_regional_html_map.py --check
python -m pytest -q tests/pipeline/test_source_examples.py
node --check docs/assets/examples/region-overview/map.js
```

The exporter uses the existing geospatial environment (H3, PyProj and Shapely). Ordinary MkDocs
builds verify its checksums, target population and copied values with the standard library;
they do not regenerate GeoJSON or acquire tiles. The focused tests also verify every native cell
boundary and closed coordinate ring. The browser checker targets the iframe's controls. The prior
SVG controller is no longer loaded; its prototype files remain local for review.

Touch pinch and offline/network-error behavior require dedicated device/browser checks; resizing
a desktop viewport or choosing the local coastline layer is not proof of those paths.
