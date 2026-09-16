---
name: validation
description: Select and run viewshed checks for behavior, API, import, component, provider, case-study or quality-gate changes; not routine prose edits.
---

# validation

All paths and commands below are relative to the toolkit-viewshed checkout root.
Source/tests, explicit contracts and architecture remain authoritative.

## Safe commands

Run the smallest relevant test first, then broaden validation in proportion to the change.

```bash
PYTHONPATH=src python -m pytest -q tests/path/to/test_file.py
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
git diff --check
```

For changes to the explicit component workflow, providers, case-study code, or the incremental
quality-gate scope, also run the focused tests and component quality gate required by CI:

```bash
PYTHONPATH=src python -m pytest -q tests/pipeline/test_component_workflow.py
PYTHONPATH=src python -m pytest -q tests/pipeline/test_case_study.py
python scripts/check_components.py
```

`scripts/check_components.py` applies expanded Ruff rules, Black checks, and strict mypy to its
defined component-code scope. It is not required for a prose-only edit that does not change the
script or its quality-gate instructions.

Format changed Python files with Black at the repository's 100-character line length. Do not use a
whole-tree Black failure as evidence that your change is bad: the repository documentation records
pre-existing full-tree formatting debt. Check the files you touched.

CLI inspection is safe and must not start a regional run:

```bash
PYTHONPATH=src python -m viewshed_toolkit --help
PYTHONPATH=src python -m viewshed_toolkit <command> --help
```

For real acquisition or regional execution, load the regional-run skill before proceeding.

Some GDAL integration tests skip when the required CLI or Python bindings are unavailable. Report
skips and environment limitations; do not present a skipped integration path as validated.

## Testing expectations

Every behavioral fix should include a regression test. Prefer small synthetic geometries, rasters,
and pair tables under `tmp_path` over regional fixtures.

Choose focused coverage by change type:

- Imports or module moves: `tests/pipeline/test_architecture.py`.
- Configuration or paths: `test_config_contract.py` and `test_canonical_viewshed_config.py`.
- Artifact schemas/finalization: `test_static_pair_universe.py`,
  `test_terrain_factor_composition.py`, and related schema tests.
- Cleanup/promotion: `test_cleanup_safety.py` and rollback tests.
- Raster caching/alignment: canonical-stack, fingerprint, clip-bounds, and canopy tests.
- Sampling/batching/LOS: source-sampling, batch-scheduling, aggregation-accuracy, and GDAL tests.
- Maps: `test_static_maps.py` and `test_map_artifacts.py`.
- Public facade or orchestration: `tests/test_viewshed.py` and
  `tests/pipeline/test_service_stage_contract.py`.

For numerical changes, test invariants and edge cases as well as a nominal value: empty inputs,
nodata, zero support, partial coverage, duplicates, invalid ranges, CRS/grid mismatch, and
determinism under reordered input.

## Documentation and validation claims

- Update `README.md`, `docs/api.md`, `docs/pipelines.md`, or the methodology guides when
  user-facing behavior, public imports, CLI stages, workflow effects, or scientific interpretation
  changes.
- Treat the OrcaCast resources linked from `docs/reports/orcacast-history.md` and
  `docs/reports/viewshed_migration_validation.json` as historical evidence, not a current run.
- Do not hand-edit generated validation snapshots to imply that validation was rerun.
- Keep source-data citations, access dates, licenses, processing, and CRS documentation with any
  committed example data.
- State validation boundaries precisely in the final handoff: tests run, skips, unrun pipeline or
  notebooks, required data that was absent, and artifacts inspected. Never claim a regional rebuild
  from unit tests or a preserved artifact snapshot.

## Completion checklist

Before handing off a change:

1. Review `git status` and `git diff`; confirm unrelated changes were preserved.
2. Run focused tests for the changed contract, then the full suite when practical.
3. For component, provider, case-study, or quality-gate changes, run both focused component and
   case-study test files and `python scripts/check_components.py`.
4. Run Ruff, byte-compilation, Black on changed Python files, and `git diff --check` as applicable.
5. Verify generated or ignored outputs were not accidentally staged.
6. Summarize changed behavior, validation performed, skips or unrun checks, and any data or GDAL
   limitations.
