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
resampling and modeled LOS. Raw regional assets are not committed. No new third-party imagery is
redistributed. Offline checks validate bundled integrity and scientific assertions; they do not
independently reproduce the real model from raw data.

## Static walkthrough presentation

The default page presents three chapters: one pair, one source’s candidate targets, and a
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
evidence; the main page no longer requires the old explorer. All displayed values are inserted
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
