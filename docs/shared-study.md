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
Constant latitude/longitude edges are densified at at most 0.02 degrees before
projected buffering; geometry identity remains the five-vertex semantic rectangle.
Standalone configurations keep their established source rectangle and target halo.

Shared YAML requires explicit `paths.water_polygon_path`, `land_polygon_path`,
`regional_dem_path` and `canopy_height_path`. The actual marine-water file must match
the registry's raw-byte `mask_sha256`. No land or marine-water complement fallback
is allowed. Mixed cells may have both source roles; positive marine-water area is
required for targets. Land remains the declared mapped land, including observers
outside the reporting rectangle. Shared land filtering uses the explicit marine
mask rather than interpreting all unmapped areas or freshwater as marine water.
Shared source eligibility retains every positive mapped land intersection within
the declared source extent. It overrides the legacy fractional eligibility
thresholds and does not apply the standalone 6 km coastal-distance filter.
An empty declared land intersection in an all-water reporting rectangle is valid;
it does not prevent mapped land observers in the outside source halo.

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

The adapter validates registry metadata and binds the native mask bytes; it does not
yet read registry membership artifacts to verify row counts or enforce exact shared
H3 membership equality. The shared contract does not specify their artifact format.
That consumer integration, source/vertical-reference qualification, raster coverage,
separation of reporting selection from native/source-water geometry,
resource approval and regional independent validation remain release gates. Neither
synthetic tests nor historical pair parity establish regional readiness. No immutable
Data release is published by this adapter change.
