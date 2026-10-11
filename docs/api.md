# Supported API

## Public package facade

The stable public API is the package root plus packaged configuration resources:

```python
from viewshed_toolkit import (
    DEFAULT_STAGES,
    STAGES,
    AppConfig,
    DistanceProfile,
    StageInvocation,
    StageSpec,
    ViewshedRequest,
    ViewshedRunResult,
    WorkflowIdentity,
    build_distance_profile,
    build_pair_distances,
    load_app_config,
    process,
    run_stage,
    run_viewshed,
    validate,
    validate_distance_product,
)
from viewshed_toolkit.resources import default_config_path
```

`load_app_config` reads and validates configuration without creating output directories:

An optional `study_config=Path(...)` selects the portable MarineCast study v1 contract;
`ViewshedRequest` accepts the same field. See [shared-study selection, roles and gates](shared-study.md).

```python
from viewshed_toolkit import load_app_config
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())
print(config.config_hash)
```

The main calls have distinct scopes:

| Call | Behavior |
| --- | --- |
| `load_app_config(path)` | Read-only configuration loading and normalization. |
| `run_stage(config, invocation)` | Execute one registered stage; writes that stage's outputs. |
| `run_viewshed(config)` | Execute every canonical stage and retain the durable static outputs. |
| `process(request)` | Execute selected stages, validate expected paths, and write a run manifest. |
| `validate(artifacts)` | Check that referenced artifact paths exist; it is not a schema or scientific validation. |

Both `run_viewshed` and a complete `process` request clean the viewshed directory down to its
durable static-output contract after successful stage execution. Use a dedicated output directory
when experimenting.

## Running selected stages

`StageInvocation` is the typed, parser-free way to call one stage:

```python
from viewshed_toolkit import StageInvocation, load_app_config, run_stage

config = load_app_config("configs/salish_sea.yaml")
result = run_stage(
    config,
    StageInvocation(
        "build-distance-weights",
        source_type="land",
        overwrite=False,
    ),
)
```

The canonical stage order is:

1. `download-data`
2. `build-land-cells`
3. `prepare-source-target-lookup`
4. `build-distance-weights` for land and water
5. `build-dual-surface-canopy-weights`
6. `terrain-weight` for water
7. `build-vegetation-path-weights` for water
8. `finalize-viewshed-lookups`
9. `export-static-maps`

`STAGES` and `DEFAULT_STAGES` expose these canonical stage names. Some stages expand into separate
land and water invocations through the registry.

For resumable, manifest-backed orchestration:

```python
from pathlib import Path

from viewshed_toolkit import ViewshedRequest, process

result = process(
    ViewshedRequest(
        config=Path("configs/salish_sea.yaml"),
        stages=("build-land-cells",),
        run_id="land-cells-local",
        resume=True,
    )
)
```

Run IDs must be simple filename identifiers (no path separators, drive prefixes or traversal).
Manifest collision checks run before any stage. `resume=True` does not override a different logical
run; explicit `force=True` is required for manifest replacement. Complete resume validates the
retained paired generation and present inputs before skipping. Partial resume executes stage cache
checks and does not return `skipped=True` from output checksums alone.

The distance CLI commands reject `--limit` and `--start`; bound the configuration instead.
`terrain-weight` retains intermediates and no longer advertises a redundant retention flag.
Legacy vegetation support/ray commands remain available for advanced compatibility work; canonical
land production uses matched bare-earth/canopy `terrain-weight` or the paired stage. Legacy output
is not a substitute for current producer lineage.

`viewshed-toolkit doctor --config <file> --workflow land|water|distance` is read-only local readiness
inspection; see [quick start](getting-started/quick-start.md). It exits 1 for missing required inputs
or unavailable output permission hints, and does not certify scientific validity.

`ViewshedRequest` accepts an optional `WorkflowIdentity`. Without one, the service reads the
configuration's `provenance` section and otherwise uses toolkit-generic defaults.

## Canonical advanced imports

Advanced code should import directly from the module that owns the implementation:

```python
from viewshed_toolkit.pipeline.prepare.area.observers import build_observers_from_sample_points
from viewshed_toolkit.pipeline.visualization.data import aggregate_target_weight_sums
from viewshed_toolkit.pipeline.weights.terrain.runner import run_source_cells
```

The old pass-through modules are intentionally absent:

```text
viewshed_toolkit.aggregation
viewshed_toolkit.observers
viewshed_toolkit.raster
viewshed_toolkit.terrain
viewshed_toolkit.validation
viewshed_toolkit.viewshed
viewshed_toolkit.visualization
```

Only the package-root facade is covered by the public compatibility contract. The
`viewshed_toolkit.pipeline` tree is available for advanced stage-level use, while anything under
`viewshed_toolkit._internal` is private implementation support.

## Command line

The following entry points are equivalent:

```bash
viewshed-toolkit --help
python -m viewshed_toolkit --help
```

