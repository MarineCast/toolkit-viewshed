# Viewshed Toolkit

Viewshed Toolkit is a Python package for reproducible terrain, vegetation, distance, and
source-to-target visibility modeling. It began as OrcaCast's Salish Sea viewshed subsystem. The
reusable library now lives under `src/viewshed_toolkit`; OrcaCast-specific notebooks, contracts,
and artifact inventories are kept in an explicit case study.

## Requirements and installation

- Python 3.11 or newer.
- A geospatial environment with compatible GDAL and Rasterio builds.
- Regional terrain and geometry inputs for an actual pipeline run.

Install the package and development tools from a checkout:

```bash
python -m pip install -e ".[dev,analysis]"
viewshed-toolkit --help
python -m viewshed_toolkit --help
```

Optional source-acquisition dependencies are available through `.[acquisition]`.

## Start with configuration

The wheel contains a default Salish Sea configuration, so importing and configuration loading do
not depend on a source checkout:

```python
from viewshed_toolkit import load_app_config
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())
print(config.region.name, config.region.bbox_wgs84)
```

Relative data and output paths resolve against the current project directory. Set
`VIEWSHED_TOOLKIT_ROOT` to choose that root explicitly, or pass `--config` to a CLI command.

The supported public import surface is the `viewshed_toolkit` package root. Stage-level code
should import from its canonical module under `viewshed_toolkit.pipeline`; the former top-level
pass-through modules are not part of the package. See the [API guide](docs/api.md) for examples
and execution side effects.

## Run the CLI

Inspect commands without starting the pipeline:

```bash
viewshed-toolkit --help
viewshed-toolkit terrain-weight --help
viewshed-toolkit finalize-viewshed-lookups --help
```

The installed command and `python -m viewshed_toolkit` are equivalent. `scripts/run_viewshed.py`
is only a source-checkout wrapper around the same CLI.

Running the complete workflow requires the configured DEM, canopy-height model, land and water
polygons, H3 support products, and compatible GDAL Python bindings. Finalization and complete API
runs can remove stage scratch products after durable outputs are written; review the
[API guide](docs/api.md) before running against an existing output directory.

## Repository organization

```text
src/viewshed_toolkit/
  __init__.py                  Supported Python facade
  __main__.py                  python -m viewshed_toolkit
  pipeline/
    api/                       Typed orchestration and stage registry
    cli/                       Argument parsing and terminal presentation
    config/                    Configuration loading and path contracts
    contracts/                 Schemas, artifact paths, cleanup, and provenance
    prepare/                   Area, elevation, and vegetation preparation
    weights/                   Distance, terrain, and vegetation calculations
    finalize/                  Durable pair-kernel materialization
    visualization/             Static and interactive map exports
    diagnostics/               Source-cell audit tools
  _internal/                   Shared private persistence and geospatial support
  resources/                   YAML configuration bundled into wheels
configs/                       Editable checkout configuration
analysis/case_studies/orcacast/ OrcaCast notebooks, contracts, history, and artifact inventory
data/                           Ignored local inputs and copied research artifacts
outputs/                        Ignored generated maps and rasters at their canonical paths
tests/                          API, architecture, and scientific regression tests
docs/                           API, methodology, and validation documentation
scripts/                        Source-checkout CLI wrapper
```

## OrcaCast case study

The editable case-study configuration is `configs/salish_sea.yaml`. The two migrated notebooks
are:

- `analysis/case_studies/orcacast/notebooks/01_STATIC_VIEWABILITY.ipynb`
- `analysis/case_studies/orcacast/notebooks/02_LAND_REPORTING_OPPORTUNITY.ipynb`

The tracked artifact manifest describes 56 ignored files totaling 489,629,550 bytes. Generated
data is not part of the installable package or a fresh clone. Restore each file at the exact
repo-relative `path` recorded in
`analysis/case_studies/orcacast/manifests/artifacts.json`, then verify its byte count and SHA-256.
The repository currently has no automated sync or migration-validation helper, so older commands
that referenced those scripts are no longer documented as runnable.

The checked-in [migration validation snapshot](docs/reports/validation.md) confirms the two
primary H3 kernels but does not establish a complete artifact restoration or a raw-data rebuild.

## Scientific boundary

The durable pair-table grain is exactly `source_h3 × target_h3`. Static viewability is a physical
visibility kernel assembled from terrain/line-of-sight and conditional vegetation support;
distance remains separately inspectable. Missing, unavailable, and not-applicable states are not
interchangeable with observed zeroes.

These products do not estimate observer activity, reporting capture, whale detection probability,
or whale occurrence. The OrcaCast reporting-opportunity notebook composes later layers while
keeping those concepts separate.

## Development checks

```bash
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

Run Black on Python files you change. The current full-tree Black baseline still identifies eight
pre-existing files that need formatting, so a whole-tree `black --check src tests` is not listed as
a passing gate.

The latest source-organization cleanup passed 202 tests plus source and built-wheel import smoke
checks. The regional pipeline and notebooks were not rerun because their full upstream data bundle
is not present. See the [validation status](docs/reports/validation.md) for the exact boundary.

Code is Apache-2.0 licensed. Third-party source data and derived outputs retain their original
terms; inspect the case-study metadata before redistribution.
