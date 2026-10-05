"""Metric display transforms for exported geometry, never scientific LOS calculations."""

import math


def raster_point(grid, row, col):
    a, b, c, d, e, f = grid["affine"]
    return [a * col + b * row + c, d * col + e * row + f]


def grid_bounds(grid):
    height, width = grid["source_shape"]
    corners = [
        raster_point(grid, row, col)
        for row, col in ((0, 0), (0, width), (height, width), (height, 0))
    ]
    return [
        min(p[0] for p in corners),
        min(p[1] for p in corners),
        max(p[0] for p in corners),
        max(p[1] for p in corners),
    ]


def grid_cell_corners(grid, row, col):
    height, width = grid["source_shape"]
    stride = grid["display_stride"]
    r, c = row * stride, col * stride
    if not (0 <= r < height and 0 <= c < width):
        raise ValueError("Displayed sample outside original raster")
    bottom, right = min(r + stride, height), min(c + stride, width)
    return [
        raster_point(grid, rr, cc)
        for rr, cc in ((r, c), (r, right), (bottom, right), (bottom, c), (r, c))
    ]


def sample_spacing(grid):
    a, b, _c, d, e, _f = grid["affine"]
    stride = grid["display_stride"]
    return [stride * math.hypot(a, d), stride * math.hypot(b, e)]


def projected_viewport(bounds, width, height, padding=20, zoom=1):
    west, south, east, north = bounds
    if not (east > west and north > south and width > 2 * padding and height > 2 * padding):
        raise ValueError("Invalid projected display viewport")
    scale = (
        min((width - 2 * padding) / (east - west), (height - 2 * padding) / (north - south)) * zoom
    )
    center_x, center_y = (west + east) / 2, (south + north) / 2

    def project(point):
        return [
            width / 2 + (point[0] - center_x) * scale,
            height / 2 - (point[1] - center_y) * scale,
        ]

    return project, scale
