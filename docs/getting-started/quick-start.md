# Your first result

Get a small, validated distance product before acquiring regional terrain or canopy data.
After [installation](installation.md), run from the repository root:

```bash
python examples/offline_distance.py --output work/my-first-distance
```

Use a **new directory**. The example refuses to overwrite one that already exists. It creates an
invented straight coastline and calls the production geometry, pair-distance and profile APIs.
It does not download data, require the GDAL Python bindings, or calculate terrain/canopy LOS.
Rasterio and the package's other dependencies are still required.

## What success looks like

The final JSON prints `valid: true`, a positive `rows` count, and two absolute product paths.
`result.json` records the same checks in your output directory:

| File | What it contains |
| --- | --- |
| `config.yaml` | Fully inspectable configuration with synthetic input/output paths |
| `land.geojson`, `water.parquet` | Invented geometry, not observed coastline |
| `products/components/distance/land/pair_distances.parquet` | Unique candidate pairs with centroid distances |
| `products/components/distance/land/profiles/five-km-*.parquet` | Exponential distance weights, with checksum/provenance sidecar |
| `result.json` | Synthetic provenance, artifact paths and successful checks |

The example validates both products and checks `weight_distance = exp(-distance_km / 5)` for
pairs with centroid distance at most 1 km, and zero beyond that hard cutoff. The geometry-based
candidate universe may retain cells whose centroids fall outside the cutoff. This is a distance-only diagnostic, not physical viewability or
detection probability. The profile's cutoff cannot exceed the pair product's modeled coverage.

Inspect the path printed as `profile`:

```python
import polars as pl

# Substitute the exact profile path printed by the example.
frame = pl.read_parquet("<profile-path>")
print(frame.select("source_h3", "target_h3", "distance_km", "weight_distance").head())
```

## Inspect readiness without running a model

```bash
viewshed-toolkit doctor --config work/my-first-distance/config.yaml --workflow distance
```

`doctor` checks local input presence, reports Python/native-library versions, and inspects output
permission hints without creating directories, downloading or executing a stage. It exits 1 when
required inputs or directory permissions are missing. It does not certify raster schemas, CRS,
source coverage, or binary compatibility. For a prepared land run use `--workflow land`; for an
opaque-land water run use `--workflow water`.

## Move from invented geometry to a real place

Read the [San Juan guided example](../examples.md) without installing anything. It uses committed
real-data model results and explains ground, canopy, sampled viewpoints and modeled support.
Then follow [reproduction](../documentation-examples.md) for a bounded real model with native GDAL
and available source inputs. That run has different data and scientific requirements from this
first distance result.

[Product dictionary](../products/artifacts.md) · [Troubleshooting](troubleshooting.md) ·
[Workflow choices](../workflows/index.md) · [Python and CLI](../api.md)
