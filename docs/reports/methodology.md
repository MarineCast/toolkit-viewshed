# Methodology status

The current implementation and interpretation contract is documented in the
[methodology guide](../methodology.md). The copied July 2026 audit exists only in the
[pinned historical OrcaCast tree](https://github.com/stevetylda/viewshed-toolkit/tree/18042d2e570506a90ed826dd6fda92277fc92c1c/analysis/case_studies/orcacast/history)
and must not be used as the current module map or test status. See the
[historical resource guide](orcacast-history.md) for why that subtree is absent now.

Large terrain, GDAL, and map-rendering modules were preserved during migration to minimize
scientific change. Their interfaces now sit behind a small public facade. Splitting those
modules is a future maintainability change that should be accompanied by characterization tests
and artifact-parity checks, not mixed into the source extraction.
