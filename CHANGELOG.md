# Change history

## Unreleased — reliability and first-result experience

- Check `process` run IDs and manifest collisions before stage execution; resume no longer
  authorizes replacing another logical run. Complete resume validates retained generation evidence;
  partial resume re-enters stage cache checks rather than claiming a checksum-only skip.
- Reject unsupported distance `--limit`/`--start` and remove the redundant terrain retention flag.
- Correct projected-CRS canopy tile filtering and distinguish raw source SHA-256 from artifact
  identity in version 2 acquisition manifests. Historical hashes are not relabeled.
- Resolve water component builds without DEM/CHM acquisition; preserve land dependencies.
- Remove unreferenced private helpers and redundant parameters; clarify legacy CLI support.
- Add a synthetic offline distance example, read-only `doctor`, troubleshooting, artifact dictionary,
  contribution guide and scientific acceptance checklist.
- Require browser-tested documentation before uploading the Pages deployment artifact.

Scientific formulas and current final-product schema versions are unchanged. These are unreleased
changes; this file does not imply a package publication, deployed documentation or regional rebuild.
Historical evidence is retained under `docs/reports/` with its original scope.

Partial-run manifests use schema version 3 to record canonical local input checksums, including
missing inputs and shapefile sidecars. Partial resume rejects changed/missing input identity and
historical manifests without that inventory before entering stage checks. It does not silently
certify older cached outputs. Rebuild affected producers with their explicit overwrite controls
or a fresh workspace, then create a new manifest. Remote source freshness is not certified by
local checksum validation.
