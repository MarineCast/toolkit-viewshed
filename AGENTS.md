# AGENTS.md

## Scope

These instructions apply to the entire repository. Preserve unrelated working-tree changes and
keep edits limited to the task at hand. Check for a more specific `AGENTS.md` before working in a
subdirectory; a deeper file takes precedence for that subtree.

## Repository purpose

Viewshed Toolkit is a Python 3.11+ package for reproducible terrain, vegetation, distance, and
source-to-target visibility modeling. The durable analytical grain is one unique
`source_h3 × target_h3` pair.

Keep the scientific boundary explicit:

- Static viewability describes physical viewing support.
- It is not observer activity, public access, reporting capture, animal detection probability,
  animal presence, or ecology.
- Distance is a separately inspectable factor even when it contributes to a composed output.
- Missing, unavailable, not-applicable, and observed zero are different states. Never silently
  coerce one into another.

## Repository map

- `src/viewshed_toolkit/__init__.py`: supported public Python facade.
- `src/viewshed_toolkit/__main__.py`: `python -m viewshed_toolkit` entry point.
- `src/viewshed_toolkit/pipeline/api/`: typed orchestration and the canonical stage registry.
- `src/viewshed_toolkit/pipeline/cli/`: argument parsing and terminal presentation only.
- `src/viewshed_toolkit/pipeline/config/`: configuration, path, and runtime initialization.
- `src/viewshed_toolkit/pipeline/contracts/`: schemas, artifact paths, cleanup rules, pair keys, and
  provenance identity.
- `src/viewshed_toolkit/pipeline/prepare/`: area, elevation, and vegetation preparation.
- `src/viewshed_toolkit/pipeline/weights/`: distance, terrain, canopy, and vegetation calculations.
- `src/viewshed_toolkit/pipeline/finalize/`: durable pair-kernel materialization and validation.
- `src/viewshed_toolkit/pipeline/visualization/`: static and interactive map exports.
- `src/viewshed_toolkit/_internal/`: private shared persistence and geospatial infrastructure.
- `src/viewshed_toolkit/resources/`: YAML resources shipped in the wheel.
- `configs/salish_sea.yaml`: editable checkout copy of the canonical case-study configuration.
- `analysis/case_studies/orcacast/`: application-specific notebooks, contracts, history, and
  artifact manifest; this is not part of the reusable package API.
- `tests/`: API, architecture, numerical, artifact, and cleanup regression tests.
- `data/` and `outputs/`: ignored local inputs and generated products.

Do not edit ignored `__pycache__`, `.pytest_cache`, or `*.egg-info` contents.

## Environment and installation

Use an isolated environment with Python 3.11 or newer. GDAL and Rasterio must be binary-compatible
for real geospatial runs.

```bash
python -m pip install -e ".[dev,analysis]"
```

Install `.[acquisition]` only when source-download work needs it. Do not assume that a checkout has
the regional DEM, canopy raster, land/water polygons, H3 support products, or OrcaCast datasets.

Relative data and output paths resolve from the project root. Use `VIEWSHED_TOOLKIT_ROOT` when a
different root is intentional. Do not encode workstation-specific absolute paths in source,
configuration, metadata, notebooks, or documentation.

## Safe commands

Run the smallest relevant test first, then broaden validation in proportion to the change.

```bash
PYTHONPATH=src python -m pytest -q tests/path/to/test_file.py
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
git diff --check
```

Format changed Python files with Black at the repository's 100-character line length. Do not use a
whole-tree Black failure as evidence that your change is bad: the repository documentation records
pre-existing full-tree formatting debt. Check the files you touched.

CLI inspection is safe and must not start a regional run:

```bash
PYTHONPATH=src python -m viewshed_toolkit --help
PYTHONPATH=src python -m viewshed_toolkit <command> --help
```

Do not run a pipeline stage, acquisition command, notebook, or complete workflow merely to smoke
test an unrelated change. Those operations can download data, write large artifacts, consume
substantial compute, or clean intermediate outputs. If a regional or notebook run is required,
confirm inputs are present, use a dedicated output directory, and report exactly what was run.

Some GDAL integration tests skip when the required CLI or Python bindings are unavailable. Report
skips and environment limitations; do not present a skipped integration path as validated.

## Architecture and API rules

