# Producer lineage and export integrity validation

Executed October 3, 2026 in an isolated worktree based on main `0c0c33b60ab34f6e22d87ff235538d82112f2589`.
The original six-PR stack and Pages workflow were already merged. Live main runs
[package/science 37158597891](https://github.com/MarineCast/toolkit-viewshed/actions/runs/37158597891)
and [Pages 37158597893](https://github.com/MarineCast/toolkit-viewshed/actions/runs/37158597893)
were successful; the historical unused-ignore failure was already repaired with a typed PyArrow
boundary. This follow-up keeps strict mypy and adds `fail-fast: false`.

## Reproductions and ownership

The original overwrite reproduction produced seven failures while reuse rejected those same
mutations. Four scientific manifest relabeling cases were also accepted before the fix.
Land/water aggregate report-failure reproductions both replaced previous outputs before failing.
These now reject or roll back. Producer receipts belong to successful terrain/clear-sky,
distance, paired canopy and neutral water-vegetation execution. Finalization, geometry and
aggregate publication validate producer evidence before promotion. Pure lazy joins remain
calculation helpers. Paired, standalone and aggregate publication preserve previous files and
sidecars on exercised validation/promotion failures; no concurrent-reader snapshot is promised.

Receipts: `viewshed_factor_producer_v1`, `viewshed_output_set_v3`, and
`viewshed_single_role_v1`; public bundle: `san_juan_lessons_v2`. Historical working factors without
trusted producer receipts require rebuilding their producing stages. Valid retained durable
receipts still support read-only reuse after permitted cleanup. Missing receipts are never
backfilled by the finalizer. Scope excludes presentation settings and keeps centroid distance
independent of observer heights and rasters. Source-role sampling, grids, complete-water
populations and the matched bare/canopy design remain explicit.

The expensive lookup-hash regression counts five hashes for five metadata calls before a stage
snapshot and one with a shared snapshot. A fresh run detects changed contents; a mutation during
execution is rejected before publication. This is a call-count measurement, not a regional
speed measurement.

## Real bounded rebuild and identity

The actual production runner rebuilt the bounded San Juan model from retained checksum-validated
USGS elevation, ETH 2020 canopy windows and Natural Earth geometry. No synthetic public data or
edited weights were substituted. Producer revision: `69945d4d7f5bf3a6cf153d40fc2e55dbe6ba06ff`.

| Identity | Before | After |
| --- | --- | --- |
| Scientific generation | `4cbc6a9a15817c26594458caaf151f3d92011c981c69464e9d48ea71c3b3ec85` | `558d7a9aedbe7681e170107d21d6e0449ac776d3be2f9c47eeb33c6eaade11c5` |
| Bundle | `3ced605314445f44d0f42fdf270d1ed6a542ba8aafe60256e4e5d9cf963c93c3` | `e1f0e8e89fe3f23a4f0052865d3dbaa46b09a8b200b93c1a28242083df454155` |
| Scientific config | `92295ba2c418fb8c` | `92295ba2c418fb8c` |

All 5,424 columnar pair rows, IDs, states and numeric values are **exactly equal** to the prior
committed export. Rebuilt Parquet byte identities changed; the generation identity binds those
working artifact hashes, so it changed despite numerical parity. Repeating the export yielded
exactly the same final bundle ID. The final bundle contains 337,584 compressed JSON bytes and
3,970,262 total bytes. Export executions measured 2.31 and 2.33 seconds locally.

Bundle identity seals finite, sorted, compact UTF-8 JSON plus a trailing newline, preserved as
`identity_json`, covering semantic fields and payload checksums. Retained producing evidence
checks labels independently of copied display text. Original producer revision is separate from
informational export/documentation revision. Resolution, CRS, clearance, vintage and curve
contradictions fail even when the manifest envelope is rehashed. Integrity is not source
authenticity: an unsigned bundle cannot authenticate someone replacing all evidence and hashes.
Near/far ordering, matched canopy thresholds (strong retention ≤0.20; little effect ≥0.98, with
bare support >0.02), common inverse target and linked aggregate/profile obstruction evidence
are tested. Shorter-cutoff logistic, exponential and piecewise diagnostic evaluations agree with
the production evaluator. The attributed exporter explicitly rejects unsupported provider presets.

## Local execution

Native environments: Python 3.11.16 and 3.14.6 with working GDAL/Rasterio. Commands below were
executed from the worktree; runtime paths and logs remain local intermediates.

```bash
PYTHONPATH=src python -m pytest -q
ruff check src tests
python scripts/check_components.py
python -m compileall -q src
python scripts/check_installed_wheel.py
PYTHONPATH=src python scripts/run_san_juan_demo.py --config configs/san_juan_demo.yaml --rebuild --model-only
PYTHONPATH=src python scripts/build_documentation_examples.py --config configs/san_juan_demo.yaml --output docs/assets/examples/san-juan
python -S scripts/build_documentation_examples.py --check --output docs/assets/examples/san-juan
node --check docs/assets/examples/explorer.js
python -m mkdocs build --strict
python scripts/check_documentation_browser.py --engines chromium webkit --output work/browser-qa
```

- Full suites: 395 passed, no skips, on each Python (135.56 s / 138.83 s). Three subsequent
  provider-preset guards were added and exercised with the full focused bundle suite.
- Expanded Ruff, Black (36 scoped files), strict mypy (24 source files), byte compilation and
  installed-wheel probes passed. Wheels were installed outside the checkout; toolkit imports,
  packaged resources and CLI help resolved inside the isolated installed environments.
- Architecture/component/case-study subset: 36 passed. Publication plus bundle regressions:
  38 passed before the last three pure provider guards.
- Strict documentation-only build and Node syntax check passed.
- Chromium and WebKit passed all existing lifecycle, role, pair, factor, keyboard, mobile,
  light/dark, CSS 200% reflow, no-JavaScript and third-party-blocking checks. Each also rejected
  five rehashed scientific metadata mutations and retained all seven static figures. No page
  errors. Automated CSS zoom is not a manual browser-zoom study.
- Code-only repository graph refreshed locally; it is ignored and not published.

Remote follow-up CI is separate evidence and is recorded on the pull request for its exact head.
The successful main runs above do not certify these changes. This report does not claim merge,
deployment or package publication.

## Remaining limits

PR B addresses metric map alignment, linked sample/profile interaction and legacy runner defaults.
Sampling/resolution/clearance sensitivity, a larger regional run, human novice testing and field
validation remain unperformed. The 9.69% missing-canopy fraction concerns mapped land input pixels,
not the fraction of affected pairs. Scores are static modeled support, not detection probability,
observer presence, animal presence, access or sightings.
