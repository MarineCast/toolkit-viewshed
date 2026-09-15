# Runtime telemetry

Each component build records wall time, parent CPU time, sampled peak process-tree RSS,
work-directory disk usage, and applicable row/source/pair throughput in its run manifest.
Sampling uses a 50 ms interval. Short peaks can be missed; CPU excludes children and disk usage
covers only the configured work directory. Unavailable memory is null, with partial/scope flags.

The package uses Python, GDAL, and Numba. No Rust extension or benchmark suite is included.
Regional run telemetry describes the executed configuration and is not a general performance claim.

Regional execution avoids repeated work while preserving numerical contracts:

- Batch packing reuses candidate merge scores, with exact equivalence to exhaustive greedy packing.
- Input checksum reuse checks device, inode, size, modification time, and change time; changed files are fully reread. Durable output validation still calculates full checksums.
- Water-source denominator conversion is limited to candidate targets, and cached per-source result tables are bounded.
- Component composition releases each input after its join to limit peak regional memory.

Batch shutdown removes only empty shared scaffolding. A process must not delete another source
role's active raster directory. Each completed batch retains responsibility for its own scratch
files, and failed preparations remain available for diagnosis.

Observer-specific canopy clearance uses a GDAL VRT overlay and a small private patch in the
configured batch directory. It avoids copying the full obstruction raster for every observer.
Regression checks compare every raster pixel and GDAL visibility output against the full-copy
implementation, including edge observers, clearance radii, nodata, and unchanged shared inputs.
