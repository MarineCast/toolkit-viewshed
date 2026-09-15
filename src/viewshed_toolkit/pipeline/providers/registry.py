"""Extensible provider registration without orchestration conditionals."""

from ..config.datasets import register_provider_name
from .base import FileProvider, RasterProvider
from .global_canopy_height import GlobalCanopyHeightProvider
from .usgs_3dep import USGS3DEPProvider

_PROVIDERS: dict[str, RasterProvider] = {
    "local": FileProvider(),
    "usgs_3dep": USGS3DEPProvider(),
    "global_canopy_height": GlobalCanopyHeightProvider(),
}


def register_provider(name: str, provider: RasterProvider) -> None:
    if not name or name in _PROVIDERS:
        raise ValueError(f"Provider name empty or already registered: {name!r}")
    _PROVIDERS[name] = provider
    register_provider_name(name)


def get_provider(name: str) -> RasterProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown dataset provider {name!r}; registered: {sorted(_PROVIDERS)}"
        ) from exc
