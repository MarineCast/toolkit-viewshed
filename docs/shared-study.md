# MarineCast study v1 adapter

The optional shared study file sets coastal reporting selection, a requested calendar window,
R7 Viewshed grid, producer buffers, marine mask identity and portable Data root. The
toolkit remains standalone: it imports no sibling checkout, guesses no study-file
location and bundles its schema. Install the declared `jsonschema` dependency.

Select the file explicitly, or set `MARINECAST_STUDY_CONFIG`. Explicit selection wins;
an invalid explicit file fails rather than falling back to the environment:

```bash
viewshed-toolkit plan-study --study-config /path/to/config/study.v1.json
viewshed-toolkit validate-region --config /path/to/viewshed.yaml --study-config /path/to/config/study.v1.json
```

The global flag before a command is also supported. `plan-study` is read-only and
accepts proposed geometry. Every production CLI command, including map exports,
requires an approved domain with approval provenance and a validated registry.
Configuration loading creates no directories. The current coastal selection policy
approves waters within 12 nautical miles of the intended coast plus requested
inland waters; its rectangle is an acquisition/planning envelope. The materialized
coastal mask, source-relative geometry and registry remain pending. Policy approval
does not authorize production; these pending states fail the production gate even
if domain approval and nominal registry metadata are supplied.
`bbox_role`, `geometry_status` and `selection_policy` are mandatory as a complete
coastal contract for both planning and production. Omitted, null or malformed
fields fail before execution or export. This requirement applies only when a
shared study is selected; standalone YAML behavior is unchanged.

Python callers can select the same file without mutating process environment:

```python
from pathlib import Path
from viewshed_toolkit import load_app_config, ViewshedRequest, process

app = load_app_config(Path("viewshed.yaml"), study_config=Path("config/study.v1.json"))
request = ViewshedRequest(config=Path("viewshed.yaml"), study_config=Path("config/study.v1.json"))
# process(request) executes requested stages after the same production gate.
```

Typed stage and component APIs preserve the selected file when reloading stage YAML.
Execution rejects a study changed after loading `AppConfig`. Legacy `download-data`,
`download-dem`, canopy and land-cover acquisition wrappers reject shared mode: they
do not implement its input ownership and halo contract. Consequently the complete
legacy paired workflow is not yet executable in shared mode. Explicit component
download/prepare stages are the supported acquisition surface, subject to source
qualification and a separately reviewed acquisition/runtime budget.

## Roles, geometry and paths

The coastal contract requires a materialized qualified coastal-plus-inland
selection mask; its enclosing rectangle is not a
reporting-water selection. Eligible
land and water observers extend outward by the LOS distance (30 km in the example).
Native rasters cover source-to-target paths and the additional AOI margin (1 km).
Reporting geometry comes from the explicit qualified mask. Observer and native
extents buffer that geometry in the configured metric CRS. The rectangle hash
identifies only the planning envelope; it supplies no reporting-water fallback.
Standalone configurations keep their established source rectangle and target halo.

Shared YAML requires explicit `paths.reporting_water_polygon_path`,
`water_polygon_path`, `land_polygon_path`, `regional_dem_path` and
`canopy_height_path`. Reporting water is distinct from native/source water. Only
the reporting mask must match registry `mask_sha256`; this consumer supports
`sha256_exact_file_bytes`. Native source-water polygons support outside observers
and endpoint surfaces; native mapped land supports intervening obstructions.
There is no shared land/water complement or edge-open-water fallback.

The owner-pinned `registry-artifact-interface.v1.json` defines membership files:
ASCII lowercase canonical H3 IDs, sorted uniquely, one per LF-terminated line,
without a header. SHA256 identifies exact bytes. The single R7 water_reporting
entry resolves from the selected study directory and must remain under Data.
The reader validates count, resolution, hash and exact equality with positive
WGS84 ellipsoidal-area intersections of the qualified reporting mask. It uses
H3 4.4 or later overlap enumeration; cell-center fill and zero-area touches are
excluded. Mask and membership bytes are checked again before cached reuse. The cache binds
all transported study validation metadata, including the planning envelope, and
revalidates envelope containment before returning a warm cached result.

