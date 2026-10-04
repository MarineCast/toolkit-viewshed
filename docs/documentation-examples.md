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
by the checker (approximately 0.32 MiB compressed data and 2.7 MiB total before presentation).
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
mobile layout, 200% zoom, navigation back and JavaScript-disabled reading. Screenshots and a
machine-readable receipt are saved in the output directory. These are automated checks,
not a human novice review.

The [executed validation and limitations report](reports/real-data-examples-validation.md)
records full model/export operations, both supported Python checks, browser evidence and the
remaining sensitivity and human-review gaps.
