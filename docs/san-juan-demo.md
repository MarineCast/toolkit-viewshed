# Run the San Juan model

The [guided Examples page](examples.md) uses the broader regional delivery. This page is the bounded
real-model recipe; the [documentation reproduction guide](documentation-examples.md) separately
covers scientific export, figure rendering and website checks.

## 1. Prepare the environment and inputs

Clone the repository and use the [native GDAL setup](getting-started/installation.md#calculate-a-real-terrain-and-canopy-viewshed).
Run commands from the repository root. Read
[`configs/san_juan_demo.yaml`](../configs/san_juan_demo.yaml) before execution: this is the
100 m, R7, 5 km bounded San Juan demonstration, not the 30 m regional delivery.

The runner uses real Natural Earth land, USGS 3DEP ground and ETH 2020 canopy. Its default
input/output tree is `data/san_juan_demo/`. A model rebuild may acquire missing inputs and prepare
rasters; it is not the offline example. Check provider availability, cache locations and disk space.
The [dataset card](dataset-card.md) records source rights, fallback assumptions and limitations.

## 2. Run the bounded model

```bash
PYTHONPATH=src python scripts/run_san_juan_demo.py \
  --config configs/san_juan_demo.yaml --rebuild --model-only
```

`--rebuild` explicitly replaces the configured demo products. Use a separate checkout or a copied
configuration with distinct raw/prepared/work/output paths if you need to preserve an existing
run. This command does not rebuild the website's committed figures.

## 3. Find and inspect the result

With the unchanged example configuration, final products are in `data/san_juan_demo/outputs/`:
land/water static weights, matching observation geometry and a generation receipt. The configuration's
`paths.final_output_dir` is authoritative if you change it.

```python
from pathlib import Path
import polars as pl

root = Path("data/san_juan_demo/outputs")
land = pl.read_parquet(root / "LAND_STATIC_WEIGHTS_R7.parquet")
lime_kiln = land.filter(pl.col("source_h3") == "8728d1061ffffff")
print(lime_kiln.select(
    "source_h3", "target_h3", "weight_distance", "weight_terrain",
    "weight_vegetation", "weight_static_viewability",
))
```

This selects the **land source area containing Lime Kiln**, not a single lighthouse viewpoint.
The [visual walkthrough](examples.md) now uses different regional inputs, 30 m analysis, five
observer samples and a 30 km distance limit. This bounded recipe will not reproduce its values.
Preserve accompanying metadata. A new acquisition or scientific configuration may differ from the preserved October
example; matching filenames alone does not establish matching generations.

## 4. Continue

[Read the outputs](products/index.md) to choose a table and interpret zero versus unavailable
support. For maintainer tasks, [reproduce the instructional bundle](documentation-examples.md).
The bounded native model was not rerun during the November 6 documentation revision.