Mixed cells may retain both observer roles. Shared source eligibility retains
all positive mapped land/water intersections in the source extent, including
observers outside reporting water. It overrides legacy fractional eligibility
thresholds and does not apply the standalone 6 km coastal-distance filter.
Empty mapped reporting land is valid when outside land observers exist.

Both land-cell entrypoints validate current marine-mask bytes before cache reuse.
Shared land-cell receipts additionally bind configuration, mapped land and water
inputs and output checksums under `land_source_cells_shared_v2`. Missing or stale
receipts fail and require an explicit rebuild. Standalone filtering and cache
behavior retain the established legacy contract.

Relative input paths and local-provider assets resolve from `storage.data_root`,
itself relative to the selected study file. A leading `data/` is removed from legacy
relative input paths. Absolute paths can reference retained inputs read-only.
Mutable work is routed to `Data/viewshed/work/<config_sha256>` and prospective
outputs to `Data/viewshed/generations/<config_sha256>`. Preparation cannot overwrite
retained inputs outside the owned Viewshed root. Shared input preparation requires
owned native destinations configured explicitly; it does not silently copy or rebuild
external retained rasters.

## Provenance and validation boundary

One read snapshot provides the raw-file SHA256, canonical sorted compact JSON SHA256,
and canonical geometry SHA256. Artifacts record domain revision/status/approval,
requested dates, buffer policies and grid registry. The full study hash namespaces
work and outputs; requested dates do not alter the static physical equations.

The requested 2009–2026 calendar is not evidence of daily coverage or source vintage.
The retained CHM describes 2020, not a reconstruction of 2009. Native scientific
contracts, role-specific pair completeness, nulls and missing-canopy policies remain
in force. Sums describe aggregate static support, never probability or activity.

The functional consumer reads owner-format membership artifacts and enforces
exact reporting membership. Geometry-only preparation can inspect eligible source
and target roles. Before physical shared stages or canonical raster creation,
read-only qualification requires native land/water union coverage through the
31 km path extent, native water coverage of reporting water, declared raster CRS
and complete actual affine raster footprints (including rotation/skew), with
explicit uncovered-path accounting, finite non-null mapped-land DEM/CHM pixels,
ISO `source_date` or a source compilation interval for DEM, DEM `vertical_reference` and metre units, and canopy
`height_reference=above_ground` and metre units. Canopy may declare either an exact
ISO `source_date` or an explicit four-digit `source_year` with annual precision.
Annual metadata records a half-open calendar interval and leaves `source_date` null;
its interval start is not an asserted acquisition day. Conflicting year/date metadata
or a declared precision inconsistent with the selected date field fails qualification. ETH sentinel 255 remains missing.
These conservative gates do not repair missing canopy or infer source provenance
from requested dates. The raster tags are required qualification declarations,
not independent proof that a vertical datum transformation or source date is correct;
qualified producer receipts and independent source review are still required.
Current prepared inputs lacking these declarations cannot run physical stages.

Native-water endpoint/canopy surfaces and reporting-water aggregation masks are
separate aligned rasters. The water kernel retains native land outside reporting
water as an obstruction and samples water observers from source water. Target
water-area denominators use reporting-water geometry under the established
native area method; LOS equations and standalone behavior are unchanged.

Real coastal geometry, registry artifacts, source/vertical-reference qualification,
resource approval and regional independent validation remain release gates. Neither
synthetic tests nor historical pair parity establish regional readiness. No immutable
Data release is published by this consumer change.

DEM compilations may declare `source_time_precision: interval`, canonical ISO
`source_start_date` and `source_end_date` (both inclusive), with no `source_date` or
`source_year`. Both ordered endpoints are required. The receipt retains the interval and
null exact date, explicitly qualifying that spatial source epochs may differ. Pin individual
asset metadata and its temporal coverage in provenance; a compilation envelope does not
establish a common observation day or current terrain everywhere. Publication/download dates
are not source observation dates. This representation does not relax vertical-reference,
metre-unit, native geometry or mapped-land pixel-coverage validation.
