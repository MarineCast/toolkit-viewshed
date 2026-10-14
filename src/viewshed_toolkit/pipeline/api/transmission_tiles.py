"""Provisioned, acquisition-free canonical tile planning and preprocessing."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import geopandas as gpd
import h3
import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.warp import transform_bounds
from shapely import from_wkb
from shapely.geometry import Polygon, box, mapping
from shapely.ops import transform, unary_union

from ..config.transmission_tiles import TransmissionTileConfig, load_transmission_tile_config
from ..contracts.transmission import DTYPES, digest, implementation_identity, verify_pins
from ..contracts.transmission_store import store_runtime_identity
from ..prepare.area.transmission import endpoint_grid
from ..prepare.area.transmission_envelope import centroid_read_envelope, validate_cropped_envelope
from ..prepare.elevation.transmission import extended_grid, validate_native_band
from ..prepare.elevation.transmission_tiles import CanonicalTileBuilder


def verified_inputs(config: TransmissionTileConfig) -> tuple[dict[str, Any], dict[str, str]]:
    """Verify provisioned metadata, native pins and VRT membership; no writes."""
    for field in (config.provisioned_metadata, config.source_inventory, config.reporting_water):
        if digest(field.path) != field.sha256:
            raise ValueError(f"Input checksum mismatch: {field.path}")
    metadata = json.loads(Path(config.provisioned_metadata.path).read_text())
    pins = metadata["input_sha256"]
    if not pins or any(not Path(p).is_absolute() for p in pins):
        raise ValueError("Explicit absolute native asset pins required")
    verify_pins(pins)
    references = [metadata["coastline"], metadata["terrain_vrt"], metadata["building_subset"]]
    references += [
        entry["path"] for name in ("canopy_assets", "worldcover_assets") for entry in metadata[name]
    ]
    if any(str(Path(p).resolve()) not in pins for p in references):
        raise ValueError("Unpinned provisioned asset")
    vrt = Path(metadata["terrain_vrt"])
    for node in ET.parse(vrt).iter("SourceFilename"):
        p = Path(node.text or "")
        if node.get("relativeToVRT") == "1":
            p = vrt.parent / p
        if str(p.resolve()) not in pins:
            raise ValueError("Unpinned VRT member")
    if metadata["building_inventory_policy"] not in {"unverified", "assume_complete"} or metadata[
        "water_class_policy"
    ] not in {"retain_unknown", "zero_if_missing"}:
        raise ValueError("Unknown provisioned policy")
    policy = metadata.get("canopy_gap_policy", "retain_unknown")
    if policy not in {"retain_unknown", "assume_zero_acquired"}:
        raise ValueError("Unknown canopy gap policy")
    if policy == "assume_zero_acquired":
        _canopy_gap_coverage(metadata)
    if metadata["building_inventory_policy"] == "assume_complete":
        _building_absence_coverage(metadata)
    return metadata, pins


def _building_absence_coverage(metadata: dict[str, Any]) -> Any:
    """Qualify acquired support, not surveyed absence, for the explicit assumption."""
    evidence = metadata.get("building_absence_qualification", {})
    if (
        evidence.get("contract") != "gba_covered_absence_assumption_v1"
        or evidence.get("user_approved") is not True
        or evidence.get("canopy_nodata_policy")
        != metadata.get("canopy_gap_policy", "retain_unknown")
    ):
        raise ValueError("Explicit building absence qualification required")
    asset = evidence.get("acquisition_receipt", {})
    path = Path(asset.get("path", ""))
    pins = metadata["input_sha256"]
    if not path.is_absolute() or pins.get(str(path.resolve())) != asset.get("sha256"):
        raise ValueError("Pinned building acquisition receipt required")
    if digest(path) != asset["sha256"]:
        raise ValueError("Building acquisition receipt changed")
    receipt = json.loads(path.read_text())
    subset = str(Path(metadata["building_subset"]).resolve())
    if (
        receipt.get("status") != "completed_validated"
        or str(Path(receipt.get("subset_path", "")).resolve()) != subset
        or receipt.get("subset_sha256") != pins.get(subset)
    ):
        raise ValueError("Successful matching building acquisition required")
    bounds = receipt.get("study_bbox", [])
    if (
        len(bounds) != 4
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bounds)
        or not all(math.isfinite(v) for v in bounds)
        or not -180 <= bounds[0] < bounds[2] <= 180
        or not -90 <= bounds[1] < bounds[3] <= 90
    ):
        raise ValueError("Valid acquisition study bounds required")
    roles: dict[str, set[str]] = {}
    tiles = []
    for entry in receipt.get("files", []):
        tile, role = entry.get("tile", ""), entry.get("role", "")
        if (
            entry.get("status") != "validated"
            or role not in {"polygon", "odbl", "height"}
            or role in roles.setdefault(tile, set())
            or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
        ):
            raise ValueError("Incomplete building acquisition files")
        roles[tile].add(role)
    if not roles or any(value != {"polygon", "odbl", "height"} for value in roles.values()):
        raise ValueError("Missing building acquisition tile or role")
    for tile in roles:
        match = re.fullmatch(r"([we])(\d+)_([ns])(\d+)_([we])(\d+)_([ns])(\d+)", tile)
        if match is None:
            raise ValueError("Unqualified building acquisition tile bounds")
        values = match.groups()
        x0, y1, x1, y0 = [
            int(values[i + 1]) * (-1 if values[i] in {"w", "s"} else 1) for i in (0, 2, 4, 6)
        ]
        if not -180 <= x0 < x1 <= 180 or not -90 <= y0 < y1 <= 90:
            raise ValueError("Invalid building acquisition tile bounds")
        tiles.append(box(x0, y0, x1, y1))
    return unary_union(tiles).intersection(box(*bounds))


def _require_building_coverage(coverage: Any, bounds: Any) -> None:
    geographic = box(*transform_bounds(32610, 4326, *bounds, densify_pts=41))
    if not coverage.covers(geographic):
        raise ValueError("Building absence assumption outside acquired coverage")


def _canopy_gap_coverage(metadata: dict[str, Any]) -> Any:
    """Require available pinned ETH raster coverage before assuming gaps are zero."""
    evidence = metadata.get("canopy_gap_qualification", {})
    if (
        evidence.get("contract") != "eth_acquired_gap_zero_assumption_v1"
        or evidence.get("user_approved") is not True
        or evidence.get("basis") != "existing_pinned_native_rasters"
    ):
        raise ValueError("Explicit acquired canopy gap qualification required")
    entries = metadata.get("canopy_assets", [])
    if not entries:
        raise ValueError("Missing acquired canopy assets")
    coverage = []
    for entry in entries:
        path = Path(entry["path"]).resolve()
        expected = metadata["input_sha256"].get(str(path))
        if expected is None or digest(path) != expected:
            raise ValueError("Missing or changed acquired canopy raster")
        with rasterio.open(path) as src:
            validate_native_band(src)
            if (
                src.crs != rasterio.crs.CRS.from_epsg(4326)
                or src.nodata != 255
                or src.dtypes[0] != "uint8"
                or src.transform.b
                or src.transform.d
                or src.transform.a <= 0
                or src.transform.e >= 0
            ):
                raise ValueError("Qualified north-up ETH uint8 NoData255 rasters required")
            coverage.append(box(*src.bounds))
    return unary_union(coverage)


def _require_canopy_coverage(coverage: Any, bounds: Any) -> None:
    if not coverage.covers(box(*transform_bounds(32610, 4326, *bounds, densify_pts=41))):
        raise ValueError("Canopy gap assumption outside acquired raster coverage")


def provisioned_context(config: TransmissionTileConfig) -> dict[str, Any]:
    """Load retained geometry after byte verification; no outputs are created."""
    metadata, _ = verified_inputs(config)
    cells = json.loads(Path(config.source_inventory.path).read_text())["source_h3"]
    if (
        not cells
        or len(cells) != len(set(cells))
        or any(not h3.is_valid_cell(c) or h3.get_resolution(c) != 7 for c in cells)
    ):
        raise ValueError("Unique nonempty R7 source inventory required")
    grid = Affine(*config.global_transform)
    bounds = transform_bounds(
        32610,
        4326,
        grid.c,
        grid.f - config.global_shape[0] * 10,
        grid.c + config.global_shape[1] * 10,
        grid.f,
        densify_pts=41,
    )
    land = unary_union(
        gpd.read_file(metadata["coastline"], layer="land_area", bbox=bounds, engine="pyogrio")
        .to_crs(32610)
        .geometry
    )
    water = unary_union(
        gpd.read_file(metadata["coastline"], layer="water_area", bbox=bounds, engine="pyogrio")
        .to_crs(32610)
        .geometry
    )
    reporting = from_wkb(Path(config.reporting_water.path).read_bytes())
    if reporting.is_empty or not reporting.is_valid:
        raise ValueError("Nonempty valid EPSG32610 reporting-water WKB required")
    return dict(
        metadata=metadata, cells=cells, grid=grid, land=land, water=water, reporting_water=reporting
    )


def _plan(config: TransmissionTileConfig, context: dict[str, Any]) -> dict[str, Any]:
    grid = context["grid"]
    project = Transformer.from_crs(4326, 32610, always_xy=True).transform
    near = context["reporting_water"].buffer(
        config.max_distance_m / math.cos(math.pi / 256), quad_segs=64
    )
    coverage = (
        _building_absence_coverage(context["metadata"])
        if context["metadata"].get("building_inventory_policy") == "assume_complete"
        else None
    )
    canopy_coverage = (
        _canopy_gap_coverage(context["metadata"])
        if context["metadata"].get("canopy_gap_policy") == "assume_zero_acquired"
        else None
    )
    keys: set[tuple[int, int]] = set()
    sources = []
    for cell in context["cells"]:
        polygon = transform(
            project, Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)])
        )
        read_envelope = centroid_read_envelope(polygon, config.max_distance_m)
        if coverage is not None:
            _require_building_coverage(coverage, read_envelope["bounds"])
        if canopy_coverage is not None:
            _require_canopy_coverage(canopy_coverage, read_envelope["bounds"])
        active = polygon.intersection(context["land"])
        if (
            active.area / polygon.area < 0.01
            or polygon.intersection(context["water"]).area / polygon.area > 0.95
            or active.intersection(near).area <= 0
        ):
            raise ValueError(f"Inventory contains ineligible source: {cell}")
        a, b, c, d = active.bounds
        original, original_shape = extended_grid(
            grid, np.array([[a, b, 0.0], [c, d, 0.0]]), config.max_distance_m
        )
        window, shape = endpoint_grid(
            original, active, context["reporting_water"], config.max_distance_m
        )
        validate_cropped_envelope(read_envelope, window, shape)
        r = round((grid.f - window.f) / 10)
        col = round((window.c - grid.c) / 10)
        if (
            r < 0
            or col < 0
            or r + shape[0] > config.global_shape[0]
            or col + shape[1] > config.global_shape[1]
        ):
            raise ValueError(f"Source endpoint window outside canonical domain: {cell}")
        source_keys = sorted(
            (rr, cc)
            for rr in range(r // 384 * 384, r + shape[0], 384)
            for cc in range(col // 384 * 384, col + shape[1], 384)
        )
        keys.update(source_keys)
        sources.append(
            dict(
                source_h3=cell,
                required_tile_keys=[list(key) for key in source_keys],
                read_envelope=read_envelope,
                source_land=mapping(active),
                original_transform=list(original)[:6],
                original_shape=list(original_shape),
                window_transform=list(window)[:6],
                window_shape=list(shape),
            )
        )
    if coverage is not None or canopy_coverage is not None:
        for row, col in {(r // 1536 * 1536, c // 1536 * 1536) for r, c in keys}:
            x0, y1 = grid * (col, row)
            x1, y0 = grid * (
                min(col + 1536, config.global_shape[1]),
                min(row + 1536, config.global_shape[0]),
            )
            if coverage is not None:
                _require_building_coverage(coverage, (x0, y0, x1, y1))
            if canopy_coverage is not None:
                _require_canopy_coverage(canopy_coverage, (x0, y0, x1, y1))
    # Preparation identity excludes scoring distance coefficients and thread count.
    code = implementation_identity()
    identity = dict(
        contract="canonical_transmission_tiles_v2",
        method=config.method,
        grid=config.global_transform,
        shape=config.global_shape,
        tile_size=384,
        parent_size=1536,
        dtypes=DTYPES,
        native_pins=context["metadata"]["input_sha256"],
        metadata_sha256=config.provisioned_metadata.sha256,
        source_inventory_sha256=config.source_inventory.sha256,
        reporting_water_sha256=config.reporting_water.sha256,
        land_wkb_sha256=hashlib.sha256(context["land"].wkb).hexdigest(),
        water_wkb_sha256=hashlib.sha256(context["water"].wkb).hexdigest(),
        source_policy=config.source_policy,
        code=code,
        runtime=store_runtime_identity(),
    )
    identity["source_windows"] = sources
    identity["planning_radius_m"] = config.max_distance_m
    identity["census_sources"] = [
        sources[i]["source_h3"]
        for i in np.linspace(
            0, len(sources) - 1, min(config.census_sources, len(sources)), dtype=int
        )
    ]
    ordered = sorted(keys, key=lambda k: (k[0] // 1536, k[1] // 1536, k))
    return dict(
        identity=identity,
        required_keys=[list(k) for k in ordered],
        sources=sources,
        census_sources=[
            sources[i]["source_h3"]
            for i in np.linspace(
                0, len(sources) - 1, min(config.census_sources, len(sources)), dtype=int
            )
        ],
        uncompressed_tile_bytes=len(keys)
        * 384**2
        * sum(np.dtype(v).itemsize for v in DTYPES.values()),
        unique_parent_count=len({(r // 1536, c // 1536) for r, c in keys}),
        global_transform=config.global_transform,
        global_shape=config.global_shape,
    )


def plan_transmission_tiles(config: TransmissionTileConfig) -> dict[str, Any]:
    return _plan(config, provisioned_context(config))


def run_tile_preparation(
    config: TransmissionTileConfig | str | Path, *, stage: str = "plan", resume: bool = False
) -> dict[str, Any]:
    """Prepare one immutable store; budget exhaustion retains resumable partial tiles."""
    if not isinstance(config, TransmissionTileConfig):
        config = load_transmission_tile_config(config)
    if stage not in {"plan", "prepare", "validate"}:
        raise ValueError("Unknown tile preparation stage")
    started = time.monotonic()
    context = provisioned_context(config)
    plan = _plan(config, context)
    if stage == "plan":
        return plan
    from ..prepare.elevation.transmission_store import PreparedTileStore, PreparedTileWriter

    if stage == "validate":
        store = PreparedTileStore(config.store_dir, expected_identity=plan["identity"])
        store.validate()
        return dict(
            status="validated", required_tiles=len(plan["required_keys"]), manifest=store.manifest
        )
    writer = PreparedTileWriter(
        config.store_dir,
        plan["identity"],
        context["grid"],
        (config.global_shape[0], config.global_shape[1]),
        plan["required_keys"],
        max_bytes=config.max_store_bytes,
        resume=resume,
    )
    try:
        writer.write_sidecar(
            "plan.json", json.dumps(plan, sort_keys=True, allow_nan=False).encode()
        )
        writer.write_sidecar("reporting-water.wkb", Path(config.reporting_water.path).read_bytes())
        from ..prepare.area.transmission_index import decode_water_index, encode_water_index

        if writer.has_sidecar("water-index.npz"):
            decode_water_index(
                writer.read_sidecar("water-index.npz"), context["reporting_water"], context["grid"]
            )
        else:
            writer.write_sidecar(
                "water-index.npz", encode_water_index(context["reporting_water"], context["grid"])
            )
        metadata = context["metadata"]
        builder = CanonicalTileBuilder(
            dict(
                inputs=dict(
                    terrain_vrt=dict(path=metadata["terrain_vrt"]),
                    gba_subset=dict(path=metadata["building_subset"]),
                ),
                building_inventory_policy=metadata["building_inventory_policy"],
            ),
            metadata["canopy_assets"],
            metadata["worldcover_assets"],
            context["land"],
            context["water"],
            context["grid"],
            (config.global_shape[0], config.global_shape[1]),
            parent_capacity=1,
            profile=True,
        )
        peak_window = 1536**2 * sum(np.dtype(v).itemsize for v in DTYPES.values()) * 8
        if peak_window > config.max_memory_bytes:
            raise ValueError("Conservative parent preparation memory exceeds configured budget")
        for row, col in plan["required_keys"]:
            if time.monotonic() - started >= config.wall_seconds:
                return dict(
                    status="partial_wall_limit",
                    required_tiles=len(plan["required_keys"]),
                    parent_builds=builder.parent_builds,
                )
            if not writer.has_tile(row, col):
                writer.write_tile(row, col, builder(row, col, 384))
        verified_inputs(config)
        if implementation_identity() != plan["identity"]["code"]:
            raise ValueError("Preparation implementation changed during execution")
        writer.complete()
        manifest = PreparedTileStore(config.store_dir, expected_identity=plan["identity"]).manifest
        return dict(
            status="complete",
            manifest=manifest,
            parent_builds=builder.parent_builds,
            prepare_seconds=builder.timings,
        )
    finally:
        writer.close()
