# Reproduce the opt-in building obstruction pilot

This research workflow is supported through the public
`rasterize_building_footprints`, `compose_above_ground_surface` and
`above_ground_visibility` helpers. It is deliberately separate from production
`run_viewshed` and the raster-provider registry: it does not download GBA, register
new production stages, or declare regional readiness. The application owns bounded
input acquisition, footprint inventory containment, fixed source/target sampling,
resource supervision, checksummed manifests and product promotion.

## Required retained inputs

Use the same north-up 30 m EPSG:32610 grid for bare terrain, ETH 2020 maximum canopy,
canopy unknown mask and full-supported-water mask. Check all shapes, affine transforms
and CRS before reading arrays; do not silently warp or resize masks. Use a qualified
GBA SQLite subset with original EPSG:3857 WKB and official `source + id + region`
height linkage, preserving the reader's height state and separate footprint license.
The immutable full-source hashes and subset SHA256 must match the acquisition manifest.
The entire source-relative 31 km ray-grid perimeter, densified before transformation,
must be contained in the filtered study bbox. A regional bbox inventory is not a
claim of regional source completeness.

The qualified research pilot pins GBA.LoD1 revision
`9da24b3a8dce436a7420d5c3589de718d7ba14d6` and GBA.ODbLPolygon revision
`ddf775455af217f9909593c090a52e67be887e7a`. Its bbox is
`[-129.7, 45.9, -121.5, 51.5]`. Local source vectors are not distributed with code.
GBA primarily represents 2019 imagery with 2018 fill; ETH canopy represents 2020.
Neither this workflow nor their combination reconstructs annual 2009–2026 conditions.

## Portable caller recipe

The following recipe consumes caller-qualified inputs. `stack` is an existing raster
with authoritative `shape`, `transform`, `crs` and `bounds`. `ground`, `canopy`,
`canopy_unknown`, `qualified_water`, and integer `target_codes` are already aligned
arrays. `targets` is the canonical target-code lookup. `observers` is the retained,
ordered list of actual projected land-source samples; do not substitute cell centroids.
Run a single site per resource-supervised process. No source data or replay copies are
required in the repository.

```python
from pathlib import Path
import math
import numpy as np
from pyproj import Transformer
from rasterio.transform import xy
from viewshed_toolkit import above_ground_visibility, rasterize_building_footprints
from viewshed_toolkit.pipeline.providers.global_building_atlas import iter_gba_subset

# Caller verifies the manifest hashes and complete ray-grid containment first.
# Derive a conservative EPSG:3857 candidate bbox by densifying all grid edges.
bounds_3857 = Transformer.from_crs(stack.crs, 3857, always_xy=True).transform_bounds(
    *stack.bounds, densify_pts=101
)
records = iter_gba_subset(Path(subset_path), bounds_3857)
buildings = rasterize_building_footprints(
    ((record.geometry_wkb, record.height_m) for record in records),
    shape=stack.shape, transform=stack.transform, crs=stack.crs,
)
def clearance_mask_for(observer, stack, radius_m):
    row, col = stack.index(observer.x, observer.y)
    radius_cells = math.ceil(radius_m / stack.transform.a)
    mask = np.zeros(stack.shape, dtype=bool)
    mask[row, col] = True
    for r in range(max(0, row-radius_cells), min(stack.height, row+radius_cells+1)):
        for c in range(max(0, col-radius_cells), min(stack.width, col+radius_cells+1)):
            x, y = xy(stack.transform, r, c, offset="center")
            if math.hypot(x-observer.x, y-observer.y) <= radius_m:
                mask[r, c] = True
    return mask

counts = np.zeros((3, len(targets)), dtype=np.int64)  # unknown, blocked, clear
for observer in observers:
    row, col = stack.index(observer.x, observer.y)
    # Construct the retained private 30 m canopy-clearance mask in projected metres.
    # This is a declared source model assumption, not a building absence certificate.
    canopy_clearance = clearance_mask_for(observer, stack, radius_m=30)
    states = above_ground_visibility(
        ground, canopy, canopy_unknown, buildings,
        qualified_water=qualified_water, observer_row=row, observer_col=col,
        pixel_size_m=30, canopy_clearance_mask=canopy_clearance,
        grounded_observer=True, observer_height_m=1.7, target_height_m=1.0,
        curvature_coefficient=0.85714, earth_radius_m=6378137,
        max_distance_m=30000,
    )
    selected = target_codes >= 0
    np.add.at(counts, (states[selected] + 1, target_codes[selected]), 1)
```

Production callers can process target rows in strips to bound temporary allocations.
The retained two-site pilot uses five fixed land-source points per H3 source cell and
all actual 30 m water target centers (stride one), including blocked and out-of-range
comparisons. Exact-point diagnostics use one observer and remain secondary. Preserve
unique source/target keys and the exact target-code lookup before joining counts.
Arithmetic visibility is `clear/(clear+blocked+unknown)`, null if the population is
empty or unknown is nonzero. The known-only diagnostic is `clear/(clear+blocked)`;
full-population bounds are `clear/total` to `(clear+unknown)/total`. Do not replace
unknown with zero or reweight these comparisons to create positives.

The package rasterizer projects with `always_xy=True`, records projected polygon
repairs, burns every touched pixel at the maximum overlapping valid AGL prediction,
and ORs overlapping missing-height support. Feature order cannot change heights or
support. Outside published footprints on land, the lower bound is zero and the upper
height remains unknown. A predicted zero is a value, not verified building absence.
Composition is `max(canopy_lower, building_lower)` above terrain; upper uncertainty is
the union of component unknown masks. Full-water qualification may resolve zero,
except building/water contradictions remain unknown. The LOS helper retains known
height floors beneath uncertain components. Its central-cell grounding is explicit;
private clearance changes canopy only and never neighboring building heights.

Retain terrain-only, canopy-only and building-only diagnostics. The existing
`weight_vegetation` stays canopy-only. The new `weight_obstruction` conditional factor
contains canopy and buildings once. Its distance-integrated combined kernel uses the
retained equivalent-water-area denominator and a bare-terrain upper bound; never
multiply distance or a separate canopy factor again. Missingness intervals condition
on supported predicted heights; provider variance is not a calibrated confidence
interval. Include model/grid/source/geometry hashes in a new scientific signature.

## Rights and verification boundary

[GBA](https://github.com/zhu-xlab/GlobalBuildingAtlas) heights and non-ODbL footprints
carry [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/). ODbL footprint
geometry retains [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Preserve
per-feature source identity and license split, including the one non-ODbL retained
Victoria footprint. Existing ETH/terrain/FABDEM attribution travels with their inputs;
GBA does not replace those terms. Source vectors remain outside Git and the pilot ZIP.

Raster and caller regressions verify max instead of sum, feature-order independence,
reprojection, missing overlaps, empty inventories, zero predictions, grid rejection,
explicit grounding and preservation of adjacent roofs under canopy clearance.
The published pilot is a conditional model, not field verification of buildings,
canopy porosity, open ground, tidal heights, or urban completeness.
