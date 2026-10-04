# Troubleshooting

Start with a read-only check, using your own configuration:

```bash
viewshed-toolkit doctor --config configs/salish_sea.yaml --workflow land
```

Use `water` or `distance` for those component workflows. A missing input is a readiness finding,
not permission to substitute zero support. The diagnostic does not download or modify files.

| Symptom | Likely cause | Safe next step |
| --- | --- | --- |
| `No module named osgeo` or native import failure | GDAL bindings missing or mixed binary environments | Use the [single conda-forge environment](installation.md); inspect GDAL/Rasterio versions before rebuilding anything. |
| Missing land/water geometry | Configuration points at absent regional inputs | Check resolved paths in `doctor`; provision the intended geometry. `build` is not a general coastline downloader. |
| Missing DEM/CHM | Land workflow inputs are not prepared | Inspect `download-*` and `prepare-*` requirements in [pipelines](../pipelines.md); acquisition may use network and substantial disk. |
| Manifest belongs to another logical run | Same run ID used with different settings or stages | Choose a new run ID and isolated output roots. `resume=True` does not authorize a different identity. |
| Resume rejects changed inputs or missing generation evidence | Existing products no longer prove current producer identity | Preserve the old set for inspection; rebuild producers in a fresh workspace. Never fabricate a receipt or refresh metadata to certify old values. |
| Profile cutoff exceeds coverage | Pair universe is narrower than requested profile | Lower the profile cutoff or intentionally generate a wider universe. Missing pairs are not zeros. |
| Distance CLI rejects `--limit` / `--start` | These flags were previously accepted but ignored | Bound the configuration's area/range. The distance commands only support complete runs. |
| Empty canopy tile selection | No mapped land intersects the request, or invalid geometry/CRS | Inspect bounds and CRS. Both geographic and projected land inputs are supported; missing CRS is an error. |
| Offline example reports an existing directory | Overwrite protection | Choose a new output directory. The example never clears existing products. |
| Blank interactive map | Missing bundle or blocked script | Read the static lesson figures/captions; rebuild docs with `mkdocs build --strict` and inspect browser errors. |

## Cache and overwrite semantics

`force=True` on `process` authorizes replacement of a manifest; it does not force every producing
stage to recalculate. Full-run resume validates the retained generation and currently present
inputs before skipping. Partial resume re-enters the requested stage's own cache checks and
returns a non-skipped orchestration result. `run_components(..., overwrite=True)` rebuilds its
selected dependency closure. See [workflow contracts](../pipelines.md) before choosing one.

For a bug report, include the commit, command, redacted configuration, `doctor` output, error and
smallest reproducing input. Do not attach credentials, private locations or large source datasets.

Partial-run manifests use schema version 3 to record canonical local input checksums, including
missing inputs and shapefile sidecars. Partial resume rejects changed/missing input identity and
historical manifests without that inventory before entering stage checks. It does not silently
certify older cached outputs. Rebuild affected producers with their explicit overwrite controls
or a fresh workspace, then create a new manifest. Remote source freshness is not certified by
local checksum validation.
