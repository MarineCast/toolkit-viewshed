# Viewshed Toolkit

A Python 3.11+ package for reproducible terrain, canopy, and distance visibility modeling.
It produces provenance-aware H3 tables at one unique `source_h3 × target_h3` pair per source
role, with each component retained for inspection.

These are **static physical viewability** products. They do not estimate observer effort,
public access, reporting probability, animal detection probability, occurrence, or abundance.

```text
configuration → area / source cells / water target cells → candidate pairs
                     │
       ┌─────────────┼──────────────────┐
       ▼             ▼                  ▼
 DEM provider    CHM provider       pair distances
       ↓             ↓                  ↓
  download       download          configured decay
       ↓             ↓                  ↓
  prepare DEM    prepare CHM       distance table
       ↓             ↓
  terrain LOS → canopy LOS / conditional factor
       └─────────────┴──────────────────┘
                     ↓
        composition → final tables → validation → maps
```

## Install

Use an isolated environment with binary-compatible GDAL and Rasterio, then:

```bash
python -m pip install -e '.[dev,analysis]'
viewshed-toolkit --help
```

Install `.[acquisition]` to use the USGS py3dep downloader. The package itself remains Python;
The small end-to-end test requires GDAL
Python bindings. `.github/environment.yml` describes the geospatial CI environment.

## Configure a region

Start with `configs/salish_sea.yaml`, or the configuration bundled in the wheel:

```python
from viewshed_toolkit import load_app_config
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())  # read-only
print(config.region.bbox_wgs84)
```

The Salish Sea config uses the single `model_area` definition in packaged `common.yaml`, matching
the OrcaCast SRKW domain. For another region, remove `area` and supply `region.bbox_wgs84` with
`min_lon`, `min_lat`, `max_lon`, and `max_lat`. Configure land/water geometry paths, provider
settings, H3 resolution, physical assumptions, and separate working/durable output directories.
Relative paths resolve through the existing project-root helpers; `VIEWSHED_TOOLKIT_ROOT` selects
an intentional alternate project root. See [configuration](docs/configuration.md).

## Run independent stages

The explicit component workflow supplements the existing paired production workflow:

```bash
viewshed-toolkit stage download-dem --config configs/salish_sea.yaml
viewshed-toolkit stage download-chm --config configs/salish_sea.yaml
viewshed-toolkit stage prepare-dem --config configs/salish_sea.yaml
viewshed-toolkit stage prepare-chm --config configs/salish_sea.yaml

# Build a selected product and its dependencies:
viewshed-toolkit build dem --config configs/salish_sea.yaml --run-id dem
viewshed-toolkit build chm --config configs/salish_sea.yaml --run-id chm
viewshed-toolkit build distance --config configs/salish_sea.yaml --run-id distance
viewshed-toolkit build all --config configs/salish_sea.yaml --source-type land --run-id land
viewshed-toolkit build all --config configs/salish_sea.yaml --source-type water --run-id water
```

`stage` runs exactly one operation and requires its upstream inputs. `build` resolves dependencies;
it can download regional data and run substantial computation. Geometry inputs must already exist.
Use a dedicated configuration/output directory for a new regional run. `--overwrite` rebuilds
selected products; unchanged stages validate fingerprints and checksums before reuse.

## What the components mean

- **DEM:** terrain LOS diagnostics plus the existing observer-to-water-pixel distance-integrated
  `weight_terrain` kernel. It runs without CHM.
- **CHM:** conditional additional canopy obstruction from matched DEM and DEM+CHM surfaces.
  It requires the DEM component, but never the centroid-distance artifact.
- **Distance:** configured logistic, exponential, or piecewise attenuation of pair centroid
  distances, independent of rasters.
- **Static:** `weight_terrain × weight_vegetation`. Distance is already integrated inside the
  terrain kernel; multiplying by the centroid-distance diagnostic again would change the science.

See [scientific methodology](docs/scientific-methodology.md) for formulas and missingness rules.

## Outputs

The explicit workflow writes under `<paths.final_output_dir>/components/`:

```text
inputs/{dem,chm}/         Source assets, download manifests, checksums
geometry/                Area and target-cell products
weights/{land,water}/     dem_weights, chm_weights, distance_weights, static_weights
final/                   land_static_weights.parquet / water_static_weights.parquet
manifests/               Per-source run metrics and validation reports
maps/                    Static target-support HTML maps
```

Prepared DEM/CHM, source cells, pair lookup, and scratch partitions use the existing configured
paths. Component stages never clean these directories implicitly. The earlier
`run_viewshed`/`process` workflow still produces its original compact final contracts and retains
its existing cleanup behavior; see [API](docs/api.md) and [pipelines](docs/pipelines.md).

## Columbia River to northern Vancouver Island

The regional case study uses `configs/salish_sea_case_study.yaml`. Its `case_study` section
creates `analysis/salish_sea`; all raster inputs, intermediate data, weights, aggregates, maps,
and run logs are stored under `data/salish_sea`. The HTML analysis report lives in the analysis
folder. This domain extends beyond the separate canonical OrcaCast model-area configuration.

```bash
PYTHONPATH=src python scripts/run_case_study.py --config configs/salish_sea_case_study.yaml
```

The command acquires data and performs the complete land and water calculation. It is a regional
run, not a quick smoke test. See [the case-study guide](docs/salish-sea-case-study.md) for outputs,
report packaging, and source limitations.

## Reproduce and validate

```bash
PYTHONPATH=src python -m pytest -q tests/pipeline/test_component_workflow.py
viewshed-toolkit validate-region --config configs/salish_sea.yaml --inputs-only
viewshed-toolkit validate-region --config configs/salish_sea.yaml
```

The CI fixture is synthetic coast, terrain, and canopy at Admiralty Inlet coordinates; it contains
multiple land/water H3 cells and needs no network. It also compares separated land execution
with the existing paired runner. It is not measured local elevation or a full regional rebuild.
The [Salish Sea guide](docs/salish-sea-case-study.md) records the regional data boundary.

## Development and design

```bash
PYTHONPATH=src python -m pytest -q
ruff check src tests
python scripts/check_components.py
python -m compileall -q src
```

The component code passes expanded Ruff, Black, and strict mypy gates. Existing unrelated
formatting/import-order debt remains outside those incremental gates.

- [Architecture](docs/architecture.md) · [Acquisition](docs/data-acquisition.md)
- [Performance](docs/performance.md)
- [Refactor validation and remaining boundaries](docs/reports/productionization.md)

Code is Apache-2.0 licensed. Source data and derived products retain their original terms and
are not redistributed with the package.
