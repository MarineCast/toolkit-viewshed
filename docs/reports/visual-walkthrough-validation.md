# Static visual walkthrough — October 5, 2026

The Examples page now teaches three connected scales by scrolling: one recorded land-source
pair A → B, every candidate target for A, and the maximum valid combined pair weight for each
target across the included land sources. Six finished figures and the pair/contributor tables
are visible without interaction or JavaScript. Detailed execution stays in the reproduction guide.

## Implementation and artifacts

| Files | Result |
| --- | --- |
| `docs/examples.md`, `docs/assets/examples/examples.css` | Wide three-chapter atlas, comfortable prose, stacked coverage comparisons, light/dark figures and explicit missingness legends |
| `docs/index.md`, `mkdocs.yml` | Guided reading is the primary homepage route and first Examples entry |
| `docs/understand/index.md`, `docs/products/index.md` | Concepts follow the three scales; “Read one result” is populated from the committed pair |
| `docs/getting-started/quick-start.md`, `docs/documentation-examples.md` | Synthetic distance-only quick start is distinct from reading real results; execution/export/presentation remain separate |
| `docs/workflows/index.md`, `docs/san-juan-demo.md`, `docs/development/index.md` | Current journey replaces obsolete seven-lesson descriptions; existing URLs remain |
| `scripts/render_documentation_walkthrough.py`, `scripts/docs_hooks.py` | Checked, deterministic presentation export and build-time values; scientific bundle is unchanged |
| `scripts/check_documentation_browser.py`, `tests/pipeline/test_documentation_walkthrough.py` | Browser acceptance for the new reading flow; aggregation and presentation-integrity regressions |

`docs/assets/examples/walkthrough/` contains `pair-map`, `profile`, `ground`, `canopy`,
`combined` and `target-summary` SVG figures, each in desktop/mobile and light/dark variants
(24 SVG assets). Full-resolution SVGs are displayed directly. The accompanying
`target-summary.json` and `manifest.json` preserve the derivation and presentation identity.

All principal maps use recorded EPSG:32610 geometry, equal metric scale on both axes,
verified island labels, accurate projected scale bars and fixed linear 0–1 support scales.
The first two chapters share one extent. The regional map fits all recorded H3 corners;
its display extends beyond the prepared-raster footprint to avoid cropping candidates.
A regression checks that all 229 exported target polygons fit both regional layouts.
Label leader lines, desktop captions and dark-theme colors were refined after screenshot inspection.

## Scientific preservation

The original `docs/assets/examples/san-juan/` files are byte-for-byte unchanged from the base
commit. Scientific bundle identity remains
`cbef9b87bbe37337f229cfed23408a9582a6aec5c887ab0fc1808125030ae2b5`;
underlying generation remains
`558d7a9aedbe7681e170107d21d6e0449ac776d3be2f9c47eeb33c6eaade11c5`.
The presentation manifest pins both, plus pair identity, source hashes, geometry extents,
operator, role and figure checksums. Its read-only checker regenerates expected bytes in memory,
so manually changed or stale summaries/figures fail even if presentation metadata is edited.

A has 29 candidate targets: 16 positive ground-only support indices, 5 positive canopy indices,
and 5 positive combined scores; the other 24 combined scores are computed zeros.
The summary contains 84 included land-source areas and 197 targets with land-source candidates,
with 2,468 valid land candidate records and no missing candidate records in this committed
example. Another 32 included water targets have no land-source candidates and retain null
summary values with an explicit outside-candidate state. No absent record becomes zero.
B has 29 valid land-source candidate records; its strongest score is 0.0778447613120079.
A contributes 0.028681768104434013, without multiplying the centroid diagnostic again.

Tests cover computed zeros, missing values, no valid contributors, partial maxima, empty inputs,
duplicate pairs (including an unselected role), tied maxima, source-role separation,
nonfinite/out-of-range values, scenario separation, ordering determinism and edited artifacts.
This maximum is an example-derived summary, not a new supported product, probability or union visibility.

## Executed checks

Documentation/browser tools were installed in the isolated
`/private/tmp/viewshed-walkthrough-docs` environment (Python 3.14); the native package tests used
the existing Python 3.11 geospatial interpreter with `PYTHONPATH=src` pointing to this worktree.
Browser binaries are isolated in `/private/tmp/viewshed-walkthrough-browsers`.

