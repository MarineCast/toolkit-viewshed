# Methodology status

The current implementation and interpretation contract is documented in the
[methodology guide](../methodology.md). The copied July 2026 audit under
`analysis/case_studies/orcacast/history/` is historical provenance and must not be used as the
current module map or test status.

Large terrain, GDAL, and map-rendering modules were preserved during migration to minimize
scientific change. Their interfaces now sit behind a small public facade. Splitting those
modules is a future maintainability change that should be accompanied by characterization tests
and artifact-parity checks, not mixed into the source extraction.
