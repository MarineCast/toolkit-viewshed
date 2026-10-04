# Reliability and onboarding improvements

Implementation date: **2026-10-04**. This is unreleased work on top of the documentation stack
reviewed at `27f8450`; it does not claim publication, deployment or empirical qualification.

## Changes

- Public `process` checks manifest identity and path containment before side effects. Complete
  resume uses the same retained-generation validator as finalization; partial manifests record
  local input identity and reject changed inputs or historical evidence without that inventory.
- Unsupported distance partial-run flags are rejected. Water component builds omit raster
  acquisition. Land dependency and numerical contracts are unchanged.
- Canopy filtering transforms its bbox to source CRS. New acquisition manifests distinguish raw
  byte SHA-256 from filename-inclusive artifact identity, with explicit compatibility notes.
- Removed the audit's 12 unreferenced definitions, the indirect obsolete checksum helper, and
  two unused parameters. Reachable legacy vegetation code remains, with clearer CLI labeling.
- Added the synthetic offline distance example, read-only `doctor`, artifact dictionary,
  troubleshooting, dataset card, contribution guidance, citation metadata and acceptance protocol.
- Expanded strict quality gates to service, dependency planning, preflight and CLI adapters.
  CI and Pages share one documentation quality workflow; Pages uploads only after browser checks.

## Evidence and limits

The first-result command completed with **73 candidate pairs**, validated both distance products,
and checked the exponential formula including the hard cutoff. `doctor --workflow distance`
returned valid without acquisition or model execution. These are synthetic geometry outputs.

The installed-wheel probe passed on Python 3.14.6, importing packaged resources and CLI outside
the source checkout. It reused native/scientific dependencies. A fresh environment solve, hosted
GitHub Actions execution, deployment and package release were not performed.

Final validation on the implementation working tree:

| Check | Result |
| --- | --- |
| Full native pytest suite | 451 passed, 2 warnings, 146.11 seconds; Python 3.14.6 on macOS |
| Architecture constraints | 8 tests passed, also included in the full suite |
| Expanded quality gate | Ruff passed; Black passed for 48 gate files; strict mypy passed for 30 source files |
| Changed Python formatting | Black checked 26 changed/new Python files |
| General Ruff, compileall, JS syntax, diff whitespace | Passed |
| Offline bundle integrity | Passed; 5,424 committed pairs; unchanged bundle identity |
| Local documentation references | 203 checked; passed |
| MkDocs strict build | Passed |
| Browser checks | Chromium and WebKit passed, no page errors; automated mobile/theme/keyboard/touch/fallback checks |
| Workflow configuration | YAML parsed; reusable documentation QA wired to both CI and Pages; hosted Actions not executed |

The review's historical coverage percentage is not presented as a new coverage measurement.
A temporary link failure during documentation assembly was repaired, and the entire suite was
rerun successfully. No generated model products or raw data were added to Git.

No live acquisition, San Juan/regional rebuild, sensitivity matrix, human reader study or field
comparison was performed. Existing GDAL exception-policy and Polars GeoArrow warnings remain;
neither is evidence of changed model values. Large numerical-module refactoring and full legacy
configuration-schema migration remain separate work, not silently bundled into these fixes.
