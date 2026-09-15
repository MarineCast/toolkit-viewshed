# Data

This directory contains small example datasets used for demonstrations and testing, plus
locally copied OrcaCast viewshed products when present.

Large source datasets, generated rasters, and locally processed data should not be committed
to this repository unless redistribution is explicitly permitted.

Each dataset included here should document:

- Source
- Original URL or citation
- License
- Processing performed
- Spatial reference system
- Date accessed

## Historical OrcaCast case-study products

The migration copied the complete H3 R7 static pair kernels to:

- `processed/domain/human/viewshed/RES7/LAND_STATIC_WEIGHTS_R7.parquet`
- `processed/domain/human/viewshed/RES7/WATER_STATIC_WEIGHTS_R7.parquet`

Their adjacent JSON sidecars retain the source configuration, factor-coverage checks, and
artifact checksums. These files are generated research artifacts and remain ignored by Git.
They are physical source-to-target viewability kernels, not observer effort, detection
probabilities, or whale-occurrence estimates.

The manifest is not part of the current tree. It is preserved in the
[last revision containing the OrcaCast analysis subtree](https://github.com/stevetylda/viewshed-toolkit/blob/18042d2e570506a90ed826dd6fda92277fc92c1c/analysis/case_studies/orcacast/manifests/artifacts.json).
Restore files only from a matching historical source bundle, using the exact recorded paths, byte
sizes, and SHA-256 values. See the [historical resource and restoration guide](../docs/reports/orcacast-history.md)
for the provenance boundary and retrieval instructions. This checkout does not contain an
automated artifact-sync helper.

The current `analysis/salish_sea/` case study is newer and geographically expanded. Its artifacts
are not interchangeable with this OrcaCast bundle.

The manifest expects generated map products under `outputs/effort/viewshed`. A local copy under
`data/outputs/effort/viewshed` does not satisfy that contract and is reported as missing by the
checked-in migration-validation snapshot.
