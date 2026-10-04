# API overview

Use the package-root Python facade or the command line for supported operations. The CLI presents
the same typed APIs and registered stage functions.

| Task | Python entry point | Details |
| --- | --- | --- |
| Read configuration | `load_app_config` | [Read-only loading](../api.md#public-package-facade) |
| Execute one canonical stage | `run_stage` with `StageInvocation` | [Selected stages](../api.md#running-selected-stages) |
| Run the established complete workflow | `run_viewshed` | [Lifecycle and outputs](../pipelines.md) |
| Execute selected stages with a run manifest | `process` with `ViewshedRequest` | [Manifest-backed orchestration](../api.md#running-selected-stages) |
| Build explicit components or one component stage | `run_components`, `run_component_stage` | [Component API](../api.md#explicit-component-api) |
| Create and validate standalone distance products | `build_pair_distances`, `build_distance_profile`, `validate_distance_product` | [Distance API](../distance-products.md#python-api) |

## Inspect before executing

```bash
viewshed-toolkit --help
viewshed-toolkit stage --help
```

Help and configuration loading are read-only. Build calls may acquire inputs, write outputs and
follow the registered dependency closure. Choose dedicated output directories and review the
[workflow contracts](../pipelines.md#workflow-contracts) before calling a complete run.

The [Python and CLI reference](../api.md) contains exact signatures, examples, compatibility
boundaries and advanced imports. Start with the [quick start](../getting-started/quick-start.md)
if you want to inspect a configuration first.
