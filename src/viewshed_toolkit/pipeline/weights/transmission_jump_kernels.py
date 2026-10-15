"""Direct block exits on certified clear surfaces and zero-depth canopy blocks.

Boundary times are recomputed from lattice coordinates; bitwise equivalence to
incremental DDA is not promised. Unknown/support masks prevent unsafe skips.
"""

import math
from typing import Any

import numpy as np

from .transmission_fast_kernels import _native, _patch_clear
from .vegetation.classified import _below_length


@_native
def _jump_block(
    row: int,
    col: int,
    ro: int,
    co: int,
    sx: int,
    sy: int,
    vx: float,
    vy: float,
    gx: float,
    gy: float,
    ox: float,
    oy: float,
    pixel: float,
    distance: float,
) -> tuple[float, int, int, float, float]:
    br, bc = (row - ro) // 128, (col - co) // 128
    edge_col = co + (bc + (sx > 0)) * 128
    edge_row = ro + (br + (sy > 0)) * 128
    bx = (gx + edge_col * pixel - ox) / vx if vx != 0 else math.inf
    by = (edge_row * pixel - (gy - oy)) / vy if vy != 0 else math.inf
    t = min(bx, by, distance)
    if t >= distance:
        return distance, row, col, math.inf, math.inf
    # Calculate the other axis from the crossing coordinate. Correct its cell
    # membership using direct boundary times rather than a positional epsilon.
    nc = math.floor((ox - gx + vx * t) / pixel)
    nr = math.floor((gy - oy + vy * t) / pixel)
    if bx <= by:
        nc = edge_col if sx > 0 else edge_col - 1
    if by <= bx:
        nr = edge_row if sy > 0 else edge_row - 1
    nx = (gx + (nc + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
    ny = ((nr + (sy > 0)) * pixel - (gy - oy)) / vy if vy != 0 else math.inf
    if nx <= t:
        nc += sx
        nx = (gx + (nc + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
    if ny <= t:
        nr += sy
        ny = ((nr + (sy > 0)) * pixel - (gy - oy)) / vy if vy != 0 else math.inf
    # Roundoff can put the non-exit coordinate on the next cell early.
    if bx > t and vx != 0:
        previous = (gx + (nc + (sx < 0)) * pixel - ox) / vx
        if previous > t:
            nc -= sx
            nx = previous
    if by > t and vy != 0:
        previous = ((nr + (sy < 0)) * pixel - (gy - oy)) / vy
        if previous > t:
            nr -= sy
            ny = previous
    return t, nr, nc, nx, ny


@_native
def _rays_block_cached(
    ground: Any,
    lower: Any,
    unknown: Any,
    clearance: Any,
    ox: Any,
    oy: Any,
    targets: Any,
    origin_x: Any,
    origin_y: Any,
    pixel: Any,
    eye: Any,
    target_height: Any,
    curve: Any,
    radius: Any,
    terrain_support: Any,
    row_offset: Any,
    col_offset: Any,
    block_max: Any,
    terrain_blocked: Any = None,
) -> Any:
    out = np.full(len(targets), -1, dtype=np.int8)
    height, width = ground.shape
    source_col = math.floor((ox - origin_x) / pixel)
    source_row = math.floor((origin_y - oy) / pixel)
    if (
        terrain_support is not None
        and not terrain_support[source_row - row_offset, source_col - col_offset]
    ):
        raise ValueError("Observer outside declared terrain support")
    for n in range(len(targets)):
        tx, ty = targets[n]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        if distance > radius:
            out[n] = -2  # Range exclusion is distinct from obstruction.
            continue
        tr = math.floor((origin_y - ty) / pixel)
        tc = math.floor((tx - origin_x) / pixel)
        if not math.isfinite(ground[tr - row_offset, tc - col_offset]) or (
            terrain_support is not None and not terrain_support[tr - row_offset, tc - col_offset]
        ):
            raise ValueError("Required target terrain missing or outside support")
        endpoint_unknown = (
            unknown[tr - row_offset, tc - col_offset]
            and not clearance[tr - row_offset, tc - col_offset]
        )
        if distance == 0:
            out[n] = -1 if endpoint_unknown else 1
            continue
        slope = (
            ground[tr - row_offset, tc - col_offset] + target_height - eye - curve * distance**2
        ) / distance
        vx, vy = dx / distance, -dy / distance
        col, row = source_col, source_row
        sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
        next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
        next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        entered, uncertain, blocked = 0.0, endpoint_unknown, False
        cached_br, cached_bc, clear_block = -1, -1, False
        while row_offset <= row < row_offset + height and col_offset <= col < col_offset + width:
            br, bc = (row - row_offset) // 128, (col - col_offset) // 128
            if br != cached_br or bc != cached_bc:
                cached_br, cached_bc = br, bc
                edge_x = origin_x + (col_offset + (bc + (sx > 0)) * 128) * pixel
                bx = (edge_x - ox) / vx if vx != 0 else math.inf
                # vy in this kernel follows raster rows;physical exit uses the same sign.
                by = (
                    ((row_offset + (br + (sy > 0)) * 128) * pixel - (origin_y - oy)) / vy
                    if vy != 0
                    else math.inf
                )
                hi = min(bx, by, distance) + 1e-6
                lo = max(0.0, entered - 1e-6)
                dd = (
                    min(hi, max(lo, -slope / (2 * curve)))
                    if curve > 0
                    else (lo if slope >= 0 else hi)
                )
                floor = eye + slope * dd + curve * dd * dd
                clear_block = hi >= lo and block_max[br, bc] < floor - 1e-5
            if clear_block:
                exited, nr, nc, nx, ny = _jump_block(
                    row,
                    col,
                    row_offset,
                    col_offset,
                    sx,
                    sy,
                    vx,
                    vy,
                    origin_x,
                    origin_y,
                    ox,
                    oy,
                    pixel,
                    distance,
                )
                if exited >= distance:
                    break
                entered, row, col, next_x, next_y = exited, nr, nc, nx, ny
                continue
            exited = min(next_x, next_y, distance)
            # Central source cell is explicitly grounded. Cell boundaries of
            # zero length do not turn an adjacent off-ray roof into a blocker.
            if exited > entered + 1e-9 and (row != source_row or col != source_col):
                if not math.isfinite(ground[row - row_offset, col - col_offset]) or (
                    terrain_support is not None
                    and not terrain_support[row - row_offset, col - col_offset]
                ):
                    raise ValueError("Required ray terrain missing or outside support")
                h = (
                    0.0
                    if clearance[row - row_offset, col - col_offset]
                    else lower[row - row_offset, col - col_offset]
                )
                if (
                    unknown[row - row_offset, col - col_offset]
                    and not clearance[row - row_offset, col - col_offset]
                ):
                    uncertain = True
                if row != tr or col != tc:
                    d = (
                        min(exited, max(entered, -slope / (2 * curve)))
                        if curve > 0
                        else (entered if slope >= 0 else exited)
                    )
                    if (
                        ground[row - row_offset, col - col_offset]
                        + h
                        - eye
                        - slope * d
                        - curve * d * d
                        > 1e-7
                    ):
                        blocked = True
                        # Reuse this same interval only if ground alone blocks.
                        if terrain_blocked is not None:
                            terrain_blocked[n] = (
                                ground[row - row_offset, col - col_offset]
                                + 0.0
                                - eye
                                - slope * d
                                - curve * d * d
                                > 1e-7
                            )
                        break
            if exited >= distance:
                break
            advance_x, advance_y = next_x <= next_y, next_y <= next_x
            entered = exited
            if advance_x:
                col += sx
                next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
            if advance_y:
                row += sy
                next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        out[n] = 0 if blocked else -1 if uncertain else 1
    return out


@_native
def _path_depth_zero(
    ground: Any,
    canopy: Any,
    missing_height: Any,
    coefficient: Any,
    missing_class: Any,
    clearance: Any,
    baseline: Any,
    ox: Any,
    oy: Any,
    targets: Any,
    origin_x: Any,
    origin_y: Any,
    pixel: Any,
    eye: Any,
    target_height: Any,
    curve: Any,
    row_offset: Any,
    col_offset: Any,
    zero_blocks: Any,
    clearance_row: Any = 0,
    clearance_col: Any = 0,
) -> Any:
    """Return weighted canopy-intersection metres; negative states stay distinct."""
    output = baseline.astype(np.float64)
    source_col = math.floor((ox - origin_x) / pixel)
    source_row = math.floor((origin_y - oy) / pixel)
    height, width = ground.shape
    for n in range(len(targets)):
        if baseline[n] != 1:
            continue
        tx, ty = targets[n]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        output[n] = 0.0
        if distance == 0:
            continue
        tr = math.floor((origin_y - ty) / pixel)
        tc = math.floor((tx - origin_x) / pixel)
        if missing_height[tr - row_offset, tc - col_offset] and not _patch_clear(
            clearance, tr - row_offset, tc - col_offset, clearance_row, clearance_col
        ):
            output[n] = -1
            continue
        slope = (
            ground[tr - row_offset, tc - col_offset] + target_height - eye - curve * distance**2
        ) / distance
        vx, vy = dx / distance, -dy / distance
        col, row = source_col, source_row
        sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
        next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
        next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        entered, depth, uncertain = 0.0, 0.0, False
        while row_offset <= row < row_offset + height and col_offset <= col < col_offset + width:
            br, bc = (row - row_offset) // 128, (col - col_offset) // 128
            if zero_blocks[br, bc]:
                exited, nr, nc, nx, ny = _jump_block(
                    row,
                    col,
                    row_offset,
                    col_offset,
                    sx,
                    sy,
                    vx,
                    vy,
                    origin_x,
                    origin_y,
                    ox,
                    oy,
                    pixel,
                    distance,
                )
                if exited >= distance:
                    break
                entered, row, col, next_x, next_y = exited, nr, nc, nx, ny
                continue
            exited = min(next_x, next_y, distance)
            if (
                exited > entered + 1e-9
                and (row != source_row or col != source_col)
                and (row != tr or col != tc)
                and not _patch_clear(
                    clearance, row - row_offset, col - col_offset, clearance_row, clearance_col
                )
            ):
                if missing_height[row - row_offset, col - col_offset]:
                    uncertain = True
                elif canopy[row - row_offset, col - col_offset] > 0:
                    length = _below_length(
                        entered,
                        exited,
                        eye,
                        slope,
                        curve,
                        ground[row - row_offset, col - col_offset]
                        + canopy[row - row_offset, col - col_offset],
                    )
                    if length > 0:
                        if missing_class[row - row_offset, col - col_offset]:
                            uncertain = True
                        else:
                            depth += coefficient[row - row_offset, col - col_offset] * length
            if exited >= distance:
                break
            advance_x, advance_y = next_x <= next_y, next_y <= next_x
            entered = exited
            if advance_x:
                col += sx
                next_x = (origin_x + (col + (sx > 0)) * pixel - ox) / vx if vx != 0 else math.inf
            if advance_y:
                row += sy
                next_y = ((row + (sy > 0)) * pixel - (origin_y - oy)) / vy if vy != 0 else math.inf
        output[n] = -1 if uncertain else depth
    return output
