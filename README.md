# Viewshed Toolkit

Viewshed Toolkit is a Python package for reproducible terrain, vegetation, distance, and
source-to-target visibility modeling. It began as OrcaCast's Salish Sea viewshed subsystem;
the reusable library now lives in `src/viewshed_toolkit`, while the original application is
kept as an explicit case study.

## Install and inspect

Use a GDAL-capable geospatial environment:

```bash
python -m pip install -e '.[dev,analysis]'
viewshed-toolkit --help
python -m viewshed_toolkit --help
pytest -q
```

The installed command uses a packaged default Salish Sea configuration, so `--help` and
configuration loading work outside the source checkout. Relative data and output paths resolve
against the current project directory. Set `VIEWSHED_TOOLKIT_ROOT` to make that workspace root
explicit, or pass `--config` for another region.

```python
from viewshed_toolkit import load_app_config
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())
print(config.region.name, config.region.bbox_wgs84)
```

Running the complete workflow requires the regional DEM, canopy-height model, land and water
polygons, configured H3 support products, and compatible GDAL Python bindings. Optional source
acquisition libraries are available through `.[acquisition]`.

## Repository organization

```text
src/viewshed_toolkit/           Supported public modules and staged implementation
  _internal/                    Shared implementation support; not a public API
  pipeline/                     Prepare, weight, finalize, visualize, and service stages
  resources/                    Configuration bundled into wheels
configs/                        Editable checkout configuration
analysis/case_studies/orcacast/ OrcaCast notebooks, history, and artifact inventory
data/                           Local ignored inputs and durable pair kernels
outputs/                        Local ignored generated maps and rasters
tests/                          Public-contract and focused scientific regression tests
docs/                           Package and methodology documentation
  assets/                       Small tracked documentation assets
  reports/                      Current validation and migration reports
scripts/                        CLI wrapper, artifact sync, and migration validation
```

The supported public import surface is the `viewshed_toolkit` package root. Stage-specific and
scientific implementations live at their canonical paths under `pipeline/`; redundant top-level
re-export modules are intentionally not retained. See [API documentation](docs/api.md) for the
supported boundary.

## OrcaCast case study

The case study configuration is `configs/salish_sea.yaml`. The two migrated notebooks are:

- `analysis/case_studies/orcacast/notebooks/01_STATIC_VIEWABILITY.ipynb`
- `analysis/case_studies/orcacast/notebooks/02_LAND_REPORTING_OPPORTUNITY.ipynb`

The 56-file, roughly 490 MB data/output payload is intentionally ignored by Git. A tracked,
repo-relative SHA-256 inventory makes it recoverable and verifiable:

```bash
python scripts/sync_case_study_artifacts.py
python scripts/sync_case_study_artifacts.py \
  --source-root /path/to/OrcaCast
PYTHONPATH=src python scripts/validate_copied_outputs.py \
  --output docs/reports/viewshed_migration_validation.json
```

The copy command expects the source bundle to retain the same `data/...` and `outputs/...`
relative paths. Existing mismatched files are preserved unless `--overwrite` is supplied. The
original generated manifests remain untouched because their absolute OrcaCast paths are useful
historical provenance; the tracked artifact inventory is the portable transfer contract.

## Scientific boundary

The durable pair-table grain is exactly `source_h3 × target_h3`. Static viewability is a
physical visibility kernel assembled from terrain/line-of-sight and conditional vegetation
support; distance remains separately inspectable. Missing, unavailable, and not-applicable
states are not interchangeable with observed zeroes.

These products do not estimate observer activity, reporting capture, whale detection
probability, or whale occurrence. The OrcaCast reporting-opportunity notebook composes later
layers but keeps them conceptually and contractually separate.

Code is Apache-2.0 licensed. Third-party source data and derived outputs retain their original
terms; inspect the case-study metadata before redistribution.