```bash
python3 -S scripts/build_documentation_examples.py --check --output docs/assets/examples/san-juan
python3 -S scripts/render_documentation_walkthrough.py
python3 -S scripts/render_documentation_walkthrough.py --check
python3 -S scripts/check_documentation_links.py
python -m mkdocs build --strict
node --check docs/assets/examples/explorer.js

PYTHONPATH=src python -m pytest -q \
  tests/pipeline/test_documentation_bundle.py \
  tests/pipeline/test_documentation_geometry.py \
  tests/pipeline/test_documentation_walkthrough.py \
  tests/pipeline/test_terrain_factor_composition.py \
  tests/pipeline/test_static_pair_universe.py

python -m ruff check scripts/render_documentation_walkthrough.py scripts/docs_hooks.py \
  scripts/check_documentation_browser.py tests/pipeline/test_documentation_walkthrough.py
python -m black --check --line-length 100 scripts/render_documentation_walkthrough.py \
  scripts/docs_hooks.py scripts/check_documentation_browser.py \
  tests/pipeline/test_documentation_walkthrough.py
python3 -m compileall -q scripts/render_documentation_walkthrough.py \
  scripts/docs_hooks.py scripts/check_documentation_browser.py

PLAYWRIGHT_BROWSERS_PATH=/private/tmp/viewshed-walkthrough-browsers \
  python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa
git diff --check
```

- **97 tests passed**, no skips; 90 Rasterio affine deprecation/GeoArrow registration warnings.
- Scientific and presentation offline checks passed. Strict MkDocs build, local-reference checks,
  JavaScript syntax, changed-file Ruff/Black, compilation and whitespace checks passed.
- **Chromium and WebKit passed** on the finished site, with zero page errors or failed local assets.
  Both block third-party requests; each retained the 5 MiB initial-page resource-size gate.
- Homepage and all three chapters were captured at 1440×1000 desktop and 390×844 mobile,
  in light/dark themes. Figures, tables, captions, labels and themed colors were visually inspected.
  Figure captures suppress the sticky theme header during capture only, preventing screenshot overlays.
- Complete JavaScript-disabled reading passed at desktop/mobile widths, with all six figures and
  both result tables. Theme toggle, instant navigation back, compatibility HTML/page links under
  `/toolkit-viewshed/`, and 200% CSS zoom reflow passed. This is CSS reflow, not a manual browser-zoom test.

Screenshots, `browser-receipt.json`, build logs and test logs are retained in ignored `work/`.
Useful captures include `work/browser-qa/webkit-desktop-light-figure-6.png`,
`webkit-mobile-light-figure-1.png`, and `webkit-mobile-dark-figure-6.png` in that same directory.
The receipt records the exact presentation identity and capture time.

## PR quality-gate follow-up

The initial PR #15 run passed all 467 package tests on both Python 3.11 and 3.14, plus both
documentation jobs. Both geospatial jobs then failed the same expanded Ruff gate with eight
violations: browser callbacks captured engine-loop state, imports were unsorted, and three
apostrophes conflicted with the gate's Unicode rules. Compile and installed-wheel steps were
skipped after that failure. The original local Ruff command used only the default rule set.

The follow-up binds each callback's engine-specific state, sorts imports, and uses plain
apostrophes. The renderer and its tests now join `scripts/check_components.py` so the expanded
rules cover the new files too. Rendering-loop refactors preserve identical output bytes;
the presentation identity and scientific bundle remain unchanged.

Local follow-up validation passed:

- `python scripts/check_components.py`: expanded Ruff, Black on 50 files, strict mypy on
  30 source files, using Python 3.14.
- Documentation bundle, geometry, walkthrough, component-workflow and case-study tests:
  **107 passed**, no skips; 361 existing affine/GeoArrow warnings, using Python 3.11.
- Strict MkDocs build, presentation check and Chromium/WebKit browser checks, with zero
  page errors and failed local assets. New evidence is in ignored `work/ci-fix-browser-qa/`.

## Remaining boundaries

No acquisition, regional model execution, notebook execution, installed-wheel check or full package
suite was performed locally for this presentation change. The initial hosted run's full-suite
result is recorded separately above. The existing geospatial/scientific contracts
remain unchanged. Sampling/resolution/clearance sensitivity, empirical field accuracy and a
novice usability study remain unperformed. Generalized coastline and the recorded 9.69% missing
land-canopy input fraction remain relevant limitations; zero-height fallback is not measured bare land.

Only Viewshed is modified. This work does not publish a release, deploy documentation or push main.
