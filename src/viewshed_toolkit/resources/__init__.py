"""Packaged configuration resources."""

import atexit
from contextlib import ExitStack
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path

_RESOURCE_FILES = ExitStack()
atexit.register(_RESOURCE_FILES.close)


@lru_cache(maxsize=None)
def resource_path(name: str) -> Path:
    """Return a stable filesystem path for a packaged configuration resource."""

    resource = files(__package__).joinpath(name)
    if not resource.is_file():
        raise FileNotFoundError(f"Missing packaged viewshed resource: {name}")
    return Path(_RESOURCE_FILES.enter_context(as_file(resource)))


def default_config_path() -> Path:
    """Return the self-contained Salish Sea example configuration."""

    return resource_path("salish_sea.yaml")


def common_areas_path() -> Path:
    """Return the named-area definitions used by the packaged example."""

    return resource_path("common.yaml")


__all__ = ["common_areas_path", "default_config_path", "resource_path"]
