"""Exact conservative block proofs; original DDA increments and coefficients retained."""

import math
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

import numpy as np
from numba import njit

from .vegetation.classified import _below_length

_NativeFunction = TypeVar("_NativeFunction", bound=Callable[..., Any])


def _native(function: _NativeFunction) -> _NativeFunction:
    """Typed boundary for Numba's external untyped dispatcher, with no fastmath."""
    return cast(_NativeFunction, njit(cache=True, nogil=True)(function))


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
        delta_x = pixel / abs(vx) if vx != 0 else math.inf
        delta_y = pixel / abs(vy) if vy != 0 else math.inf
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
                finished = False
                # Preserve every original floating-point DDA addition; only
                # invariant terrain reads/checks are omitted in certified blocks.
                while (row - row_offset) // 128 == br and (col - col_offset) // 128 == bc:
                    exited = min(next_x, next_y, distance)
                    if exited >= distance:
                        finished = True
                        break
                    advance_x, advance_y = next_x <= next_y, next_y <= next_x
                    entered = exited
                    if advance_x:
                        col += sx
                        next_x += delta_x
                    if advance_y:
                        row += sy
                        next_y += delta_y
                if finished:
                    break
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
                next_x += delta_x
            if advance_y:
                row += sy
                next_y += delta_y
        out[n] = 0 if blocked else -1 if uncertain else 1
    return out


@_native
def _patch_clear(clearance: Any, row: Any, col: Any, r0: Any, c0: Any) -> Any:
    r, c = row - r0, col - c0
    return 0 <= r < clearance.shape[0] and 0 <= c < clearance.shape[1] and clearance[r, c]


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
        delta_x = pixel / abs(vx) if vx != 0 else math.inf
        delta_y = pixel / abs(vy) if vy != 0 else math.inf
        entered, depth, uncertain = 0.0, 0.0, False
        while row_offset <= row < row_offset + height and col_offset <= col < col_offset + width:
            br, bc = (row - row_offset) // 128, (col - col_offset) // 128
            if zero_blocks[br, bc]:
                finished = False
                while (row - row_offset) // 128 == br and (col - col_offset) // 128 == bc:
                    exited = min(next_x, next_y, distance)
                    if exited >= distance:
                        finished = True
                        break
                    advance_x, advance_y = next_x <= next_y, next_y <= next_x
                    entered = exited
                    if advance_x:
                        col += sx
                        next_x += delta_x
                    if advance_y:
                        row += sy
                        next_y += delta_y
                if finished:
                    break
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
                next_x += delta_x
            if advance_y:
                row += sy
                next_y += delta_y
        output[n] = -1 if uncertain else depth
    return output


