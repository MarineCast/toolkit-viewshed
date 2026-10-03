# Real-data Examples validation — October 3, 2026

The six ordered PRs implement scientific contract corrections, generation integrity, a real San
Juan lesson bundle, seven guided lessons, a linked explorer and repeatable release gates. They
are review branches, not a merged release or deployment. The original `main` checkout remains
clean at `2849a048a8d588736e0bef1fe49d0be8b186988f`.

## Scientific and package evidence

| Gate | Executed result |
| --- | --- |
| Python 3.14.6 primary geospatial environment | 332 tests passed, 2 warnings, 44.32 s |
| Python 3.11.16 disposable geospatial environment | 332 tests passed, 2,463 warnings, 45.91 s |
| Baseline Ruff over `src tests` | Passed |
| Expanded Ruff and Black over incrementally maintained code | Passed |
| Strict mypy | Passed for 23 owned source files |
| Compile source modules | Passed |
| Installed wheel, resources and CLI outside checkout | Passed on both Python versions |
| Offline bundle check without site packages | Passed; 5,424 real candidate pairs |
| Strict MkDocs build without site packages, using only docs dependencies | Passed |
| Chromium and WebKit reader matrix | Passed; no page errors; third-party requests blocked |
| Local structural graph refresh | Code-only, no clustering; graph remains ignored/local |

Regression evidence includes the independent two-target canopy oracle, opposite survival
patterns, zero/tiny/anomalous kernels, a direct exponential-distance oracle with unchanged
unweighted population, invalid observed diagnostics, unavailable legacy evidence, exact projected
grid alignment, candidate additions/removals/replacements/empty universes, stale empty partitions,
partition corruption, all value/state pairs, compact diagnostic parity, prepared-source/audit
changes and rollback after staging/promotion failures. Newly surviving gaps were reproduced before
their fixes. Public data is real; synthetic fixtures occur only in tests.

The primary warnings are GDAL's future exception-default notice and Polars' unregistered
GeoArrow-storage notice. The minimum-version environment also has Affine 3 pending deprecation
warnings for existing `*` matrix operations in toolkit, tests and Rasterio. They remain visible;
none were blanket-suppressed. The local real run also emitted existing Rasterio `BLOCKXSIZE`
notices for untiled temporary surfaces and sandbox-limited Arrow CPU-cache discovery notices.
These did not fail execution. Material prints an advisory about future MkDocs 2 support; the
verified build uses pinned MkDocs 1.6.1 and Material 9.7.7.

The wheel check creates a temporary venv, force-installs the built wheel without dependencies,
and verifies that imports and both packaged YAML resources resolve inside that venv. Dependencies
are reused from the tested native environment. This is an installed-wheel check, not an editable
source import or a claim of an independent fresh download of every dependency.

## Actual bounded real run and export

The final model was rebuilt from committed producer code `573c340f68695dbc2c8e6de4734dc8e2f210374a`.
The explicit bounded runner used checksum-validated cached real USGS 30 m elevation, bounded ETH
2020 10 m canopy windows and Natural Earth geometry. It recomputed both land surfaces for 84
source areas and the water kernel for 92 source areas. No global raster download, large Salish
Sea run or synthetic substitution occurred. Fresh provider acquisition was not requalified.

The final production runner took approximately 14.57 s, including its production static-map
stage. Export took 2.02 s and 1.98 s on two successive runs. The exporter includes checked SVG
generation; standalone presentation rendering took 0.217 s. A strict docs build took 0.46 s.
These are local measurements, not performance guarantees.

- Geometry method: `4.0.0-research`; output receipt: `viewshed_output_set_v2`.
- Scientific generation: `4cbc6a9a15817c26594458caaf151f3d92011c981c69464e9d48ea71c3b3ec85`.
- Bundle identity: `3ced605314445f44d0f42fdf270d1ed6a542ba8aafe60256e4e5d9cf963c93c3`.
- Repeated export preserves the generation and bundle identities; documentation revision is
  informational and does not force acquisition.
- All 2,468 land and 2,956 water compact pairs match the previous actual San Juan generation:
  maximum absolute differences in terrain, distance, vegetation and final weights are zero.
- All four durable files, their shared receipt, compact/geometry parity, current partition
  identities and selected prepared-surface profiles were checked during real export.
- Committed offline checks establish bundle integrity and recorded assertions. They are not
  independent raw-data reproduction or field validation.

