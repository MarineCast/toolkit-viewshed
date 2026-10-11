"""Read a pinned, bbox-filtered GlobalBuildingAtlas research subset.

This vector adapter is separate from the production raster-provider registry.
A published footprint inventory is not a verified absence mask.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

GBA_CONTRACT = "gba_lod1_above_ground_v1"
GBA_CRS = "EPSG:3857"


def parse_height_record(record: Mapping[str, object] | None) -> tuple[float | None, float | None]:
    """Preserve absent, negative (-999), nonfinite heights and variance as missing.

    A predicted zero is a numeric prediction, not evidence of building absence.
    Variance is a provider diagnostic, not a calibrated confidence interval.
    """
    if record is None:
        return None, None

    def value(item: object) -> float | None:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        return number if math.isfinite(number) and number >= 0 else None

    return value(record.get("height")), value(record.get("var"))


@dataclass(frozen=True)
class GBABuilding:
    tile: str
    source: str
    source_id: str
    region: str
    height_m: float | None
    variance_m2: float | None
    geometry_wkb: bytes
    footprint_license: str
    height_status: str

    @property
    def provider_key(self) -> str:
        """Official linkage is source + id + region; retain all components too."""
        return self.source + self.source_id + self.region


def iter_gba_subset(
    path: Path, bbox_3857: tuple[float, float, float, float]
) -> Iterator[GBABuilding]:
    """Stream intersecting bounding-box candidates from a validated subset.

    Caller verifies the acquisition manifest/hash and requested support containment.
    Geometry remains EPSG:3857 and requires exact intersection after this bbox filter.
    Missing heights remain None; no returned feature does not qualify absence.
    """
    x0, y0, x1, y1 = bbox_3857
    if not all(math.isfinite(v) for v in bbox_3857) or x0 >= x1 or y0 >= y1:
        raise ValueError("Finite nonempty EPSG:3857 bounds required")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT tile,key,source,id,region,height,variance,height_status,geometry_wkb,"
            "footprint_license FROM buildings WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=?",
            (x1, x0, y1, y0),
        )
        for row in rows:
            tile, key, source, ident, region, height, variance, status, geometry, license_name = row
            if not all(isinstance(v, str) and v for v in (tile, key, source, ident, region)):
                raise ValueError("Missing GBA source identity")
            if key != source + ident + region:
                raise ValueError("GBA height join identity mismatch")
            if license_name not in ("ODbL1.0", "CC BY-NC4.0") or not isinstance(geometry, bytes):
                raise ValueError("Invalid GBA geometry/license contract")
            if status not in ("predicted_height", "missing_height"):
                raise ValueError("GBA height join not qualified")
            h, variance_value = parse_height_record({"height": height, "var": variance})
            if (h is None) != (status == "missing_height"):
                raise ValueError("GBA height state contradicts numeric value")
            yield GBABuilding(
                str(tile),
                str(source),
                str(ident),
                str(region),
                h,
                variance_value,
                geometry,
                str(license_name),
                str(status),
            )
    finally:
        connection.close()
