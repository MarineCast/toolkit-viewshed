# Architecture

The package keeps its existing layers: CLI → typed Python API → stage registry → implementation.
`api/registry.py` owns both the legacy production sequence and the supplemental component DAG.
`run_components` resolves a dependency closure; `run_component_stage` executes exactly one stage.
Neither routes scientific work through argparse.

## Boundaries

| Layer | Responsibility |
|---|---|
| `config/datasets.py` | Strict dataset/provider settings and supported composition policy |
| `providers/` | Discover/download source assets; no mosaic, projection, LOS, or composition |
| `prepare/datasets.py` | Mosaic, clip, reproject, resample, and validate DEM or CHM |
| `prepare/area/` | Existing geometry, deterministic H3 roles, sampling, pair universe |
| `contracts/distance.py` | Standalone distance schemas, paths, checksum-backed persistence |
| `weights/distance/products.py` | Raw pair-distance promotion, reusable profiles, validation |
| `weights/components.py` | Independently persist terrain, conditional canopy, and distance |
| `finalize/composition.py` | Exact coverage joins, explicit formula, lineage validation |
| `contracts/components.py` | Paths, pair validation, fingerprints, atomic writes, metadata |
| `_internal/performance.py` | Generic timing/RSS/disk sampling without pipeline imports |

The refactor adds narrow modules around established calculations. It does not arbitrarily split
the large GDAL/LOS/runner modules or replace their algorithms. Existing import-cycle and layer
architecture tests remain active.

## Preserved scientific dependencies

Canopy obstruction uses absolute `DEM + CHM` elevations and the existing observer-grounding policy.
The conditional canopy factor also needs the matched DEM kernel as its denominator. Therefore
CHM acquisition/preparation is independent, while canopy weighting explicitly depends on DEM
weighting. Neither LOS product consumes the centroid-distance table. Both continue to use the
configured observer-pixel attenuation function inside the existing LOS aggregation.

Standalone pair distances and profiles form a narrow branch from the canonical pair lookup. Raw
distance identity includes only relevant vector/pair lineage and scientific contracts, not raster
settings. Profile identity depends on that raw scientific identity and normalized effective curve
parameters. The existing distance component adapts the default profile for old consumers; custom
profiles never enter static composition.

Water keeps its opaque-land-mask policy. Its direct DEM/terrain component stage does not require
raster input validation. The shared `build` DAG currently retains DEM preparation dependencies for
water too; use individual `stage` commands for a raster-free water computation.

## Compatibility and promotion

`DEFAULT_STAGES`, `run_viewshed`, and `process` retain the previous paired land workflow and
compact cleanup contract. Explicit component products occupy a separate durable namespace and
never overwrite that pair of legacy final outputs. Source-specific manifests make separate land
and water runs explicit. There is no new two-source atomic generation pointer.

Target-cell output is independently inspectable. The existing lookup builder still verifies and
reconstructs its role classifications using the same policy; it does not trust an unvalidated
external target-cell file. Further consolidation is possible after regional validation.
