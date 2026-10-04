# Quick start

Start by reading a result, then inspect the configuration before choosing a model workflow.

## 1. Follow a result without running code

Open the [San Juan guided example](../examples.md). Follow **Observer area A → Water area B**
through the seven questions. Compare ground and tree-height inputs, the sampled profile and
the pair-level values. The explorer filters stored results; it does not run LOS in the browser.

## 2. Inspect the packaged configuration

After [installation](installation.md), run this read-only Python example:

```python
from viewshed_toolkit import load_app_config
from viewshed_toolkit.resources import default_config_path

config = load_app_config(default_config_path())
print(config.region.bbox_wgs84)
print(config.config_hash)
```

Loading a configuration does not create output directories or start acquisition. Before a run,
review the [configuration guide](../configuration.md): region, geometry paths, providers,
resolution, physical assumptions, and separate working and durable output directories.

## 3. Choose your next operation

<div class="grid cards" markdown="1">

-   **Reproduce the bounded example**

    Run the real San Juan model with compatible native tools and available source inputs, then
    export its checked documentation derivatives.

    [San Juan model setup](../san-juan-demo.md) · [Export and checks](../documentation-examples.md)

-   **Build one component**

    Choose DEM, CHM or distance and its registered dependency closure. Review acquisition and
    lifecycle effects before calling a build target.

    [Pipeline stages](../pipelines.md) · [Python and CLI](../api.md)

-   **Reuse distance independently**

    Build distances from a validated lookup, then create profiles using only the persisted
    pair product and its sidecar.

    [Distance products](../distance-products.md)

</div>

The [workflow guide](../workflows/index.md) explains these choices. A full run is an explicit
operation with data and output requirements, rather than part of this read-only quick start.
