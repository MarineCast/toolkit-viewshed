# Historical OrcaCast migration resources

The former `analysis/case_studies/orcacast/` subtree was intentionally removed in commit
[`3dd47e6`](https://github.com/stevetylda/viewshed-toolkit/commit/3dd47e6c619357099a63f8ff840d131cd9eb6b52)
when the expanded Salish Sea case study was introduced. It is not a current repository location
and should not be recreated as part of ordinary toolkit work.

The last revision containing the complete tracked OrcaCast subtree is
[`18042d2`](https://github.com/stevetylda/viewshed-toolkit/tree/18042d2e570506a90ed826dd6fda92277fc92c1c/analysis/case_studies/orcacast).
That pinned tree contains the historical notebooks, observation contract, dated audit, README, and
[artifact manifest](https://github.com/stevetylda/viewshed-toolkit/blob/18042d2e570506a90ed826dd6fda92277fc92c1c/analysis/case_studies/orcacast/manifests/artifacts.json).

The current `analysis/salish_sea/` directory belongs to a newer, geographically expanded case
study. Its report and outputs are not substitutes for the migrated OrcaCast snapshot or its
artifact inventory.

## Restoring a historical artifact bundle

Restore only from a bundle known to match the pinned OrcaCast revision:

1. Retrieve `manifests/artifacts.json` from the pinned revision above, either through the link or,
   from a clone containing that revision, with:

   ```bash
   git show \
     18042d2e570506a90ed826dd6fda92277fc92c1c:analysis/case_studies/orcacast/manifests/artifacts.json \
     > /tmp/orcacast-artifacts.json
   ```

2. Place every bundle file at the exact repository-relative `path` recorded in the manifest.
3. Verify the recorded byte count and SHA-256 for every restored file before using it.
4. Treat the restored products as historical local data. Do not commit them or describe them as a
   rebuild from the current pipeline.

The repository has no artifact-sync or manifest-verification helper. The checked-in
[`viewshed_migration_validation.json`](viewshed_migration_validation.json) is a preserved report
from the earlier migration. Paths inside that generated JSON describe the tree at the time of the
inspection; they are not current checkout paths, and the snapshot must not be edited to imply that
validation was rerun.
