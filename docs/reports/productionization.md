# Initial component refactor validation snapshot

Date: 2026-09-15, before the subsequent regional case-study run.

For the current regional workflow, see [Salish Sea case study](../salish-sea-case-study.md).

## Outcome

The toolkit now exposes explicit acquisition, preparation, DEM weighting, conditional CHM
weighting, distance weighting, composition, finalization, validation, and map stages. The CLI
supports `build dem|chm|distance|all` and exact-stage execution. Registered USGS 3DEP and ETH
Global Canopy Height providers support independently configured inputs and checksum-aware caches.

This is an incremental implementation alongside the established production workflow. It has passed
synthetic integration and scientific regression validation. Full regional production qualification
has not been established.

## Scientific and artifact contracts

- Every component uses unique, non-null, valid `source_h3 × target_h3` keys at configured resolutions.
- The existing terrain kernel already includes observer-to-pixel distance attenuation. Composition
  keeps `static = weight_terrain × weight_vegetation`; multiplying centroid distance again would
  change the science. The separate centroid-distance factor remains inspectable.
- Conditional canopy uses the matched bare-earth kernel, with the existing neutral convention for
  terrain-blocked pairs. Water canopy is explicitly not applicable.
- Failed or incomplete LOS execution cannot silently turn absent pairs into observed zero.
- Sorted atomic tables and sidecars retain configuration/input fingerprints, source lineage,
  software/algorithm versions, CRS, grid, bbox, H3 resolution, and output checksums. Run manifests
  include resolved configuration, stages, timing, and completion/failure state.
- Existing paired land/water promotion and cleanup behavior remains unchanged. Supplemental
  component products live in a separate durable namespace and are not automatically promoted.

## Executed validation

| Check | Result |
|---|---|
| Baseline before changes | 202 tests passed |
| Final full suite | 222 tests passed; no skips |
| New workflow tests | 20 passed, including GDAL-backed synthetic integration |
| Scientific comparison | Separated land components agree with existing paired runner at absolute tolerance 1e-7 |
| Quality gates | Whole source/tests Ruff; expanded new-code Ruff; Black; strict mypy on 17 new source files; compileall; diff whitespace check |
| Package build | Wheel built from clean tracked/unignored source snapshot |
| Installed package | Import, CLI help, and complete synthetic workflow exercised outside the checkout |
| Map QA | Browser inspected synthetic H3 map; repeated export bytes verified identical |
| Regional preflight | Canonical bbox verified; missing inputs reported with nonzero exit |

The full suite emitted one existing GeoArrow/Polars extension warning; it did not skip GDAL paths.
Local execution used Python 3.14.6 and compatible GDAL 3.12.3 bindings. The installed wheel used an
isolated virtual environment inheriting those existing dependencies; a fresh dependency resolver
installation and the new GitHub Actions workflow were not executed.

## Remaining production boundaries

1. The canonical Salish Sea DEM, CHM, land polygons, and water polygons were absent. No regional
   downloads, full Salish Sea rebuild, real medium regional sample, field accuracy validation, or
   case-study notebooks were executed. Synthetic coordinates are not measured regional data.
2. Real provider discovery and existing backend wiring are tested, but live USGS/ETH acquisition
   was not exercised. Regional coverage, availability, licenses, and source versions need review
   with the intended data bundle before promotion.
3. New component materialization currently collects each source role's pair table eagerly. Full
   regional memory requirements are unverified; the synthetic timings do not establish scalability.
4. The target-cell artifact is inspectable, while the existing lookup still reconstructs/verifies
   classifications. The shared water build DAG still includes raster preparation dependencies;
   direct water component stages support the raster-free opaque-land policy.
5. Dataset and composition configuration are strictly typed. The existing wider configuration
   schema remains in place; this is not a wholesale migration of historical dictionaries. Existing
   distance models are preserved; no new Gaussian scientific model was introduced.
6. Separate land/water component runs use separate manifests. No combined atomic generation
   pointer or new component cleanup command was added. Legacy rollback/cleanup protections remain
   covered by the full suite; component stages perform no implicit cleanup.
7. Browser map display currently loads Leaflet dependencies from a CDN. Reproducible HTML bytes
   do not guarantee offline browser availability.

The next promotion gate is a pinned real regional input bundle, followed by medium/full-domain
resource profiling, output-equivalence checks, and inspection of coverage and unavailable states.
