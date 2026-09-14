# Methodology

The pipeline models physical viewing support between an active source area and a target area.
Both sides use durable H3 identifiers, and the final table grain is one unique
`source_h3 × target_h3` pair.

The staged calculation is:

1. Build valid land and water source/target support and candidate pairs.
2. Sample observer points inside active source geometry.
3. Calculate clear-sky line of sight against a projected terrain surface, including configured
   observer and target heights, range, curvature, and nodata policy.
4. Calculate the distance diagnostic/kernel and conditional canopy attenuation.
5. Compose the configured factors on the canonical pair universe with explicit missingness and
   uniqueness checks.
6. Materialize compact H3 pair kernels and optional selected-source or aggregate maps.

The default Salish Sea run uses H3 resolution 7, full-pixel aggregation, a 30 km hard range,
bare-earth terrain plus a matched canopy surface, and deterministic source sampling. Exact
parameters and version names live in [`configs/salish_sea.yaml`](../configs/salish_sea.yaml).

## Interpretation

`weight_static_viewability` is bounded physical support, not a probability that a whale will be
seen or reported. Observer presence, public access, weather, daylight, platform activity,
reporting capture, animal presence, and ecological processes are distinct layers. Unknown or
unavailable support must remain missing or explicitly state-coded; it must not be silently
converted to zero.

Generated artifacts carry configuration/scientific hashes and sidecar metadata. Reuse is valid
only when the relevant source fingerprints, schemas, spatial grids, and configuration agree.
The migrated case-study artifacts satisfy checksum/schema/config-alignment checks, but that does
not establish that this checkout rebuilt them from raw inputs.
