# Viewshed Toolkit agent guidance

## Scope

Independent Python package `viewshed_toolkit` (distribution `viewshed-toolkit`). Preserve unrelated
changes, inspect `git status --short` before edits and the diff afterward, and check deeper
`AGENTS.md` before editing a subtree. Do not edit ignored caches or egg-info.

## Shared MarineCast context

For boundary, dependency, shared schema/provenance, or application-integration changes, read
[INFRASTRUCTURE.md](https://github.com/MarineCast/.github/blob/HEAD/INFRASTRUCTURE.md).
In the grouped workspace use `../../.github/INFRASTRUCTURE.md`; in a flat clone layout use
`../.github/INFRASTRUCTURE.md`, resolving from this checkout. Otherwise use the linked copy;
if unavailable, report the limitation without inventing a shared standard. Sibling instructions
are not inherited. Local implementation contracts remain authoritative; surface conflicts.

## Always preserve

- Model static physical viewability separately from observer activity/access, reporting,
  detection, species presence and ecology. Preserve units, support, CRS and missingness;
  unknown, unavailable, not-applicable and observed zero are distinct.
- Terrain already integrates physical distance attenuation; do not multiply the centroid-distance
  diagnostic into the static result. Keep standalone distance products independent.
- Preserve unique non-null `source_h3 × target_h3` within each source role; combined roles also
  require `source_type`. Validate join cardinality, provenance, cache identity and deterministic output.
- Keep paired production and explicit component workflow promotion/cleanup guarantees distinct.
  Never broaden cleanup roots or remove outputs before durable validation; preserve rollback safety.
- Do not run acquisition, regional pipelines or notebooks as routine smoke tests. Keep generated
  data/outputs untracked; redistribution needs explicit rights review.

## Task routing and local skills

Read only the matching skill(s) before making the relevant change. From a workspace-root task,
open the linked `SKILL.md` explicitly if its name is not discovered; shell `cd` alone does not
establish discovery. Skills travel with this repository; no global installation is required.

| Task | Required guidance |
| --- | --- |
| Numerical/scientific change | [$scientific-change](.agents/skills/scientific-change/SKILL.md) + `docs/scientific-methodology.md` |
| Schema, key, config, provenance, hash or product compatibility | [$data-contract-change](.agents/skills/data-contract-change/SKILL.md) |
| Orchestration, stage dependency, cleanup, overwrite or promotion | [$pipeline-safety](.agents/skills/pipeline-safety/SKILL.md) + `docs/api.md`, `docs/pipelines.md` |
| Acquisition, regional or notebook execution | [$regional-run](.agents/skills/regional-run/SKILL.md); also pipeline-safety if changing lifecycle behavior |
| Behavior, API/import, component/provider/case-study or quality-gate change | [$validation](.agents/skills/validation/SKILL.md) |
| Ownership, module/import change | `ARCHITECTURE.md`; architecture regression tests |
| Typo/prose-only edit | Target document, relevant references and `git diff --check`; no skills or full architecture preload |

Numerical changes that affect cache identity also require data-contract-change. Test additions use
validation. Scientific or contract documentation changes load the corresponding skill, even if
no Python changes. `docs/reports/` are dated evidence, not proof of a current run.

## Architecture and API constraints

- Follow the ownership map and dependency direction in `ARCHITECTURE.md`; do not create a second
  ownership map here. Run `tests/pipeline/test_architecture.py` after changing ownership or imports.
- Keep package-root exports intentional and minimal. A supported public API change requires facade,
  documentation, and test updates; do not recreate removed pass-through modules or aliases.
- Keep CLI code as presentation over the typed API. Change canonical stage names or order only in
  `pipeline/api/registry.py`, then align the API, CLI, documentation, and contract tests.
- Put shared schemas, artifact paths, provenance, pair keys, and cleanup policy in their existing
  owners under `pipeline/contracts/`, not in calculation or finalization modules.

## Basic setup, validation and completion

Use isolated Python 3.11+; real geospatial runs require compatible GDAL/Rasterio.

```bash
python -m pip install -e ".[dev,analysis]"
PYTHONPATH=src python -m pytest -q tests/path/to/test_file.py
PYTHONPATH=src python -m pytest -q
git diff --check
```

Choose focused tests first; the validation skill owns conditional CI/format/type-check requirements.
For documentation-only changes, verify references and current names, inspect the diff and run
`git diff --check`; do not run the package suite. For every handoff, report changed files, checks
actually run, skips, missing data and unrun regional/native/integration paths. Preserve unrelated
changes and confirm no ignored outputs were staged. Do not hand-edit validation snapshots to
imply new evidence. Scientific source attribution, rights and processing metadata travel with data.

## Codebase navigation

Use the existing local `graphify-out/graph.json` for structural questions; skip graph work for
obvious targeted edits. Prefer the smallest useful retrieval:

1. Known symbol: `explain` first, then follow only relevant callers/callees/imports/dependencies.
2. Use direct relationships before deeper traversal; broad `query` is for unknown ownership.
3. If results are large or truncated, narrow the symbol or relationship before increasing budget.
   `--budget` is not a guaranteed cap. Avoid unnecessary depth-2 neighborhoods.
4. Read the identified source/tests. They outrank schemas/contracts, architecture docs, then the
   graph. Use targeted `rg` when graph coverage or freshness is insufficient.

```bash
graphify explain "run_components"
graphify affected "run_components" --relation calls --depth 1
graphify query "<topic>" --context call --budget 1500
```

`affected` follows reverse edges (callers); `explain` shows immediate connections. Use ast-grep
for syntax patterns and `rg` for literal/config/prose/filename searches. Read architecture for
ownership/import changes, not every typo. Optional shared setup/examples:
[code navigation](https://github.com/MarineCast/.github/blob/HEAD/docs/code-navigation.md)
(local grouped workspace: `../../.github/docs/code-navigation.md`). No sibling checkout is required.

Graphify is optional developer tooling (`graphifyy==0.9.62`, isolated Python 3.10+), not a runtime
dependency. Create a missing graph or refresh after structural edits, from this checkout only:

```bash
graphify extract . --code-only --no-cluster
```

Do not rebuild merely for a question. Check intentional deletions before using `--force` to bypass
shrink protection. `.gitignore` and `.graphifyignore` apply; AST-only extraction does not index
prose semantically. Graphs/caches are disposable local-only files: never commit or publish them.
Any future sharing requires an explicit policy covering destination, revision, freshness and review.
Global skill defaults do not override repository scope or code-only extraction. Do not run
`graphify codex install` over maintained instructions or enable hooks/merge drivers implicitly.