class ClearBlockPruner:
    def __init__(
        self, native: Any, block: Any = 128, expected: Any = None, *, kernel: Any = None
    ) -> None:
        self.native = native
        self.kernel = _rays_block_cached if kernel is None else kernel
        self.expected = expected
        self.cache: dict[Any, Any] = {}
        self.roles: dict[Any, Any] = {}
        self.lock = threading.Lock()
        self.pruned = self.total = 0

    def __call__(self, *args: Any, terrain_blocked: Any = None) -> Any:
        if terrain_blocked is not None:
            terrain_blocked[:] = False
        ground, lower, unknown, clearance = args[:4]
        support = args[14]
        if (
            ground.dtype != np.float32
            or abs(args[10]) > 1e6
            or not 0 < args[13] <= 30000
            or args[9] != 10
            or args[12] < 0
            or max(abs(args[i]) for i in (4, 5, 7, 8)) > 1e8
            or abs(args[12]) * args[13] ** 2 > 1e6
        ):
            return self.native(*args)
        stationary = [ground, lower, unknown, clearance] + ([] if support is None else [support])
        if any(a.flags.writeable or not a.flags.owndata for a in stationary):
            return self.native(*args)
        role = (id(ground), id(lower), id(unknown), id(clearance))
        with self.lock:
            if role not in self.roles:
                self.roles[role] = (
                    lower,
                    unknown,
                    clearance,
                    np.nanmax(np.abs(ground)) <= 1e6
                    and np.nanmax(np.abs(lower)) <= 1e6
                    and abs(args[12]) * args[13] ** 2 <= 1e6,
                )
            allowed = self.roles[role][3]
        if not allowed:
            return self.native(*args)
        key = (id(ground), id(lower), id(unknown), id(support))
        with self.lock:
            if key not in self.cache:
                h, w = ground.shape
                hp = (h + 127) // 128 * 128
                wp = (w + 127) // 128 * 128
                maximum = np.empty((hp // 128, wp // 128), np.float64)
                for row in range(0, hp, 128):
                    n = min(128, h - row)
                    a = np.full((128, wp), np.inf, np.float64)
                    if n > 0:
                        a[:n, :w] = ground[row : row + n]
                        np.add(a[:n, :w], lower[row : row + n], out=a[:n, :w])
                        a[:n, :w][unknown[row : row + n]] = np.inf
                        if support is not None:
                            a[:n, :w][~support[row : row + n]] = np.inf
                    a[~np.isfinite(a)] = np.inf
                    maximum[row // 128] = a.reshape(128, wp // 128, 128).max(axis=(0, 2))
                self.cache[key] = (ground, support, maximum, lower, unknown)
            maximum = self.cache[key][2]
        out = cast(Callable[..., Any], self.kernel)(*args, maximum, terrain_blocked)
        if self.expected is not None:
            assert np.array_equal(
                out,
                self.expected[
                    ("bare" if not lower.any() and not unknown.any() else "roof", args[4], args[5])
                ],
            ), "Raw bare-ray mismatch"
        with self.lock:
            self.pruned += len(out)
            self.total += len(out)
        return out


class DepthZeroBlocks:
    def __init__(self, native: Any, expected: Any = None, *, kernel: Any = None) -> None:
        self.native = native
        self.kernel = _path_depth_zero if kernel is None else kernel
        self.cache: dict[Any, Any] = {}
        self.lock = threading.Lock()
        self.expected = expected

    def __call__(self, *args: Any, clearance_origin: Any = None) -> Any:
        canopy, missing = args[1:3]
        coefficient, missing_class = args[3:5]
        key = (id(canopy), id(missing), id(coefficient), id(missing_class))
        if any(
            a.flags.writeable or not a.flags.owndata
            for a in [canopy, missing, coefficient, missing_class]
        ):
            if clearance_origin is not None:
                full = np.zeros(args[0].shape, bool)
                r0, c0 = clearance_origin
                patch = args[5]
                full[r0 : r0 + patch.shape[0], c0 : c0 + patch.shape[1]] = patch
                args = (*args[:5], full, *args[6:])
            return self.native(*args)
        with self.lock:
            if key not in self.cache:
                h, w = canopy.shape
                hp = (h + 127) // 128 * 128
                wp = (w + 127) // 128 * 128
                zero = np.zeros((hp // 128, wp // 128), bool)
                for row in range(0, hp, 128):
                    n = min(128, h - row)
                    skip = np.zeros((128, wp), bool)
                    if n > 0:
                        skip[:n, :w] = ~missing[row : row + n] & (
                            (canopy[row : row + n] == 0)
                            | ((coefficient[row : row + n] == 0) & ~missing_class[row : row + n])
                        )
                    zero[row // 128] = skip.reshape(128, wp // 128, 128).all(axis=(0, 2))
                self.cache[key] = (canopy, missing, zero, coefficient, missing_class)
            zero = self.cache[key][2]
        origin = (0, 0) if clearance_origin is None else clearance_origin
        out = cast(Callable[..., Any], self.kernel)(*args, zero, *origin)
        if self.expected is not None:
            assert (
                out.tobytes() == self.expected[(args[7], args[8])].tobytes()
            ), "Canopy depth changed"
        return out
