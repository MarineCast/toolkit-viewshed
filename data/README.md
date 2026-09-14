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

## Local OrcaCast case-study products

The migration copied the complete H3 R7 static pair kernels to:

- `processed/domain/human/viewshed/RES7/LAND_STATIC_WEIGHTS_R7.parquet`
- `processed/domain/human/viewshed/RES7/WATER_STATIC_WEIGHTS_R7.parquet`

Their adjacent JSON sidecars retain the source configuration, factor-coverage checks, and
artifact checksums. These files are generated research artifacts and remain ignored by Git.
They are physical source-to-target viewability kernels, not observer effort, detection
probabilities, or whale-occurrence estimates.

The complete ignored payload is tracked by relative path, byte size, and SHA-256 in
`analysis/case_studies/orcacast/manifests/artifacts.json`. Use
`scripts/sync_case_study_artifacts.py` to verify it or copy it from a matching source bundle.
