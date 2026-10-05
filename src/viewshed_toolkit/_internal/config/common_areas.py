"""Resolve shared named areas from packaged configuration resources."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from viewshed_toolkit._internal.config.data import load_data_config
from viewshed_toolkit._internal.config.paths import resolve_config_path
from viewshed_toolkit.resources import common_areas_path

DEFAULT_COMMON_CONFIG_PATH = common_areas_path()


def _common_areas(common_config_path: str | Path | None = None) -> Mapping[str, Any]:
    path = resolve_config_path(common_config_path or DEFAULT_COMMON_CONFIG_PATH)
    raw = load_data_config(path, domains=())
    areas = raw.get("areas")
    if not isinstance(areas, Mapping):
        raise ValueError(f"Common config is missing an areas mapping: {path}")
    return areas


def area_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("area", "area_name", "common_area", "common_area_name"):
            area = value.get(key)
            if area:
                return str(area)
    return None


def bbox_for_area(
    area_name_value: str,
    *,
    common_config_path: str | Path | None = None,
) -> dict[str, float]:
    areas = _common_areas(common_config_path)
    area = areas.get(area_name_value)
    if not isinstance(area, Mapping):
        raise KeyError(f"Unknown common area: {area_name_value!r}")
    bbox = area.get("bbox_wgs84")
    if not isinstance(bbox, Mapping):
        raise ValueError(f"Common area {area_name_value!r} is missing bbox_wgs84.")

    required = ("min_lon", "min_lat", "max_lon", "max_lat")
    missing = [key for key in required if key not in bbox]
    if missing:
        raise ValueError(f"Common area {area_name_value!r} bbox_wgs84 is missing {missing}.")
    out = {key: float(bbox[key]) for key in required}
    if out["min_lon"] >= out["max_lon"] or out["min_lat"] >= out["max_lat"]:
        raise ValueError(f"Invalid bbox_wgs84 for common area {area_name_value!r}: {out}")
    return out


def bbox_from_config(
    config: Mapping[str, Any],
    *,
    bbox_key: str = "bbox_wgs84",
    common_config_path: str | Path | None = None,
) -> dict[str, float]:
    area = area_name(config)
    if area:
        return bbox_for_area(area, common_config_path=common_config_path)

    bbox = config.get(bbox_key) or config.get("bounding_box") or config.get("bbox")
    if not isinstance(bbox, Mapping):
        raise KeyError(f"Missing area reference or {bbox_key}/bounding_box/bbox mapping.")
    nested_area = area_name(bbox)
    if nested_area:
        return bbox_for_area(nested_area, common_config_path=common_config_path)

    required = ("min_lon", "min_lat", "max_lon", "max_lat")
    missing = [key for key in required if key not in bbox]
    if missing:
        raise ValueError(f"bbox mapping is missing {missing}.")
    out = {key: float(bbox[key]) for key in required}
    if out["min_lon"] >= out["max_lon"] or out["min_lat"] >= out["max_lat"]:
        raise ValueError(f"Invalid bbox mapping: {out}")
    return out


def ranges_for_area(
    area_name_value: str,
    *,
    common_config_path: str | Path | None = None,
) -> tuple[tuple[float, float], tuple[float, float]]:
    bbox = bbox_for_area(area_name_value, common_config_path=common_config_path)
    return (
        (bbox["min_lat"], bbox["max_lat"]),
        (bbox["min_lon"], bbox["max_lon"]),
    )
