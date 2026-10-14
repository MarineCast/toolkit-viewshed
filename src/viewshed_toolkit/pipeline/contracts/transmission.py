"""Versioned direct-transmission identity and immutable input evidence."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import polars as pl

from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file

TRANSMISSION_CONTRACT = "viewshed_direct_transmission_v2"
PREPARED_CONTRACT = "viewshed_direct_transmission_prepared_v3"
OBSERVER_GROUND_CONTRACT = "unflattened_land_observer_ground_v1"
RADIUS = 24140.16
DTYPES = {
    "ground": "float32",
    "observer_ground": "float32",
    "canopy": "uint8",
    "canopy_unknown": "bool",
    "building": "float32",
    "building_unknown": "bool",
    "building_present": "bool",
    "qualified_water": "bool",
    "classes": "uint8",
}


def digest(path: Any) -> Any:
    """Raw byte SHA256, distinct from filename-prefixed repository checksums."""
    return checksum_unchanged_file(path, raw_bytes=True)


def payload_hash(payload: Any) -> Any:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def implementation_identity() -> Any:
    """Include all package code and scientific resources, including curve/default owners."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".json", ".cpp"}
    )
    return {str(p.relative_to(root)): digest(p) for p in paths}


def input_identity(config: Any) -> Any:
    """Verify provisioned assets and every referenced tile/VRT before execution."""
    assets = config["inputs"]
    pins = {str(Path(a["path"]).resolve()): a["sha256"] for a in assets.values()}
    for key, array in [("canopy_inventory", "raw_canopy"), ("worldcover_manifest", "assets")]:
        entries = json.loads(Path(assets[key]["path"]).read_text())[array]
        for entry in entries:
            path = Path(entry["path"])
            if not path.is_absolute():
                raise ValueError("Manifest asset paths must be explicit absolute paths")
            if str(path.resolve()) in pins and pins[str(path.resolve())] != entry["sha256"]:
                raise ValueError("Conflicting asset identity")
            pins[str(path.resolve())] = entry["sha256"]
    for path_string, sha in list(pins.items()):
        if digest(path_string) != sha:
            raise ValueError(f"Input checksum mismatch: {path_string}")
        if Path(path_string).suffix.lower() == ".vrt":
            for node in ET.parse(path_string).iter("SourceFilename"):
                source = Path(node.text or "")
                if node.get("relativeToVRT") == "1":
                    source = Path(path_string).parent / source
                if str(source.resolve()) not in pins:
                    raise ValueError(f"Unpinned VRT source: {source}")
    return pins


def verify_pins(pins: Any) -> Any:
    if any(digest(path) != sha for path, sha in pins.items()):
        raise ValueError("Inputs changed during transmission execution")


def prepared_identity(root: Any) -> Any:
    root = Path(root)
    names = [
        "prepared.json",
        "model_and_grid.json",
        "source_cells.json",
        "land_area.parquet",
        "water_area.parquet",
        *[f"prepared/{key}.npy" for key in DTYPES],
    ]
    return {name: digest(root / name) for name in names}


# Canonical v2 pair schema also represents an empty, explicitly completed population.

TRANSMISSION_PAIR_SCHEMA: dict[str, Any] = {
    "source_type": pl.String,
    "source_h3": pl.String,
    "target_h3": pl.String,
    "method": pl.String,
    "target_support": pl.String,
    "support_status": pl.String,
    "lower": pl.Float64,
    "upper": pl.Float64,
    "average_viewability": pl.Float64,
    "input_unknown_fraction": pl.Float64,
    "out_of_range_fraction": pl.Float64,
    "target_water_pixel_count": pl.Int64,
    "sampled_target_points": pl.Int64,
    "observer_count": pl.Int64,
    "max_distance_m": pl.Float64,
    "sampling": pl.String,
    "census_checked": pl.Boolean,
    "weight_terrain": pl.Float64,
    "weight_distance": pl.Float64,
    "distance_km": pl.Float64,
    "above_ground_lower": pl.Float64,
    "above_ground_upper": pl.Float64,
    "weight_above_ground": pl.Float64,
    "above_ground_status": pl.String,
}


def runtime_identity() -> dict[str, Any]:
    """Pin native/library execution evidence for preparation-to-score reuse."""
    import sys
    from importlib.metadata import version

    import pyproj
    import rasterio

    return {
        "python": sys.version,
        "libraries": {
            name: version(name)
            for name in (
                "numpy",
                "numba",
                "rasterio",
                "pyproj",
                "shapely",
                "h3",
                "polars",
                "geopandas",
                "pyogrio",
            )
        },
        "gdal": rasterio.__gdal_version__,
        "proj": pyproj.proj_version_str,
    }
