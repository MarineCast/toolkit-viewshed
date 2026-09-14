# Supported API

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
```

Load a configuration before calling the typed workflow:

```python
from viewshed_toolkit import load_app_config, run_viewshed
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())
# result = run_viewshed(config)  # Executes all configured stages and writes artifacts.
```

`ViewshedRequest` accepts an optional `WorkflowIdentity`. If it is omitted, the service reads
the config's `provenance` section and otherwise uses reusable toolkit defaults.

Advanced code should import directly from the canonical implementation module rather than a
pass-through facade. Examples include:

```python
from viewshed_toolkit.pipeline.prepare.area.observers import build_observers_from_sample_points
from viewshed_toolkit.pipeline.visualization.data import aggregate_target_weight_sums
from viewshed_toolkit.pipeline.weights.terrain.runner import run_source_cells
```

The `viewshed_toolkit.pipeline` layout remains available for advanced stage-level use, but only
the package-root API is covered by the public compatibility contract. Anything under
`viewshed_toolkit._internal` is implementation support.

The equivalent command-line interfaces are `viewshed-toolkit` and
`python -m viewshed_toolkit`. Run `--help` to inspect stages and arguments without starting a
pipeline.