Use `<command> --help` to inspect a stage without running it. CLI commands are presentation layers
over the same canonical stage functions; registered compatibility command names may appear in
`--help`, but they are not additional Python APIs.

## Standalone distance-product API

The package root exports a small typed interface for raster-free distance reuse:

```python
from viewshed_toolkit import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    validate_distance_product,
)

pair_path = build_pair_distances("configs/salish_sea.yaml", source_type="land")
profile_path = build_distance_profile(
    pair_path,
    DistanceProfile(
        "sensitivity-5km",
        selected_model="exponential",
        exponential_lambda_km=5.0,
        hard_cutoff_km=20.0,
    ),
)
record = validate_distance_product(profile_path)
```

`build_pair_distances` consumes the validated canonical pair lookup and vector provenance.
`build_distance_profile` consumes only a validated pair-distance Parquet artifact and its sidecar.
`validate_distance_product` checks either product without a complete static workflow. The CLI
equivalents are `build-pair-distances`, `build-distance-profile`, and
`validate-distance-product`; see [distance products](distance-products.md).

The profile object is deliberately separate from the integrated `distance_weight` configuration.
Creating a profile cannot change terrain/canopy LOS. `run_components(..., target="distance")`
continues to build the raw product and the default compatibility `distance_weights.parquet`.

## Explicit component API

The package root also exports `run_components` and `run_component_stage`:

```python
from viewshed_toolkit import load_app_config, run_component_stage, run_components

app = load_app_config("configs/salish_sea.yaml")
run_component_stage(app, "download-dem")       # acquisition only
run_component_stage(app, "prepare-dem")        # existing downloaded inputs only
run_component_stage(app, "build-dem-weights")  # prepared DEM and pair lookup required

outputs = run_components(app, target="all", source_type="land", run_id="land-components")
```

Both accept a config path or `AppConfig`. `run_component_stage` returns the stage's output path;
`run_components` returns a stage-name-to-path mapping and writes a durable run manifest with
status, checksums, and sampled performance metrics. Valid build targets are `dem`, `chm`,
`distance`, and `all`. `overwrite=True` explicitly rebuilds requested stages.

Advanced provider implementations use `pipeline.providers.RasterProvider`, `Asset`,
`DownloadResult`, and `register_provider`. Dataset settings use `pipeline.config.datasets`.
Provider registration must precede loading a configuration that names the new provider.

The component API has no implicit cleanup. Its final tables and maps use a separate namespace;
the original public `process` and `run_viewshed` retain the legacy paired/cleanup contract above.
See [pipeline stages](pipelines.md) for exact dependencies and [configuration](configuration.md)
for the intentionally preserved scientific model.

Partial-run manifests use schema version 3 to record canonical local input checksums, including
missing inputs and shapefile sidecars. Partial resume rejects changed/missing input identity and
historical manifests without that inventory before entering stage checks. It does not silently
certify older cached outputs. Rebuild affected producers with their explicit overwrite controls
or a fresh workspace, then create a new manifest. Remote source freshness is not certified by
local checksum validation.

## Bounded DEM preparation

The existing source runner can select `batch.raster_stack_mode: windowed`; cross-CRS/resolution
inputs additionally require a pinned producer chunk receipt or an explicitly built and pinned
native metadata planner. Canopy alignment has its own maximum-resampling receipt. This opt-in preserves the established global warp processing
contexts and crops only requested LOS windows. See [bounded native windows](BOUNDED_WINDOWS.md)
for qualification, receipt provenance, helper build instructions and resource-limit requirements.
No acquisition or full reference warp runs implicitly, and default global behavior is unchanged.

## Bounded one-source engineering pilot

`viewshed_toolkit.pipeline.api.pilot.run_bounded_land_pilot` is an advanced POSIX-only API
for prepared and qualified paired land configs. Supply `bare_config`, `canopy_config`, one
`source_h3`, an existing owned `staging_root`, and a fresh child `checkpoint_dir`; optionally
supply the shared `study_config` and stricter `PilotCaps`. It performs no acquisition or final
promotion, retains checkpoints, and rejects output paths outside staging or preexisting manifests
and partitions. Keep batch intermediates, disable overwrite/combine/maps, use one worker and
windowed rasters. Candidate lookup validation is filtered and bounded to 6,001 keys.

A completed subprocess is not a validated product. Input qualification is unchanged, and the
caller must independently verify all paired outputs before publishing. See
[bounded native windows](BOUNDED_WINDOWS.md) for ceilings, sampling limitations and scientific
qualification. This advanced API is deliberately not exported from the package root.


### Missing values in map summaries

Map value contract `nullable_coverage_v1` preserves unknown factors as null in Parquet,
GeoJSON and HTML. For each sum column, the main value requires complete applicable-pair
coverage. A partial total remains null; separately named `*_available` and `*_mean_valid`
columns describe only available contributions. Companion `*_valid_count`, `*_missing_count`,
`*_applicable_count`, `*_not_applicable_count`, `*_coverage_fraction` and `*_status` fields expose the denominator.
Valid computed zeros count in the mean. With no valid applicable values, sums and means are
null. Neutral vegetation factors for water sources or zero bare-earth baseline are not
vegetation observations and are excluded. Unknown baseline support leaves vegetation unknown.
Source and target summaries use the same rules and keep source roles separate.

