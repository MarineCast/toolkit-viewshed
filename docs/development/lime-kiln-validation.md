# Lime Kiln documentation revision · November 6, 2026

This revision improves the reading route and presents the source area containing Lime Kiln
using the validated November 5 regional delivery (30 m, R7, 30 km limit). It adds educational sightline/canopy/decay
illustrations, qualified use cases, complete first-run instructions and a regional excerpt reader.
The original scientific bundle, 24 detailed-walkthrough figure variants, and delivered datasets
are unchanged. The Lime Kiln presentation has its own checksummed manifest and 20 map variants and a matching regional decay illustration. The original 100 m demonstration
remains available on the separate detailed sightline page.

## Checks executed

| Check | Result and scope |
| --- | --- |
| Fresh install | Python 3.14.0, macOS arm64, isolated venv; package core plus documentation/browser QA tools installed; no `osgeo` bindings or acquisition extra |
| Offline example | Passed production product validators, nonempty output and exponential formula parity; result JSON used to open and validate the exact profile |
| Included regional reader | Land: 385 pairs, 13 positive, mean 0.001574; water: 661 pairs, 67 positive, mean 0.048036 |
| Focused documentation tests | 82 passed: scientific bundle, existing walkthrough, display geometry and new Lime Kiln source/metric/state/locator checks |
| Asset integrity | Regional static/observation/metadata checksums, 661-row exact parity, retained observer hash, and excerpt regeneration passed; original model outputs unchanged |
| Site | Strict MkDocs build and local-reference checker passed |
| Code hygiene | Black and Ruff on changed Python, plus `git diff --check`, passed |
| Chromium | New pages, original detailed sightline, homepage and regional explorer passed; no page errors or failed local assets |

Browser checks cover desktop 1440×1000 and mobile 390×844, light/dark themes, image loading,
chapter anchors, 200% CSS zoom reflow, no-JavaScript content, compatibility URLs, site base path,
and regional target/observer interactions. Screenshots of the new maps and explanatory figures
were visually inspected. These are layout and software checks, not a novice-reader study.
Playwright 1.63.0 used an existing Chromium 1228 installation through
`VIEWSHED_CHROMIUM_EXECUTABLE`; the CI default remains Playwright's managed browser.

## Regional excerpt checks

The updated walkthrough includes all 661 source-to-target records, five retained observers,
35 positive combined values and 331 no-baseline neutral vegetation factors. The normalized
logistic curve is 9 km / 2.7 km with a 30 km cutoff. Independent centroid-distance and curve
calculations agree with all recorded values within float rounding. Every map retains the same
candidate set; displayed vegetation hatching does not change the producer's saved state.

The regional excerpt exporter was run and then checked against the retained delivery and inputs.
The fresh-install/offline and four-target reader checks above belong to the initial documentation
pass earlier on November 6; they were not repeated for this data-selection change.

## Reproduce the checks

```bash
python -S scripts/build_documentation_examples.py --check
python -S scripts/render_documentation_walkthrough.py --check
python -S scripts/render_lime_kiln.py --check
python -m pytest -q tests/pipeline/test_documentation_bundle.py \
  tests/pipeline/test_documentation_walkthrough.py \
  tests/pipeline/test_documentation_geometry.py \
  tests/pipeline/test_lime_kiln_documentation.py
python -S scripts/check_documentation_links.py
python -m mkdocs build --strict
python scripts/check_documentation_browser.py --engines chromium --output work/browser-qa
```