- Keep `viewshed_toolkit` package-root exports intentional and minimal. A new supported public API
  requires updating `src/viewshed_toolkit/__init__.py`, documentation, and tests.
- Advanced imports should use the canonical module under `viewshed_toolkit.pipeline`.
- Do not recreate removed top-level pass-through modules or compatibility aliases.
- Keep CLI modules as thin presentation adapters over typed, argparse-free API functions.
- `_internal` must not import `viewshed_toolkit.pipeline`.
- `prepare` must not depend on `weights`, and `config`, `prepare`, and `diagnostics` must not depend
  on `finalize`.
- Keep the internal import graph acyclic. Run `tests/pipeline/test_architecture.py` after changing
  module ownership or imports.
- Add or reorder canonical stages only through `pipeline/api/registry.py`; keep the service, CLI,
  public constants, documentation, and stage-contract tests aligned.
- Put shared schemas, path names, provenance, pair-key constants, and cleanup policy in their
  existing owners under `pipeline/contracts/`, not in late-stage implementation modules.

## Configuration and path contracts

- `load_app_config()` is read-only: loading configuration must not create directories or write
  metadata. Runtime initialization is the boundary for output-directory and metadata setup.
- Resolve paths through the existing config/path helpers. Do not infer canonical artifact paths
  with ad hoc string concatenation.
- Preserve parity between `configs/salish_sea.yaml` and the packaged
  `src/viewshed_toolkit/resources/salish_sea.yaml` where tests require it.
- Treat configuration and scientific hashes as cache/provenance contracts. A change that affects a
  scientific result must invalidate reuse through the appropriate hash or metadata fields.
- Reject unknown configuration fields and invalid enum values rather than ignoring them.

## Data and artifact contracts

- Enforce non-null, unique `source_h3` and `target_h3` keys at the expected pair grain before joins
  and writes.
- Use explicit join-cardinality validation. Do not rely on row order or `keep="first"` to hide an
  upstream uniqueness violation when a table is expected to be one row per pair.
- Validate required columns, units, ranges, CRS, raster alignment, H3 resolution, and source type at
  module boundaries. Static weights must remain finite and bounded to `[0, 1]`; apply bounds only
  where the metric contract defines them.
- Preserve explicit state and provenance columns when values are unavailable or not applicable.
  Never fill unknown support with zero merely to make an aggregation complete.
- Keep land and water source policies distinct. In particular, production land terrain uses the
  paired bare-earth/canopy workflow; do not route it through the water-only single-surface path.
- Preserve deterministic sampling, batching, ordering, and serialization. If randomness is
  necessary, expose and record a stable seed.
- Prefer the existing atomic Parquet/raster writers. Durable artifacts should not become visible in
  a partial state.
- Keep metadata sidecars, schema versions, source fingerprints, checksums, configuration hashes,
  scientific hashes, and output filenames synchronized with the artifact they describe.
- Cache reuse is valid only when relevant inputs, grids, schemas, configuration, and fingerprints
  match. Reject stale or ambiguous caches instead of silently accepting them.
- Generated Parquet, raster, map, and local data products remain untracked unless the task includes
  a redistribution and licensing review.

## Cleanup and overwrite safety

Cleanup and overwrite behavior is part of the public data contract.

- Never broaden a cleanup root or bypass containment checks.
- Never clean the repository root, current working directory, or a path outside the configured
  viewshed output directory.
- Require durable outputs and their schema/metadata validation before deleting intermediates.
- Preserve final artifacts and all supported metadata sidecar names.
- Keep paired land/water promotion rollback-safe: a failure must not leave one new artifact paired
  with one stale artifact.
- Use a new run ID or explicit force/resume behavior rather than overwriting a manifest for a
  different logical run.
- Add or update cleanup and rollback tests whenever deletion, promotion, manifest, or overwrite
  behavior changes.

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

- Update `README.md`, `docs/api.md`, or `docs/methodology.md` when user-facing behavior, public
  imports, CLI stages, or scientific interpretation changes.
- Treat `analysis/case_studies/orcacast/history/` and
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
3. Run Ruff, byte-compilation, Black on changed Python files, and `git diff --check` as applicable.
4. Verify generated or ignored outputs were not accidentally staged.
5. Summarize changed behavior, validation performed, skips or unrun checks, and any data or GDAL
   limitations.
