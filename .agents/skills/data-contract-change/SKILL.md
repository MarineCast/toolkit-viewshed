---
name: data-contract-change
description: Change viewshed schemas, pair keys, configuration, cache identity, provenance, or artifact compatibility with contract validation.
---

# data-contract-change

All paths and commands below are relative to the toolkit-viewshed checkout root.
Source/tests, explicit contracts and architecture remain authoritative.

## Configuration and path contracts

- `load_app_config()` is read-only: loading configuration must not create directories or write
  metadata. Runtime initialization is the boundary for output-directory and metadata setup.
- Resolve paths through the existing config/path helpers. Do not infer canonical artifact paths
  with ad hoc string concatenation.
- Preserve parity between `configs/salish_sea.yaml` and the packaged
  `src/viewshed_toolkit/resources/salish_sea.yaml` where tests require it.
- Treat configuration and scientific hashes as cache/provenance contracts. A change that affects a
  scientific result must invalidate reuse through the appropriate hash or metadata fields.
- Reject unknown configuration fields and invalid enum values rather than ignoring them.

## Data and artifact contracts

- Enforce non-null, unique `source_h3 × target_h3` keys **within each source role** before joins
  and writes. When combining land and water results, retain `source_type` as part of the row
  identity: a mixed coastal H3 cell can participate in both roles. A role-partitioned artifact may
  encode that identity in its path, but a cross-role table must carry it explicitly.
- Use explicit join-cardinality validation. Do not rely on row order or `keep="first"` to hide an
  upstream uniqueness violation when a table is expected to be one row per pair.
- Validate required columns, units, ranges, CRS, raster alignment, H3 resolution, and source type at
  module boundaries. Static weights must remain finite and bounded to `[0, 1]`; apply bounds only
  where the metric contract defines them.
- Preserve explicit state and provenance columns when values are unavailable or not applicable.
  Never fill unknown support with zero merely to make an aggregation complete.
- Keep land and water source policies distinct. In particular, production land terrain uses the
  paired bare-earth/canopy workflow; do not route it through the water-only single-surface path.
- Preserve deterministic sampling, batching, ordering, and serialization. If randomness is
  necessary, expose and record a stable seed.
- Prefer the existing atomic Parquet/raster writers. Durable artifacts should not become visible in
  a partial state.
- Keep metadata sidecars, schema versions, source fingerprints, checksums, configuration hashes,
  scientific hashes, and output filenames synchronized with the artifact they describe.
- Cache reuse is valid only when relevant inputs, grids, schemas, configuration, and fingerprints
  match. Reject stale or ambiguous caches instead of silently accepting them.
- Generated Parquet, raster, map, and local data products remain untracked unless the task includes
  a redistribution and licensing review.
