# Viewshed Toolkit

A Python toolkit for reproducible geospatial viewshed and visibility modeling.

The project includes tools for:

- terrain and surface preprocessing
- observer geometry
- viewshed calculation
- raster and vector processing
- spatial aggregation
- validation and sensitivity analysis
- reproducible geospatial analysis
- visualization and reporting

The toolkit was originally developed to support observer-viewability modeling for marine
wildlife applications in the Salish Sea, but the core library is designed to remain
application-independent.

## Project structure

```text
src/viewshed_toolkit/   Reusable Python package
analysis/               Research and experimental analyses
notebooks/              Exploratory and demonstration notebooks
tests/                  Automated tests
examples/               Small runnable examples
docs/                   User and API documentation
reports/                Technical reports and case studies
assets/                 Figures, maps, and documentation assets
data/                   Small redistributable example datasets
scripts/                Command-line and workflow scripts
