# Scientific methodology

These products describe static observation geometry and viewability. They do not represent
observer effort, access, reporting capture, detection probability, whale occurrence, population
abundance, or ecology.

The refactor preserves the existing distance-integrated physical kernel, not the illustrative
three-factor product of independent centroid attenuation and LOS averages.

The diagrams in the [methodology guide](methodology.md) are conceptual. Where their simplified
labels differ from the equations below—especially the depicted extra centroid-distance
multiplication—these equations define the implemented scientific contract.

For matched observer samples `o` and target-water pixels `p`:

```text
K_bare   = mean(LOS_bare(o,p)   × D(distance(o,p)))
K_canopy = mean(LOS_canopy(o,p) × D(distance(o,p)))
C        = min(K_canopy, K_bare) / K_bare, when K_bare > 0
C        = 1, when K_bare = 0 (terrain-blocked neutral canopy factor)
K_static = K_bare × C
```

- `terrain_visibility` retains unweighted joint LOS support when supplied by the backend.
  `weight_terrain` remains the distance-integrated kernel for compatibility.
- `canopy_los_raw` is an intermediate surface-support diagnostic. The CHM table's
  `weight_vegetation` is the conditional canopy factor; it does not contain a final static product.
- `weight_distance` evaluates the configured curve at H3-centroid distance. It remains inspectable
  with `distance_m`, `distance_km`, and `distance_model`. It is not multiplied into `K_static` again.

The CHM surface remains `DEM + CHM`, with configured resampling, nodata, minimum canopy height,
and observer canopy-clearance assumptions. Separate execution uses the same backend and
sampling implementation as paired execution; fixture parity is tested at absolute tolerance
`1e-7`. The fixtures do not establish regional ecological or field-observation validity.

Water uses mapped land as an opaque blocker, deterministic water samples, configured observer
height/horizon support, and distance integration. Its canopy factor is 1 with
`canopy_support=not_applicable`. This is a policy state, not measured canopy transparency.

## Pair and missingness contract

Every per-role component has non-null, nonempty, unique source/target keys. Composition requires
identical pair coverage and validated 1:1 joins. Observed weights must be finite in `[0,1]`.
Missing components and stale provenance fail rather than becoming zeros or neutral factors.
The only sparse-to-zero conversion occurs after complete successful LOS source execution,
where the existing sparse partition contract defines absence as no visible support. Missing
unweighted LOS diagnostics remain null. An empty validated universe remains empty.

Distance changes affect the integrated terrain calculation and therefore invalidate its scientific
provenance even though the centroid-distance *artifact* is not an upstream dependency.
See the existing [detailed methodology](methodology.md) for sampling, curvature, denominator,
land-mask, and endpoint algorithms.
