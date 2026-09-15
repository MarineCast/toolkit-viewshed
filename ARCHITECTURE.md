# Viewshed Toolkit Architecture

This document is a working map for maintainers and coding agents. Read it together with
`AGENTS.md`, which contains the repository's authoritative contribution and safety rules. For the
short narrative of the component refactor, see `docs/architecture.md`; for scientific semantics,
see `docs/methodology.md` and `docs/scientific-methodology.md`.

## System purpose and boundary

Viewshed Toolkit is a Python 3.11+ package for reproducible terrain, canopy, distance, and
source-to-target visibility modeling. Its durable analytical grain is exactly one unique
`source_h3 × target_h3` pair.

The package models **static physical viewability**. It does not model observer activity, public
access, reporting capture, animal detection probability, animal presence, or ecology. Distance is
an independently inspectable factor even when it contributes to a composed result. Missing,
unavailable, not-applicable, and observed zero are distinct states and must remain distinct in
schemas and calculations.

## Execution architecture

The primary dependency direction is:

```text
CLI presentation
    ↓
typed Python API
    ↓
canonical stage registry / orchestration
    ↓
prepare → weights → finalize → visualization
    ↓
contracts, configuration, and private infrastructure
```

The command-line layer parses arguments and presents results. It must not own scientific logic.
The typed API is independent of `argparse` and the CLI. The stage registry is the single source of
truth for stage names and ordering.

There are two workflow surfaces:

1. The established production workflow is described by `STAGE_SPECS` in
   `src/viewshed_toolkit/pipeline/api/registry.py` and is run through `run_stage`, `run_viewshed`, or
   `process`.
2. The supplemental component workflow is described by `COMPONENT_DEPENDENCIES` in the same
   registry. `run_component_stage` executes one stage, while `run_components` resolves and runs a
   deterministic dependency closure.

Do not add a stage in a CLI module or duplicate the dependency graph elsewhere. A stage change
usually requires synchronized API, CLI, documentation, and stage-contract test updates.

## Source ownership

| Path | Owns | Must not own |
|---|---|---|
| `src/viewshed_toolkit/__init__.py` | Small supported public facade | Convenience re-exports of internal modules |
| `pipeline/api/` | Typed orchestration, stage dispatch, canonical registry | Argument parsing or terminal formatting |
| `pipeline/cli/` | Argument parsing and presentation adapters | Scientific calculations or workflow policy |
| `pipeline/config/` | Strict configuration, paths, runtime initialization | Artifact-specific schemas or late-stage logic |
| `pipeline/contracts/` | Pair keys, schemas, distance-product paths, cleanup policy, provenance | Scientific implementations |
| `pipeline/providers/` | Discovery and acquisition of source datasets | Mosaicking, reprojection, LOS, or composition |
| `pipeline/prepare/area/` | Area geometry, H3 roles, sampling, pair universe | Weight calculation |
| `pipeline/prepare/elevation/` | DEM preparation and raster-stack support | Vegetation weighting |
| `pipeline/prepare/vegetation/` | Canopy preparation | Terrain weighting |
| `pipeline/prepare/datasets.py` | Mosaic, clip, reproject, resample, and dataset validation | Provider acquisition or final composition |
| `pipeline/weights/distance/` | Pair-distance promotion, reusable profiles, configured decay | Terrain or canopy LOS |
| `pipeline/weights/terrain/` | Terrain LOS and aggregation | Distance-policy ownership |
| `pipeline/weights/vegetation/` | Vegetation/canopy path weighting | Pair finalization |
| `pipeline/weights/components.py` | Durable component-weight orchestration | Shared schema ownership |
| `pipeline/finalize/` | Exact joins, composition, validation, promotion, cleanup | Early preparation |
| `pipeline/visualization/` | Static and interactive map exports | Canonical scientific calculations |
| `src/viewshed_toolkit/_internal/` | Private generic persistence and geospatial infrastructure | Imports from `viewshed_toolkit.pipeline` |
| `src/viewshed_toolkit/resources/` | YAML files distributed with the wheel | Workstation-specific paths |
| `analysis/salish_sea/` | Expanded regional case-study README and HTML report | Reusable package API |

The older OrcaCast analysis subtree is not present in the current tree. Its notebooks, contracts,
audit, and artifact manifest are preserved only in the pinned revision documented in
`docs/reports/orcacast-history.md`; do not substitute `analysis/salish_sea/` for those resources.

The enforced import constraints are:

- `_internal` does not import `viewshed_toolkit.pipeline`.
- `prepare` does not import `weights`.
- `config`, `prepare`, and `diagnostics` do not import `finalize`.
- The package import graph stays acyclic.
- The package root contains only `__init__.py` and `__main__.py`; removed pass-through modules and
  compatibility aliases stay removed.

`tests/pipeline/test_architecture.py` is the executable specification for these constraints.

## Core data contracts

### Pair grain

Every pair artifact must have non-null, unique `source_h3` and `target_h3` keys at its expected
boundary. Validate join cardinality explicitly. Never make duplicates disappear with row-order
assumptions or `keep="first"` when the contract requires one row per pair.

Shared pair constants and schemas belong in `pipeline/contracts/pairs.py`. Artifact paths,
metadata, fingerprints, and atomic component writes belong in the existing modules under
`pipeline/contracts/`; do not redefine them in a calculation or finalization module.

### Scientific factors

