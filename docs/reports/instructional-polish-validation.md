# San Juan instructional polish: executed validation

Validated October 4, 2026. This is PR B, dependent on [producer integrity PR #11](https://github.com/MarineCast/toolkit-viewshed/pull/11); [PR #12](https://github.com/MarineCast/toolkit-viewshed/pull/12) must be reviewed after #11. Base revision: `c9c54b7050c8c61bc1ae66a0d9a1c125f5209f43`. Implementation/native-run revision: `653a55f2471ea587a1a75fc1142f743304ecbb60`. Final head and remote checks are recorded on the PR. Neither branch was merged, deployed or published.

## Findings and resulting behavior

The baseline regression run reproduced nine failures: default routing to the retired renderer, ambiguous modes, retired render routing, stretched square raster pixels, and profile observer/endpoint/role mismatches. Existing forward/inverse indexes and role-specific pair identity already worked and remain intact.

All plan layers now use the exporter-owned projected display derivative, original affine footprint, clipped decimated blocks and one metres-to-pixels scale. Tests cover square, non-square and rotated affine pixels, three viewport shapes, partial edge blocks and independent native reprojection. Coordinate/display agreement must remain within one rendered pixel; the square-spacing browser check allows 0.001 SVG units for browser float rounding. Static and interactive previews use the same bounds, north-up convention and fixed height ranges. Sampling is nearest decimation of the first pixel in each clipped block, without averaging, interpolation or smoothing.

Actual active source geometry, role-specific observer samples, curated target support and selected profile endpoints are linked. P/Q on the map identify the source sample and target endpoint on the profile. No land profile is borrowed for water-role records; unexported support/profile availability is stated. The visible boundary is: “One explanatory sampled path; the area result summarizes the modeled population. This is not an engine trace or every contributing path.” Profiles use physical axes with explicit vertical exaggeration.

Stable named cases carry checked pair references and claims. Strong canopy effect requires baseline unweighted support above 0.02 and at most 0.2 retained; little effect requires at least 0.98 retained. Open ground requires support at least 0.99; ground-blocked requires zero plus a real sampled obstruction. The actual distance comparison uses one observer area. Its primary table contains only centroid distance and the assumed diagnostic. Fixed explanations are labeled Worked example A → B; the Your current selection panel derives numbers, map, profile, role, legend and reading sentence from one state.

The runner defaults to model-only, preserves explicit model-only use and prints the checked export command. Conflicting modes error before producers run. Retired render-only gives a migration error. Explicit legacy rendering defaults to ignored `work/legacy-demo/` and rejects documentation/tracked destinations. The README uses the verified replacement SVG. A standalone reference checker covers README and docs; browser checks cover both compatibility entry points under `/toolkit-viewshed/`.

## Numerical and lineage evidence

Every field of all **5,424 canonical pair records** is exactly equal to the PR A base, including role, states, identities and numbers. Committed scientific generation remains:

```text
558d7a9aedbe7681e170107d21d6e0449ac776d3be2f9c47eeb33c6eaade11c5
```

The original producing revision remains `69945d4d7f5bf3a6cf153d40fc2e55dbe6ba06ff`. Presentation/export revision is informational. Adding projected geometry, case metadata and revised SVG derivatives changes bundle identity from:

```text
e1f0e8e89fe3f23a4f0052865d3dbaa46b09a8b200b93c1a28242083df454155
```

to:

```text
cbef9b87bbe37337f229cfed23408a9582a6aec5c887ab0fc1808125030ae2b5
```

Two final presentation exports retained this identity (2.58 and 2.36 seconds locally). The checked derivative files total 5,159,466 bytes; compressed JSON/GeoJSON totals 396,550 bytes. Both browser engines measured 4,135,799 initial decoded bytes, within the 5 MiB budget. These are local measurements, not performance guarantees.

A separate native bounded run used checksum-validated retained real USGS elevation, ETH 2020 canopy windows and Natural Earth geometry. It used `VIEWSHED_TOOLKIT_ROOT` to select a dedicated data/output root, preserving canonical products and their generation. The exact supported operations were:

```bash
PYTHONPATH=src python scripts/run_san_juan_demo.py \
  --config configs/san_juan_demo.yaml --rebuild --model-only
PYTHONPATH=src python scripts/build_documentation_examples.py \
  --config configs/san_juan_demo.yaml --output <dedicated-root>/exported-bundle
```

The model rebuilt 84 land and 92 water source areas through production stages and successfully finalized paired products. All 26 documentation files checked before/after model-only execution were byte-identical. The separate export passed checks and all 5,424 pair records were exactly equal to the committed example. Its distinct new producer/byte lineage yielded generation `ab8312417949df0c805502cdd0956288dfef0e690940a44a3d8422eedb2efc8d` and bundle `c74a84a800b6a3cac860a7736e5111d8ffc42082839d77ca9bfc9e08bb9d2a2c`; these isolated artifacts were not substituted into the committed example. This separates a genuine rebuild from presentation-only regeneration.

## Executed checks

| Evidence | Commands and result |
|---|---|
| Local native regressions | Initial red run: 9 failures. Final focused documentation/routing/geometry tests: **76 passed**. Rehashed observer, endpoint, role, display population/bounds and case mismatches reject. |
| Local full package suites | Python 3.11.16: **435 passed** in 158.90 s. Python 3.14.6: **435 passed** in 158.51 s. No tests were skipped. |
| Local quality | `ruff check src tests` passed. `python scripts/check_components.py`: expanded Ruff, Black **42 files**, strict mypy **24 source files**, passed on both Python runtimes. `python -m compileall -q src` passed. |
| Local installed wheels | `python scripts/check_installed_wheel.py` passed on 3.11.16 and 3.14.6, including package/resources imported outside the checkout and installed CLI help. Dependencies reused system site packages; the toolkit itself came from its built wheel. |
| Offline documentation | `python -S scripts/build_documentation_examples.py --check --output docs/assets/examples/san-juan`, `python -S scripts/check_documentation_links.py`, `node --check docs/assets/examples/explorer.js`, `python -m mkdocs build --strict`: passed. All **113** README/doc local references resolved, including this report link. |
| Automated browser execution | `python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa`: both passed, no JavaScript errors. Seven lessons, named cases, exact profile/pair/observer/role/endpoint correspondence, input layer/viewport parity, forward/inverse parity, separate water samples with no land profile, six factors, zero/tiny-positive/neutral/not-applicable/noncandidate states, reset, keyboard/touch, light/dark, mobile, CSS 200% reflow, missing bundle, no-JavaScript reading, blocked third-party requests, navigation back and compatibility base paths. Five rehashed semantic mutations rejected in each engine. |
| Source/structural inspection | Existing producer ownership, query filtering and numerical composition remain unchanged. Local code-only graph refreshed; graph/cache files stay ignored. `git diff --check` passed. |
| Remote evidence | Final-head Actions results are linked on PR #12. A preview deployment skipped by the PR event is not a scientific gate pass or a deployed site. |

## Screenshots actually inspected

Inspected Chromium and WebKit desktop inputs and canopy lessons in both themes, plus mobile maps and the linked profiles. Evidence files include `{engine}-desktop-{theme}-{inputs,canopy}.png`, `{engine}-mobile-{theme}-{inputs,canopy}-map.png` and `{engine}-mobile-{theme}-profile.png`. These files and `browser-receipt.json` are emitted by the checker and uploaded as the `documentation-browser-evidence` CI artifact; they are not committed as another dataset.

Visual inspection found overlays painted below result cells, colliding long endpoint labels, and a dark-theme ray with insufficient contrast. The final rendering puts actual support above the result fill, uses linked P/Q labels, keeps A/B visible, and provides distinct profile colors in both themes. Main reading text and plots reflow without horizontal scrolling; labeled data tables can scroll. This is agent inspection of actual rendered screenshots, not a human novice study or a manual browser-zoom test.

## Limits and reader checklist

Mapped canopy missingness remains **9.69% of buffered mapped land input pixels**, using zero-height fallback; this is not an affected-pair fraction or evidence of treelessness. The model remains coarse 100 m terrain with generalized shoreline and recorded source vintages, clearance, sampling and opaque-land water assumptions. Profiles remain explanatory samples, not engine traces or all contributing paths. Scores are static physical support, not sighting probabilities, actual observer counts, animal occurrence or public access.

Sampling/resolution/clearance sensitivity, regional expansion, independent fresh provider qualification, human novice validation, empirical field validation and manual browser zoom qualification remain unperformed. No merge, Pages deployment or package publication occurred. The integrated revision must pass final checks again when #11 is merged and #12 is retargeted to main.

Manual reader checklist: identify inputs; distinguish a sampled path from an area result; explain the distance assumption, terrain and tree-height comparison; read one role/source/target pair in either query direction; distinguish zero, missing and no candidate; avoid probability and public-access conclusions.
