# AGENTS.md

## Scope

These instructions apply to the entire repository. Preserve unrelated working-tree changes and
keep edits limited to the task at hand. Check for a more specific `AGENTS.md` before working in a
subdirectory; a deeper file takes precedence for that subtree.

Do not edit ignored `__pycache__`, `.pytest_cache`, or `*.egg-info` contents.

## Shared MarineCast context

Before changing repository boundaries, dependencies, shared schemas, provenance, or application
integration, read the MarineCast [infrastructure guide](https://github.com/MarineCast/.github/blob/HEAD/INFRASTRUCTURE.md).
In the multi-repository workspace, the local copy is `../../.github/INFRASTRUCTURE.md`.
Prefer that local copy when present; in an independent checkout, read the linked document. If it
cannot be retrieved, report that limitation and use the local contracts below; do not invent a
shared standard. These instructions explicitly request that reading; a sibling repository's
`AGENTS.md` is not automatically inherited.

The infrastructure guide owns cross-repository context. This repository owns its implementation
and scientific contracts. Surface conflicts before changing an interface; do not silently replace
an existing local contract with a proposed ecosystem convention.

## Required reading and task routing

Use each guide for its designated contract instead of copying its detail here:

| Document | Primary responsibility |
|---|---|
| `AGENTS.md` | Required behavior, non-negotiable constraints, task routing, validation commands, and completion requirements |
| `ARCHITECTURE.md` | Canonical ownership map, dependency direction, and where changes belong |
| `docs/scientific-methodology.md` | Numerical definitions, assumptions, state semantics, and scientific invariants |
| `docs/api.md` and `docs/pipelines.md` | How to invoke each workflow and what it reads, writes, reuses, or deletes |
| `docs/reports/` | Clearly dated evidence; never undated instructions or proof of a current run |

**Before changing numerical behavior, read `docs/scientific-methodology.md`.** Distance attenuation
is already integrated into `weight_terrain`; do not multiply the H3-centroid distance diagnostic
into the static result. Preserve the distinction between physical viewability and observer
activity, access, detection, presence, or ecology. Missing, unavailable, not-applicable, and
observed zero are distinct states.

Distance products are independently executable. Changing a standalone distance profile must not
trigger terrain or canopy computation or alter their scientific configuration. Independently
computed H3-level averages are not generally interchangeable with joint observer/target-sample
averages. The current viewshed kernel already integrates distance attenuation; never multiply a
standalone centroid-distance weight into it again.

**Before changing orchestration, promotion, overwrite, or cleanup, identify the affected workflow.**
The established paired workflow (`run_viewshed` / `process`) and explicit component workflow
(`run_components` / `run_component_stage`) have different output, promotion, and cleanup
contracts. Read `docs/api.md` and `docs/pipelines.md` before changing either.

For module moves, new modules, shared contracts, or import changes, read `ARCHITECTURE.md` first.
For regional case-study work, use `analysis/salish_sea/`, `docs/salish-sea-case-study.md`, and the
canonical configuration. Historical OrcaCast locations are recorded only in
`docs/reports/orcacast-history.md`; do not recreate the removed subtree or substitute the current
Salish Sea study for it.

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

Do not run a pipeline stage, acquisition command, notebook, or complete workflow merely to smoke
test an unrelated change. Those operations can download data, write large artifacts, consume
substantial compute, or clean intermediate outputs. If a regional or notebook run is required,
confirm inputs are present, use a dedicated output directory, and report exactly what was run.

Some GDAL integration tests skip when the required CLI or Python bindings are unavailable. Report
skips and environment limitations; do not present a skipped integration path as validated.

## Architecture and API constraints

- Follow the ownership map and dependency direction in `ARCHITECTURE.md`; do not create a second
  ownership map here. Run `tests/pipeline/test_architecture.py` after changing ownership or imports.
- Keep package-root exports intentional and minimal. A supported public API change requires facade,
  documentation, and test updates; do not recreate removed pass-through modules or aliases.
- Keep CLI code as presentation over the typed API. Change canonical stage names or order only in
  `pipeline/api/registry.py`, then align the API, CLI, documentation, and contract tests.
- Put shared schemas, artifact paths, provenance, pair keys, and cleanup policy in their existing
  owners under `pipeline/contracts/`, not in calculation or finalization modules.

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

- Enforce non-null, unique `source_h3 × target_h3` keys **within each source role** before joins
  and writes. When combining land and water results, retain `source_type` as part of the row
  identity: a mixed coastal H3 cell can participate in both roles. A role-partitioned artifact may
  encode that identity in its path, but a cross-role table must carry it explicitly.
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

Do not transfer cleanup or promotion behavior between workflows. Successful complete execution in
the established paired workflow may clean intermediates and uses rollback-safe paired land/water
promotion. The explicit component workflow writes its separate component namespace, performs no
implicit cleanup, and uses per-role manifests rather than a combined atomic generation pointer.
For water builds, read the dependency caveat beside the examples in `docs/pipelines.md`; an
individual opaque-land stage can be raster-free even when a broader dependency-resolved build is
not.

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

## Codebase navigation

Use this repository's local Graphify graph for structural implementation, debugging, and
architecture questions before broad searches. Skip graph queries for obvious, single-file edits.
Narrow modules, symbols, callers and dependencies with `query`, `explain`, or `affected`, then
read the relevant source and tests. Read [ARCHITECTURE.md](ARCHITECTURE.md) for design intent.
Source and tests are authoritative; explicit schemas/contracts and architecture docs take
precedence over the graph. Static edges can miss dynamic dispatch or resolve names ambiguously;
fall back to targeted `rg` searches whenever coverage or freshness is insufficient.

Run from this checkout (Graphify CLI package `graphifyy==0.9.62`, Python 3.10+):

```bash
# Install once in an isolated developer environment: python -m pip install graphifyy==0.9.62
graphify extract . --code-only --no-cluster
graphify query "run_components" --budget 1500
graphify explain "run_components"
graphify affected "<symbol>" --relation calls --depth 2
```

Repeat the extraction command after structural edits: it incrementally detects changed files.
Use `--force` for a full rescan after checking intentional removals if shrink protection blocks
refresh. The graph and caches live in ignored `graphify-out/`; never commit them. `.gitignore`
and `.graphifyignore` both apply. This local AST-only setup does not semantically index prose or
produce clustered architecture reports; read docs/configuration directly when needed.

Codex uses this section plus the CLI; no skill or MCP registration is required. From a parent
workspace, change into this checkout before building; queries may instead use
`--graph <checkout>/graphify-out/graph.json`. Keep each repository's graph independent.
The upstream `graphify codex install` can add its own AGENTS section, but is unnecessary here.
Optional `graphify hook install` adds post-commit/post-checkout hooks **and** a merge driver with
`.gitattributes` changes; it is not enabled or recommended by default for these untracked graphs.
See the [upstream CLI reference](https://graphify.com/docs/cli) and `graphify --help` on upgrades.
