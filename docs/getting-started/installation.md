# Installation

Choose setup by the result you want. The [Lime Kiln visual walkthrough](../examples.md) and
[How it works](../understand/index.md) need only a browser.

## Run the offline example {#offline-example}

Use **Python 3.11 or later**. Start in the directory where you keep projects:

```bash
git clone https://github.com/MarineCast/toolkit-viewshed.git
cd toolkit-viewshed
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python examples/offline_distance.py --output work/my-first-distance
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Native Windows model
acceptance is not established here. The package still installs geospatial dependencies, including
Rasterio; **the offline example does not require the `osgeo` GDAL Python bindings**, optional
acquisition extras or source downloads. It creates synthetic geometry and a distance product.

Success prints JSON with `valid: true`, a positive row count and product paths.
Use a new output directory each time. Continue to [inspect your first result](quick-start.md#what-success-looks-like).

## Calculate a real terrain and canopy viewshed

Use compatible GDAL Python bindings and Rasterio. From the cloned repository root, a concrete
micromamba setup follows the checked-in geospatial CI specification:

```bash
micromamba create -y -n viewshed -f .github/environment.yml python=3.11
micromamba activate viewshed
python -m pip install -e '.[dev,analysis,acquisition]'
python -c "from osgeo import gdal; import rasterio; print(gdal.VersionInfo('RELEASE_NAME'), rasterio.__gdal_version__)"
```

For conda, use `conda env create -n viewshed -f .github/environment.yml`, then `conda activate viewshed`
and the same pip command. Initialize the environment manager's shell integration if activation
is unavailable. The YAML is an environment specification, not a complete dependency lock.

`analysis` supplies optional plotting tools; `acquisition` supplies the USGS py3dep downloader.
Prepared local inputs do not require live acquisition. Polars is constrained to 1.x by the package.
Inspect commands without executing a model:

```bash
viewshed-toolkit --help
viewshed-toolkit stage --help
```

Next: [Run the San Juan model](../san-juan-demo.md), including input requirements and output paths.

## Setup evidence and limits

The documentation improvement checks the offline sequence in a fresh environment; its exact
platform, dependency versions and outcome are recorded in the
[documentation validation note](../development/lime-kiln-validation.md).
The native recipe follows `.github/environment.yml`; no fresh native environment solve or
regional run is implied by the offline check. Consult CI for the selected revision and
[troubleshooting](troubleshooting.md) for native imports or stale artifacts.

To work on this website, see [Build the documentation](../development/index.md#build-the-documentation).
