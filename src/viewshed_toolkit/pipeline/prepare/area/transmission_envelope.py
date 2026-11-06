"""Conservative centroid IO envelope; actual ray cutoff and source sampling are unchanged."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from rasterio.transform import array_bounds
from shapely import get_coordinates


def centroid_read_envelope(full_cell: Any, viewing_range_m: float) -> dict[str, Any]:
    """Bound every full-cell observer and its entire straight ray in EPSG32610 metres.

    Euclidean distance is convex along polygon edges: its maximum occurs at a vertex.
    Triangle inequality places each reachable target withinR+maxObserverOffset of the
    chosen centroid. Convexity of that disk also contains every intervening ray point.
    The raster margin covers60m endpoint padding, <60m phase rounding, one10m pixel
    diagonal and the conservative inscribed-buffer expansion; round up to30m cells.
    """
    if (
        full_cell.geom_type != "Polygon"
        or full_cell.is_empty
        or not full_cell.is_valid
        or full_cell.area <= 0
        or not math.isfinite(viewing_range_m)
        or viewing_range_m <= 0
    ):
        raise ValueError("Valid full projected cell and positive finite viewing range required")
    coordinates = get_coordinates(full_cell)
    if not np.isfinite(coordinates).all():
        raise ValueError("Finite projected cell coordinates required")
    centroid = full_cell.centroid
    cx, cy = float(centroid.x), float(centroid.y)
    offset = float(np.hypot(coordinates[:, 0] - cx, coordinates[:, 1] - cy).max())
    buffer_expansion = viewing_range_m * (1 / math.cos(math.pi / 256) - 1)
    margin = 30.0 * math.ceil((60.0 + 60.0 + math.sqrt(2) * 10.0 + buffer_expansion) / 30.0)
    radius = viewing_range_m + offset + margin
    return dict(
        centroid_xy=[cx, cy],
        centroid_method="projected_full_H3_polygon_centroid",
        within_cell_bound_m=offset,
        raster_margin_m=margin,
        radius_m=radius,
        bounds=[cx - radius, cy - radius, cx + radius, cy + radius],
        viewing_range_m=viewing_range_m,
        policy="IO_only_full_cell_variance_plus_raster_margin",
    )


def validate_cropped_envelope(envelope: dict[str, Any], grid: Any, shape: Any) -> None:
    """The endpoint optimization must remain wholly inside the conservative read box."""
    left, bottom, right, top = array_bounds(*shape, grid)
    x0, y0, x1, y1 = envelope["bounds"]
    if left < x0 or bottom < y0 or right > x1 or top > y1:
        raise ValueError("Endpoint crop exceeds conservative centroid read envelope")
