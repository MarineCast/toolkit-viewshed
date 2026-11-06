"""Indexed exact-distance coastal blockers without a regional buffer dissolve."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import polars as pl
import shapely
from pyproj import Transformer, proj_version_str
from shapely.geometry import LineString

from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit._internal.geo.geometry import _geometry_is_valid, remember_validated_geometry
from ...config import AppConfig
from ...prepare.area.domains import DomainGeometries, _write_cache_receipt

LAND_EXCLUSION_ALGORITHM_VERSION = "qualified_linework_exact_clearance_distance_v17"


def load_projected_land_domain(app: AppConfig, domains: DomainGeometries) -> Any:
    contract = dict(
        algorithm="validated_projected_land_linework_components_v3",
        land_sha256=hashlib.sha256(domains.land_domain.wkb).hexdigest(),
        crs=str(app.viewshed.crs_projected),
        proj=proj_version_str,
        geos=shapely.geos_version_string,
        shapely=shapely.__version__,
    )
    fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    path = Path(app.paths.output_dir) / "geometry_cache/projected_land" / f"{fingerprint}.parquet"
    receipt = path.with_suffix(".json")
    try:
        metadata = json.loads(receipt.read_text())
        if (
            isinstance(metadata, dict)
            and metadata.get("contract") == contract
            and metadata.get("valid") is True
            and metadata.get("sha256") == checksum_unchanged_file(path, raw_bytes=True)
        ):
            return shapely.from_wkb(pl.read_parquet(path)["wkb"][0])
    except (OSError, ValueError, KeyError, IndexError, pl.exceptions.PolarsError):
        pass
    transformer = Transformer.from_crs(4326, app.viewshed.crs_projected, always_xy=True)
    projected = shapely.transform(
        domains.land_domain, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1]))
    )
    original_area = float(projected.area)
    pieces = list(shapely.get_parts(projected))
    validity = shapely.is_valid(np.asarray(pieces, dtype=object))
    reasons = []
    valid_parts = []
    for piece, valid in zip(pieces, validity, strict=True):
        if valid:
            remember_validated_geometry(piece)
            valid_parts.append(piece)
            continue
        reasons.append(shapely.is_valid_reason(piece))
        fixed = shapely.make_valid(piece)
        if not _geometry_is_valid(fixed):
            raise ValueError("Projected land component remains invalid after repair")
        valid_parts.append(fixed)
    # Buffer/intersection distributes across components. A collection preserves
    # their union without a global dissolve, including repaired collapsed pieces.
    projected = shapely.GeometryCollection(valid_parts)
    # All members were checked above. Collections allow component overlap, so
    # their validity follows without another traversal of millions of vertices.
    remember_validated_geometry(projected)
    atomic_sink_parquet(pl.DataFrame({"wkb": [projected.wkb]}).lazy(), path, overwrite=True)
    _write_cache_receipt(
        receipt,
        dict(
            contract=contract,
            sha256=checksum_unchanged_file(path, raw_bytes=True),
            valid=True,
            repair_applied=bool(reasons),
            invalid_component_count=len(reasons),
            original_validity_reasons=reasons[:20],
            original_signed_area_m2=original_area,
            repaired_area_m2=float(projected.area),
        ),
    )
    return projected


@dataclass
class LandExclusionIndex:
    parts: np.ndarray
    tree: Any
    boundary_parts: np.ndarray | None = None
    boundary_tree: Any = None
    clearance: float = 0.0

    @classmethod
    def from_parts(cls, parts: list[Any]) -> LandExclusionIndex:
        values = np.asarray(parts, dtype=object)
        shapely.prepare(values)
        return cls(values, shapely.STRtree(values))

    @classmethod
    def from_land_parts(cls, parts: list[Any], clearance: float) -> LandExclusionIndex:
        if not np.isfinite(clearance) or clearance < 0:
            raise ValueError("Coastal clearance must be finite and nonnegative")
        result = cls.from_parts(parts)
        boundaries = []
        for part in parts:
            if part.geom_type == "Polygon":
                for ring in (part.exterior, *part.interiors):
                    coordinates = shapely.get_coordinates(ring)
                    for start in range(0, len(coordinates) - 1, 256):
                        boundaries.append(LineString(coordinates[start : start + 257]))
            else:
                boundaries.append(part)
        result.boundary_parts = np.asarray(boundaries, dtype=object)
        result.boundary_tree = shapely.STRtree(result.boundary_parts)
        result.clearance = float(clearance)
        return result

    def blocked(self, lines: np.ndarray) -> np.ndarray:
        # A source can also be its target. A repeated-coordinate LineString is
        # invalid and GEOS STRtree dwithin can miss it; its represented ray is
        # exactly a point. Preserve that set with a valid Point predicate.
        zero = (shapely.get_type_id(lines) == 1) & (shapely.length(lines) == 0)
        if np.any(zero):
            lines = np.array(lines, dtype=object, copy=True)
            lines[zero] = shapely.get_point(lines[zero], 0)
        result = np.zeros(len(lines), dtype=bool)
        query, part = self.tree.query(lines)
        if len(query):
            hit = shapely.intersects(self.parts[part], lines[query])
            result[query[hit]] = True
        if self.clearance > 0 and self.boundary_tree is not None:
            pending = np.flatnonzero(~result)
            query, _ = self.boundary_tree.query(
                lines[pending], predicate="dwithin", distance=self.clearance
            )
            result[pending[query]] = True
        return result


def load_land_exclusion_index(
    app: AppConfig,
    domains: DomainGeometries,
    clearance: float,
    project: Callable[[], Any],
    *,
    validated_projection: bool = False,
) -> LandExclusionIndex:
    if not np.isfinite(clearance) or clearance < 0:
        raise ValueError("Coastal clearance must be finite and nonnegative")
    projected = project()
    if not validated_projection and not _geometry_is_valid(projected):
        raise ValueError("Invalid projected land for coastal clearance index")
    contract = dict(
        algorithm=LAND_EXCLUSION_ALGORITHM_VERSION,
        land_sha256=hashlib.sha256(domains.land_domain.wkb).hexdigest(),
        projected_land_sha256=hashlib.sha256(projected.wkb).hexdigest(),
        projected_crs=str(app.viewshed.crs_projected),
        clearance_m=clearance,
        geos=shapely.geos_version_string,
        proj=proj_version_str,
        metric="planar_euclidean_distance_lte_clearance",
        boundary_chunk_edges=256,
    )
    fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    path = Path(app.paths.output_dir) / "geometry_cache/land_exclusions" / f"{fingerprint}.parquet"
    receipt = path.with_suffix(".json")
    try:
        metadata = json.loads(receipt.read_text())
        if (
            isinstance(metadata, dict)
            and metadata.get("contract") == contract
            and metadata.get("sha256") == checksum_unchanged_file(path, raw_bytes=True)
        ):
            frame = pl.read_parquet(path)
            parts = list(shapely.from_wkb(frame.filter(pl.col("kind") == "land")["wkb"].to_numpy()))
            boundaries = shapely.from_wkb(
                frame.filter(pl.col("kind") == "boundary")["wkb"].to_numpy()
            )
            if parts and all(not p.is_empty for p in parts):
                result = LandExclusionIndex.from_parts(parts)
                result.boundary_parts = boundaries
                result.boundary_tree = shapely.STRtree(boundaries)
                result.clearance = float(clearance)
                return result
    except (OSError, ValueError, KeyError, IndexError, pl.exceptions.PolarsError):
        pass
    parts = []
    pending = list(shapely.get_parts(projected))
    while pending:
        part = pending.pop()
        if part.geom_type in {"MultiPolygon", "GeometryCollection", "MultiLineString"}:
            pending.extend(shapely.get_parts(part))
        elif not part.is_empty:
            parts.append(part)
    result = LandExclusionIndex.from_land_parts(parts, clearance)
    boundaries = list(result.boundary_parts) if result.boundary_parts is not None else []
    frame = pl.DataFrame(
        {
            "kind": ["land"] * len(parts) + ["boundary"] * len(boundaries),
            "wkb": [part.wkb for part in [*parts, *boundaries]],
        },
        schema={"kind": pl.String, "wkb": pl.Binary},
    )
    atomic_sink_parquet(frame.lazy(), path, overwrite=True)
    _write_cache_receipt(
        receipt, dict(contract=contract, sha256=checksum_unchanged_file(path, raw_bytes=True))
    )
    return result