Install browser tools from `requirements-browser.txt` and a matching browser before the final
command, as described in [documentation reproduction](../documentation-examples.md#browser-checks).
The normal build/check commands do not render replacement assets or run a model.

## Limits

No native GDAL/terrain or regional model run, new source acquisition, sensitivity study, field
validation, full package suite, WebKit/Firefox check or GitHub Pages publication was performed.
The photograph's CC BY 2.0 attribution and checksum are retained in the
[Lime Kiln receipt](../assets/examples/lime-kiln/manifest.json); its 2010 scene is context, not
proof of 2020 canopy or present-day conditions. The How it works use cases are proposed analysis
and planning applications, not an implemented or validated observation-bias correction.


## Shoreline context addition · November 6, 2026

Added two licensed eye-level views after the combined result: Chris Light's 2024 Haro Strait
panorama and Chiwauk's 2021 tree-framed shoreline photograph. The latter's published camera
geotag falls in the example source cell and receives pin A. The panorama lacks a camera coordinate
and is not pinned. Both retain their full composition as Commons-resized thumbnails, with visible
CC BY-SA 4.0 attribution and links. The suggested Google Maps photo remains a credited link only.
`shoreline-sources.json` retains exact source/download links, dates, licensing and checksums.
The source-area locator uses existing retained geometry; model values are unchanged.

Seven focused Lime Kiln tests passed, including independent photo-coordinate projection and H3
membership; the renderer's byte/hash check, 252 local-reference checks, strict MkDocs, changed-Python
Black/Ruff and diff whitespace checks passed. The in-app browser verified the section at desktop
1440 × 1000 and mobile 390 × 844, with light and dark appearances, loaded images, no horizontal
overflow, chapter navigation and the locator-to-photo link. Screenshots were inspected in the
browser. The existing browser-check script was updated for the new chapter and figures, but its
full automated suite, no-JavaScript and zoom checks were not rerun for this addition.
No model execution, scientific data acquisition, delivered-data change or publication occurred.
Photographic context is not quantitative validation; geotag accuracy is not independently surveyed.

## Four-tab walkthrough · November 6, 2026

The lead photograph now sits to the right of each introduction on desktop. Three stable-URL
location tabs share the same seven-chapter template: Lime Kiln (661 pairs, 35 positive), Everett
port area (242 pairs, 45 positive), and Larrabee inland woods (285 pairs, zero positive). The fourth
tab shows maximum valid combined land-source support for every delivered regional water target.
Source selection, geographic scope and photo-location uncertainty are explicit. The latter two
examples use centroid markers and wider-place photographs, not invented camera pins.

All three source exporters reproduced their committed excerpts exactly from the checksum-verified
delivery and retained observers. The full-region exporter checks key uniqueness, finite/ranged
values and delivery counts before aggregation. A separate pandas groupby matched every Arrow-derived
maximum and candidate/valid count for all 13,461 targets and 5,555,318 land pairs exactly.
The code preserves missing versus zero and never combines observer roles.

All 88 focused documentation tests passed. Strict MkDocs, 353 local references, deterministic
render/photo checks, Black, Ruff and whitespace checks passed. The in-app browser verified all
four tabs, active-page indicators, right-hand hero placement at 1440 × 1000, all three source map
sequences and photo sections at 390 × 844, light/dark maps, image loading and no horizontal
overflow. Regional label clipping and context-boundary artifacts found during inspection were
corrected and visually rechecked. The default viewport and light appearance were restored.

The existing automated browser script now includes the additional pages; its full automated run,
no-JavaScript and 200% zoom checks were not repeated for this revision. No full package suite,
regional/native model execution, new terrain/canopy acquisition, delivered-data modification or
publication was performed. New photo acquisition is limited to the four licensed images whose
source/download URLs, dates, licenses and checksums are retained in the place metadata/manifests.

## Regional map navigation · November 6, 2026

Added progressive SVG navigation to the fourth example tab: pan, zoom, reset, keyboard controls,
two-pointer pinch handling and target selection. The full 13,461-cell population, fixed color
scale and scientific summaries are unchanged. The displayed distance scale updates with zoom.
A static fallback remains in the document.

Current checks: three Node navigation-state tests and five focused source/aggregate tests passed;
all regional SVG weights match the saved target summary. Strict MkDocs, local references, changed
Python formatting/lint and whitespace checks passed. In-app verification covered desktop/mobile
layout, zoom buttons, keyboard zoom/pan/reset, pointer drag, a selected target's actual value,
light/dark styles and reinitialization after moving between example tabs. Native SVG hover titles
were replaced by a persistent detail panel in the interactive copy after hover interaction stalled
the browser automation; the revised selection was verified successfully.

The full automated browser suite was extended but not rerun in this revision. Touch pinch,
modifier-wheel input, a JavaScript-disabled browser session and native/regional scientific runs
were not executed. No Data changes or publication occurred.


## Embedded HTML regional map · November 6, 2026

The regional example now embeds a standalone Leaflet 1.9.4 HTML map instead of the SVG controller.
The library and BSD license are bundled locally. Street tiles use attributed OpenStreetMap service;
a local Natural Earth coastline option and static SVG fallback remain available. Regional IDs,
weights and states are unchanged in the geographic display export, which has its own source and
payload checksums. No delivered Data or scientific calculation changed.

Seven focused tests passed, including every native H3 boundary and every original target value.
Strict MkDocs, local-reference checks, changed-Python Black/Ruff, JavaScript syntax and whitespace
checks passed. In-app checks covered the embedded desktop/mobile and full-page mobile views,
all three area shortcuts, keyboard pan, zoom, full-region reset, layer toggles and a selected cell
(`8728d1a96ffffff`, combined 0.828998 displayed; 365/365 valid candidates). Online street tiles and
the local coastline both rendered; the weight layer could be toggled off and on.

The extended full automated browser suite was not executed. Native touch pinch, a disconnected
network, a JavaScript-disabled browser and native/regional model runs were not tested. The prior
SVG interaction receipts remain historical. This revision was not published.
