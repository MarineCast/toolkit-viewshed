# Installation

Choose the environment for what you want to do. Reading the example and running the model have
different requirements.

## Read the documentation

The [San Juan guided example](../examples.md) uses committed model results. You can explore it in
your browser without Python, GDAL, credentials or source-data downloads.

To build the site locally from a clone:

```bash
python -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r requirements-docs.txt
python -m mkdocs serve
```

The docs build checks the existing example bundle; it does not acquire data or run the model.

## Run the toolkit

Use **Python 3.11 or later** with binary-compatible GDAL Python bindings and Rasterio.
From a clone, a concrete micromamba setup using the same specification as geospatial CI is:

```bash
micromamba create -y -n viewshed -f .github/environment.yml python=3.11
micromamba activate viewshed
python -m pip install -e '.[dev,analysis,acquisition]'
python -c "from osgeo import gdal; import rasterio; print(gdal.VersionInfo('RELEASE_NAME'), rasterio.__gdal_version__)"
```

For conda, use `conda env create -n viewshed -f .github/environment.yml`, then `conda activate viewshed`
and the same pip command. Initialize the environment manager's shell integration if activation
is unavailable. The YAML is an environment specification, not a complete dependency lock.

If you already have a compatible isolated environment, install from the repository root:

```bash
python -m pip install -e '.[dev,analysis,acquisition]'
viewshed-toolkit --help
```

`analysis` supplies optional analysis/plotting tools; `acquisition` supplies the USGS py3dep
downloader. Local, already prepared inputs do not require live acquisition. Native GDAL support
is required for the GDAL-backed land workflow.

Inspecting help does not execute a stage:

```bash
viewshed-toolkit stage --help
python -m viewshed_toolkit --help
```

Continue with the [quick start](quick-start.md). For exact public calls, see
[Python and CLI](../api.md).

## Tested support and limits

| Environment | Evidence |
| --- | --- |
| Linux, Python 3.11 and 3.14 | Configured geospatial CI matrix; inspect the check for your commit |
| macOS, Python 3.14.6 | Local reliability review and regression suite; see current validation evidence |
| Windows | No native acceptance run recorded in this improvement pass |
| Other Python/dependency combinations | Allowed package constraints do not imply every combination was tested |

A wheel installation check verifies the toolkit package, resources and CLI outside the source
checkout. It reuses scientific dependencies; it does not prove a fresh native environment solve.
The commands above follow the checked-in CI specification; a new conda/micromamba solve was not
performed during the local improvement pass.

Run the [offline first result](quick-start.md) next. See [troubleshooting](troubleshooting.md)
for native imports, missing inputs and stale artifacts.