- Static weights are finite and bounded to `[0, 1]` where their metric contract defines that
  range.
- Distance remains independently persisted and inspectable.
- Standalone distance products branch from the canonical lookup, remain raster-free, and use
  dependency-specific identity; custom profiles never become static-composition inputs.
- Canopy obstruction uses absolute `DEM + CHM` elevations and the configured observer-grounding
  policy.
- The conditional canopy factor depends on the matching DEM kernel as its denominator, so canopy
  weighting depends on DEM weighting even though CHM acquisition and preparation are independent.
- Land and water have different source policies. Production land terrain uses the paired
  bare-earth/canopy workflow; water uses its opaque-land-mask, single-surface path.
- Deterministic sampling, batching, ordering, and serialization are part of reproducibility. Any
  necessary randomness must expose and record a stable seed.

### Configuration and provenance

`load_app_config()` is read-only. Directory creation and metadata setup begin at runtime
initialization, not configuration loading. Resolve paths through `pipeline/config/paths.py`; do not
construct canonical paths with string concatenation.

The checkout configuration at `configs/salish_sea.yaml` and the packaged copy under
`src/viewshed_toolkit/resources/` must remain in parity where the tests require it. Configuration
and scientific hashes are cache contracts: any change capable of changing a scientific result
must invalidate reuse through the appropriate hash or metadata field.

Durable artifacts and their metadata sidecars form one contract. Keep schema versions, source
fingerprints, checksums, configuration hashes, scientific hashes, and filenames synchronized.
Reuse a cache only after all relevant inputs, grids, schemas, and fingerprints match.

## Artifact lifecycle and safety

Use the existing atomic Parquet and raster writers so partially written products do not become
durable artifacts. Paired land/water promotion must remain rollback-safe: failure cannot leave one
new artifact paired with one stale artifact.

Cleanup roots must stay contained within the configured viewshed output directory. Cleanup may run
only after durable outputs and their schema/metadata are validated, and it must preserve final
artifacts and supported sidecars. Never broaden a cleanup root to the repository root or current
working directory. Prefer a new run ID, or explicit force/resume semantics, over overwriting a
manifest for a different logical run.

Generated rasters, Parquet files, maps, and local datasets under `data/` or `outputs/` are normally
ignored and untracked. Do not add them without an explicit redistribution and licensing review.

## How to route a change

Before editing, identify the contract being changed and start with its owner:

- Public import: package `__init__.py`, API documentation, and public-facade tests.
- Stage or dependency: `pipeline/api/registry.py`, then API service, CLI exposure, docs, and stage
  contract tests.
- Configuration field: the appropriate `pipeline/config/` model, strict validation, both canonical
  YAML copies when applicable, path/hash behavior, and configuration tests.
- Pair column or schema: `pipeline/contracts/`, all readers/writers, provenance metadata, and
  finalization tests.
- Numerical method: the relevant `weights/` owner, with invariant and edge-case tests.
- Data acquisition: `providers/`; keep preparation in `prepare/`.
- Promotion or cleanup: `finalize/` plus rollback and containment tests.
- Map output: `visualization/` plus map artifact tests.

Prefer a small synthetic regression fixture using `tmp_path`. For numerical work, cover nominal
behavior and relevant boundary states: empty input, nodata, zero support, partial coverage,
duplicates, invalid ranges, CRS or grid mismatch, and deterministic behavior after input reorder.

## Validation ladder

Run the smallest relevant check first and broaden in proportion to risk:

```bash
PYTHONPATH=src python -m pytest -q tests/path/to/test_file.py
PYTHONPATH=src python -m pytest -q tests/pipeline/test_architecture.py
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
git diff --check
```

Component workflow, provider, case-study, and quality-gate changes also require focused component
validation and the incremental quality gate used by CI:

```bash
PYTHONPATH=src python -m pytest -q tests/pipeline/test_component_workflow.py
PYTHONPATH=src python -m pytest -q tests/pipeline/test_case_study.py
python scripts/check_components.py
```

The script runs expanded Ruff rules, Black checks, and strict mypy over the component-code scope it
defines. Do not run this component-specific route automatically for prose-only changes unless the
prose changes the quality-gate instructions themselves.

Format only changed Python files with Black using the repository's 100-character line length.
Full-tree formatting debt may be pre-existing and is not evidence about a focused change.

CLI help is a safe inspection path:

```bash
PYTHONPATH=src python -m viewshed_toolkit --help
PYTHONPATH=src python -m viewshed_toolkit <command> --help
```

Do not casually run pipeline stages, acquisition commands, notebooks, or an end-to-end regional
workflow as smoke tests. They may download data, write large artifacts, clean intermediates, or
consume substantial compute. If such a run is required, first confirm its inputs, use a dedicated
output directory, and record exactly what ran. GDAL integration skips are environment limitations,
not successful validation of that path.

## Agent handoff checklist

Before handing off a change:

1. Review `git status` and the complete diff; preserve unrelated user changes.
2. Confirm the implementation lives in the established owner and respects import direction.
3. Confirm pair grain, state semantics, provenance, cache invalidation, and cleanup safety wherever
   relevant.
4. Run focused tests, then broader checks appropriate to the risk.
5. For component, provider, case-study, or quality-gate changes, run the component-specific CI
   route above.
6. Verify ignored/generated products were not accidentally staged.
7. Report tests run, skips, missing data or GDAL limitations, and any regional workflows or
   notebooks intentionally not run.
