"""Read-only adaptation of explicitly selected shared reporting and source roles."""

from pathlib import Path
from typing import Any

from ..contracts.study import _data_artifact, load_study_membership


def runtime_memberships(raw: dict[str, Any]) -> dict[str, Any] | None:
    section = raw.get("shared_study")
    if section is None:
        return None
    if not isinstance(section, dict) or set(section) != {"config_path"}:
        raise ValueError("shared_study requires only config_path")
    return {
        role: load_study_membership(section["config_path"], role=role)
        for role in ("water_reporting", "land_source", "water_source")
    }


def apply_shared_study(raw: dict[str, Any], config_path: str | Path) -> dict[str, Any]:
    """Bind runtime hashes, physical context and source planning extent to one study."""
    if "shared_study_identity" in raw:
        raise ValueError("shared_study_identity is derived, not a user setting")
    section = raw.get("shared_study")
    if section is None:
        return raw
    if not isinstance(section, dict) or set(section) != {"config_path"}:
        raise ValueError("shared_study requires only config_path")
    selected = Path(section["config_path"]).expanduser()
    selected = (Path(config_path).parent / selected).resolve()
    raw = {**raw, "shared_study": {"config_path": str(selected)}}
    memberships = runtime_memberships(raw)
    target = memberships["water_reporting"]
    identity, manifest = target["study_identity"], target["manifest"]
    extent = manifest["producer_source_extents"]["viewshed"]
    buffers = identity["study_config"]["producer_buffers"]["viewshed"]
    viewshed = dict(raw.get("viewshed", {}))
    for field, value in (
        ("max_distance_m", buffers["line_of_sight_m"]),
        ("aoi_margin_m", buffers["aoi_margin_m"]),
    ):
        if field in viewshed and float(viewshed[field]) != float(value):
            raise ValueError("Viewshed distance/margin differs from shared source extent")
        viewshed[field] = value
    h3 = dict(raw.get("h3", {}))
    for field in ("source_resolution", "target_resolution"):
        if int(h3.get(field, 7)) != 7:
            raise ValueError("Shared Viewshed requires native R7")
        h3[field] = 7
    paths = dict(raw.get("paths", {}))
    for field, source in manifest["physical_context"].items():
        path, _ = _data_artifact(selected, identity, source["relative_path"], source["sha256"])
        if field not in {"land_polygon_path", "water_polygon_path"}:
            raise ValueError("Unknown shared physical context role")
        if field in paths:
            configured = (Path(config_path).parent / paths[field]).resolve()
            if configured != path:
                raise ValueError("Shared study physical context conflicts with configured paths")
        paths[field] = str(path)
    return {
        **raw,
        "viewshed": viewshed,
        "h3": h3,
        "paths": paths,
        "region": {
            **raw.get("region", {}),
            "bbox_wgs84": dict(
                zip(("min_lon", "min_lat", "max_lon", "max_lat"), extent["bbox_wgs84"], strict=True)
            ),
        },
        "shared_study_identity": {
            "config_sha256": identity["config_sha256"],
            "mask_sha256": manifest["mask_sha256"],
            "manifest_sha256": identity["study_config"]["grid_registry"]["manifest_sha256"],
            "membership_sha256": {
                role: value["artifact_sha256"] for role, value in memberships.items()
            },
            "source_extent_sha256": extent["sha256"],
            "requested_time": identity["requested_time"],
            "quantity": "static_physical_viewability",
        },
    }
