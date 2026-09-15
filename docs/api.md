# Supported API

## Public package facade

The stable public API is the package root plus packaged configuration resources:

```python
from viewshed_toolkit import (
    DEFAULT_STAGES,
    STAGES,
    AppConfig,
    StageInvocation,
    StageSpec,
    ViewshedRequest,
    ViewshedRunResult,
    WorkflowIdentity,
    load_app_config,
    process,
    run_stage,
    run_viewshed,
    validate,
)
from viewshed_toolkit.resources import default_config_path
```

`load_app_config` reads and validates configuration without creating output directories:

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