Native H3 layers show missing or inapplicable values with gray dashed cells and expose their
status and coverage on hover. Computed zero remains transparent with a numeric zero. Smoothed
surfaces stay within positive known H3 support; they do not estimate missing cells. Older map
manifests without this value contract require explicit regeneration with overwrite.

These are presentation rules for the supplied candidate rows. They cannot detect omitted
candidates, certify raster coverage, or enable partially available canopy LOS in the compact
finalizer. Existing source policies, finalization requirements and scientific hashes are unchanged.


## Retained sample comparison maps

`viewshed_toolkit.export_comparison_maps(payload, output_directory)` exports offline target,
source and selected-source maps plus `SOURCE_CELL_COMPARISON_MEANS.csv`. The payload contains
`viewpoints` pair rows, `composite_sources` and display `source_polygons`, coastline and bounds.
Each pair requires actual integer `n_observers`, `target_sample_count`, clear/blocked/unknown
canopy counts and `bare_clear_observations`; duplicate or null source/target keys are rejected.
The actual count population must equal observers × target samples. For this retained pilot,
these are five land source samples × all retained 30 m water raster centers.

This export is separate from the production equivalent-area kernel: arithmetic full mean is
clear / total only if unknown=0 and total>0; otherwise null. Bounds are clear / total to
(clear+unknown) / total. Known-only mean clear / (clear+blocked) is descriptive and never
estimates unknown comparisons. Empty target populations have null metrics and are excluded
from sums and equal-cell means. Source-cell rows carry observer_scope=source_cell_samples
and no exact-point viewpoint_id. Original weighted Parquets remain unchanged.

Target maps group candidate source pairs per target; source maps group target pairs per source.
Selected-source maps default to the five-observer composite with Victoria/Dungeness choices,
with exact points secondary. Count pooling weights each comparison equally; sums add per-pair
metrics and equal-H3 means average known nonempty pair metrics. Full sums stay null with
unknown support. These statistics differ when target populations differ. Bounds use the same
sum/mean choice. Terrain-only arithmetic uses retained bare binary counts; FABDEM exact-point
scenarios expose distance-integrated weight_terrain and are explicitly labeled weighted,
rather than claiming a supported arithmetic mean. No acquisition or LOS rerun occurs.


Research callers may opt into full supported water-cell qualification through
`pipeline.prepare.elevation.canopy.qualify_water_footprints` and the explicit
`qualified_water=` argument to `qualify_partial_canopy_grid`; see the
[scientific contract](scientific-methodology.md#opt-in-supported-water-footprint-qualification).
The caller must align geometry, raster/support grids, and the declared water elevation surface.
This does not execute acquisition or automatically replace production outputs.

The comparison-map combined lower/upper metric choices visibly lock View to the corresponding bound. Select Combined to choose coverage, resolved values, or either bound independently.

## Opt-in above-ground obstruction helper

```python
from viewshed_toolkit import compose_above_ground_surface

surface = compose_above_ground_surface(
    canopy_lower, canopy_unknown, building_lower, building_unknown,
    qualified_water=qualified_water, building_present=building_present,
)
```

All arrays must share a projected grid. The result exposes `lower_height`, `unknown`,
`qualified_water` and `water_conflicts`. Inputs are not mutated. Missing footprint support must
remain unknown unless separately qualified. For LOS, pass `surface.lower_height` as both the
height input and `unknown_height_lower`, and `surface.unknown` as the unknown mask. Apply canopy
clearance to the canopy input before composition; a clearance mask on the combined surface would
also erase buildings and is inappropriate. See the [research contract](scientific-methodology.md#opt-in-building-obstruction-research-contract).

`rasterize_building_footprints` streams original EPSG:3857 footprint WKB/AGL-height
pairs onto a caller-owned projected metre grid. It returns maximum lower heights,
footprint presence, missing-height and unknown masks plus processing counts.
`above_ground_visibility` applies private clearance only to canopy before joint
composition, preserving adjacent roofs. `grounded_observer=True` is an explicit
central source-cell grounding assumption. These helpers perform no acquisition,
sampling, distance weighting or production promotion. See the
[portable research caller recipe](building-obstruction-pilot.md).

For offline comparison input inspection, `export_raster_overlay_asset` writes a
shared local classic-script asset from caller-qualified aligned float32 terrain
and lower-height arrays plus raw uint8 CHM and support flags. It preserves native
values, adds a display-only WGS84 preview, and rejects unqualified grids. See
[the building pilot recipe](building-obstruction-pilot.md#inspect-the-actual-pilot-input-grids)
for units, missingness, observer assumptions and packaging requirements.
