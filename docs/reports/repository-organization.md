# Repository organization review

## Outcome

The repository now has three explicit boundaries:

1. A supported package facade in `src/viewshed_toolkit/*.py`.
2. The staged scientific implementation in `src/viewshed_toolkit/pipeline/`, with shared private
   infrastructure in `src/viewshed_toolkit/_internal/`.
3. Application-specific notebooks, research contracts, provenance, and artifact inventory in
   `analysis/case_studies/orcacast/`.

This separation keeps the wheel reusable without discarding OrcaCast context. Packaged YAML
resources make the default CLI/configuration load outside a source checkout. The editable
checkout copy remains in `configs/salish_sea.yaml` and is regression-tested against the packaged
copy.

Inside the staged implementation, reusable schemas, path conventions, cleanup policy, pair
constants, provenance identity, and stage registration now live in `pipeline/contracts/` and
`pipeline/api/registry.py`. Early prepare/configuration modules no longer reach into the finalizer
to obtain those definitions.

## Issues addressed

- Redundant top-level re-export modules were removed; the package root is the single supported
  workflow facade and advanced imports point at canonical pipeline modules.
- Previously empty documentation and report placeholders now have defined roles and usable
  content.
- OrcaCast notebooks and its broad observation/reporting contract no longer sit in the reusable
  package surface.
- Generated case-study data remains ignored, while a tracked relative-path SHA-256 manifest
  makes the expected bundle auditable.
- The current validation report no longer embeds workstation-absolute paths.
- The dated OrcaCast audit is visibly labeled historical.
- The wheel includes its default and named-area YAML resources and does not include the former
  `core` namespace or the OrcaCast observation contract.
- Service provenance is configurable, with toolkit-generic defaults and an explicit OrcaCast
  identity in the Salish Sea configurations.
- Stage order and callable registration have one declarative source of truth.
- Pair schemas, artifact paths, safe cleanup, and Parquet I/O contracts no longer live inside the
  1,800-line finalization implementation.
- The case-study import works from the ordinary `src` package layout; tests no longer require
  adding `analysis` as a second Python path.

## Deliberately deferred

Six implementation modules remain large: `runner.py`
(1,522), `los.py` (1,396), `gdal.py` (1,342), `interactive_maps.py` (1,334), and
`summarize.py` (1,128), plus `final_artifacts.py` (1,234 lines after the first extraction).
They combine multiple internal responsibilities and are the main remaining maintainability
concern.

They were not split during migration because they implement tightly coupled scientific and
artifact contracts. A future refactor should first add characterization fixtures for artifact
schemas/checksums, LOS backend parity, cleanup containment, and rendered map inventory, then
extract one responsibility at a time without changing persisted outputs.

The pipeline also retains `orcacast.*` Parquet metadata keys for compatibility with copied
artifacts. Those keys should only be versioned or renamed through an explicit schema migration.

The case-study documentation references `scripts/sync_case_study_artifacts.py` and
`scripts/validate_copied_outputs.py`, but those helpers are not present in the current checkout.
Restoring them or removing the stale commands is a remaining repository-level cleanup item,
separate from the `src` package boundaries reviewed here.

## Validation boundary

The organized repository passed 202 tests using the ordinary `PYTHONPATH=src` layout, a full
Pyflakes scan, wheel construction, archive-content inspection, and built-wheel import and CLI
smoke tests from `/tmp`. The existing migration JSON was syntax-checked but not regenerated; the
notebooks were not run, and the regional viewshed pipeline was not rebuilt because its full
upstream data bundle is not present.
