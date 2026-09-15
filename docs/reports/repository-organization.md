# Repository organization review

> **Historical report.** This review describes revision `18042d2`, before the OrcaCast analysis
> subtree was intentionally removed. The current application-specific location is
> `analysis/salish_sea/`. See the [historical resource guide](orcacast-history.md); the two case
> studies are not interchangeable.

## Outcome

At the reviewed revision, the repository had three explicit boundaries:

1. A supported package facade in `src/viewshed_toolkit/__init__.py`, with
   `src/viewshed_toolkit/__main__.py` as the module CLI entry point.
2. The staged scientific implementation in `src/viewshed_toolkit/pipeline/`, with shared private
   infrastructure in `src/viewshed_toolkit/_internal/`.
3. Application-specific notebooks, research contracts, provenance, and artifact inventory in the
   [then-current OrcaCast subtree](https://github.com/stevetylda/viewshed-toolkit/tree/18042d2e570506a90ed826dd6fda92277fc92c1c/analysis/case_studies/orcacast).

That separation kept the wheel reusable without discarding OrcaCast context. Packaged YAML
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
- Documentation at that revision did not advertise an artifact-sync helper and pointed to the
  then-present historical manifest. Current documentation uses pinned revision links.
- Previously empty documentation and report placeholders now have defined roles and usable
  content.
- OrcaCast notebooks and its broad observation/reporting contract no longer sit in the reusable
  package surface.
- Generated case-study data remained ignored, while the relative-path SHA-256 manifest—now
  available only in the pinned historical tree—made the expected bundle auditable.
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

Five implementation modules remain large: `runner.py` (1,522), `los.py` (1,396), `gdal.py`
(1,342), `interactive_maps.py` (1,334), and `final_artifacts.py` (1,234 lines after the first
extraction). They combine multiple internal responsibilities and are the main remaining
maintainability concern.

They were not split during migration because they implement tightly coupled scientific and
artifact contracts. A future refactor should first add characterization fixtures for artifact
schemas/checksums, LOS backend parity, cleanup containment, and rendered map inventory, then
extract one responsibility at a time without changing persisted outputs.

The pipeline also retains `orcacast.*` Parquet metadata keys for compatibility with copied
artifacts. Those keys should only be versioned or renamed through an explicit schema migration.

The historical case-study artifact manifest has no automated restore or validation helper in the
current checkout. Current restoration instructions retrieve the manifest from its pinned revision.
Restoring or reimplementing the helper remains separate tooling work.

## Validation boundary

The organized repository passed 202 tests using the ordinary `PYTHONPATH=src` layout, a full
Pyflakes scan, wheel construction, archive-content inspection, and built-wheel import and CLI
smoke tests from `/tmp`. The existing migration JSON was syntax-checked but not regenerated; the
notebooks were not run, and the regional viewshed pipeline was not rebuilt because its full
upstream data bundle is not present.
