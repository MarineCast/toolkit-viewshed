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

## Browser checks

The explorer filters the committed pair records. It never recalculates LOS in the browser.
Install documentation and browser tools separately from the geospatial runtime:

```bash
python -m pip install -r requirements-docs.txt -r requirements-browser.txt
python -m playwright install chromium webkit
python -m mkdocs build --strict
python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa
```

The checker serves the built site locally and blocks third-party requests. It checks every
lesson, pair parity, role/factor changes, missing results, keyboard selection, reset, theme,
mobile layout, 200% CSS zoom reflow, navigation back and JavaScript-disabled reading. Screenshots and a
machine-readable receipt are saved in the output directory. These are automated checks,
not a human novice review.

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

Fixed prose and default tables are labeled **Worked example A → B**. The explorer's
**Your current selection** heading, numeric inspector, map and profile describe its current state.
Named cases use stable exported references with checked evidence thresholds: strong canopy effect
retains at most 20% of unweighted ground support, and little effect retains at least 98%, with
baseline support above 0.02. These indices are not percentages of visible area. The distance
comparison shares one source; its primary result shows the assumed diagnostic, not final scores.

The retired `--render-only` command errors with a migration message. Explicit `--legacy-render`
uses existing validated results and writes to ignored `work/legacy-demo/`; `--legacy-output`
cannot target documentation or a tracked directory. Ambiguous mode flags error before producers run.
The supported default and `--model-only` never replace the compatibility redirect.

## Manual reader checklist

- Identify ground, tree height, coastline, source area and water target.
- Follow the source sample and target endpoint on the map and profile; explain why one path is not an area result.
- Explain the assumed distance rule, ground obstruction and matched canopy comparison.
- Read the same role/source/target pair forward and inverse without changing its value.
- Recognize modeled zero, tiny positive, missing input, no candidate and neutral/not-applicable canopy.
- Avoid conclusions about sighting probability, actual observers, public access or field accuracy.

Automated CSS reflow is not a manual browser-zoom check or a novice usability study. Sampling,
resolution and clearance sensitivity, human reader validation and empirical field validation remain unperformed.

The [instructional polish validation report](reports/instructional-polish-validation.md) records the projected display, exact pair/generation parity, isolated bounded rebuild and browser inspection for PR B.