See the checked [manifest](../assets/examples/san-juan/manifest.json),
[export validation receipt](../assets/examples/san-juan/validation.json) and
[browser receipt](real-data-browser-checks.json). Source rights and transformations are retained
with the bundle; raw regional datasets are not committed.

## Reader checks and inspected evidence

The automated matrix uses Playwright 1.63.0 with Chromium build 1243 and WebKit build 2359,
1440×1000 desktop and 390×844 mobile viewports. It exercises all seven lessons; actual displayed
factor values; forward/inverse identity; mixed land/water roles; tiny positives, neutral canopy,
not-applicable canopy and noncandidate selections; keyboard selection; reset/back/next; input
layers; light/dark themes; 200% zoom; completed instant navigation away/back; blocked third-party
requests; and JavaScript-disabled figures and reveal tables. No browser engines were skipped.

Screenshots were captured and inspected for desktop/mobile and light/dark layouts, as well as
zoom and JavaScript-disabled views. The local initial browser payload is approximately 2.72 MB,
below 5 MiB. Gzip-compressed JSON/GeoJSON data is 320,898 bytes, below 2 MiB. All checked bundle
assets together are 3,885,871 bytes; preview images are lazy-loaded.

This is an automated task-based review plus maintainer visual inspection. **No human novice
usability study occurred.** The six reader questions map to the following visible evidence:

| Reader task | Evidence without technical panels |
| --- | --- |
| Identify real inputs and source versus target | Selected observer/water areas, real coastline, height-layer buttons, persistent coverage badge |
| Distinguish one viewing path from an area result | Actual sample dots, explanatory profile and visible path-versus-aggregate wording |
| Explain distance, terrain and vegetation | Real near/far curve markers, clear/blocked ground profiles and matched canopy comparison |
| Read the same pair in both directions | Shared pair record and identical forward/inverse value |
| Recognize missing, zero and unmodeled states | Missing-height preview, modeled-zero labels, hatch/outline states and explicit noncandidate message |
| Avoid probability/access interpretations | Visible static-support, missing-input and public-access limitations |

## Remaining limits

- Missing canopy affects 3,678 of 37,938 mapped-land pixels in the buffered analysis grid
  (9.694765%); the model uses zero-height fallback. This is run-level input coverage, not a
  measured fraction of affected rays or pairs. No mapped-land DEM pixels are missing in this audit.
- Analysis is 100 m; native elevation is 30 m and canopy is 10 m. Display grids are decimated
  to 400 m. Resampling does not create new detail. Profiles are explanatory sampled geometry,
  not GDAL engine rays or explanations of every path in an aggregate.
- Sampling-density, analysis-resolution and canopy-clearance sensitivity runs were **not run**.
  Curated cases have verified baseline values, not demonstrated robustness across those choices.
- Source vintages, execution completeness and observed-input coverage remain separate. Unknown
  local source years remain unknown. No current observer/boat activity, sightings, access or
  detection probability is inferred.
- Publication provides single-writer rollback, not concurrent-reader snapshot atomicity. Readers
  validate/retry the shared receipt; simultaneous writers need separate output directories.
- Legacy missing-unweighted outputs and version 1 receipts require the documented rebuild.
  Metadata refresh cannot relabel changed science. Cleanup may prune intermediates after
  validation; original source checksums and coverage remain in the receipt.
- GitHub CI is newly wired for minimum/primary Python plus docs-only browser gates. Local results
  above are executed evidence; a remote CI run is not claimed here. No merge, Pages configuration
  or deployment was performed.

## Reproduction gates

Run from the repository root in a compatible geospatial environment:

```bash
PYTHONPATH=src python -m pytest -q
ruff check src tests
python scripts/check_components.py
python -m compileall -q src
python scripts/check_installed_wheel.py
PYTHONPATH=src python scripts/run_san_juan_demo.py --config configs/san_juan_demo.yaml --rebuild --model-only
PYTHONPATH=src python scripts/build_documentation_examples.py --config configs/san_juan_demo.yaml --output docs/assets/examples/san-juan
python -S scripts/build_documentation_examples.py --check --output docs/assets/examples/san-juan
```

In a separate docs-only environment:

```bash
python -m pip install -r requirements-docs.txt -r requirements-browser.txt
python -m mkdocs build --strict
python -m playwright install chromium webkit
python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa
```

Exact local invocations and environment versions are retained in the task's delivered evidence
directory. See [reproduction documentation](../documentation-examples.md) for ownership, assets,
source rights and the distinction between model, export, presentation and offline checking.
