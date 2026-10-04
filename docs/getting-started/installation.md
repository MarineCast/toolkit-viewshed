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

Use **Python 3.11 or later** in an isolated environment with binary-compatible GDAL Python bindings
and Rasterio. The repository's [geospatial CI environment](../../.github/environment.yml) gives a
concrete dependency setup. Install from the repository root:

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
