"""Direct block exits checked against independent boundary enumeration."""

from dataclasses import replace

import numpy as np
import pytest
from affine import Affine
from viewshed_toolkit.pipeline.weights import transmission, transmission_fast as fast
from viewshed_toolkit.pipeline.weights.transmission_jump_kernels import _jump_block

"""Independent crossing enumeration, not the production incremental DDA/horizon recurrence."""

import itertools
from decimal import Decimal, localcontext


def oracle(prepared, observer, target, roof=False):
    g = prepared.transform
    x0 = (observer[0] - g.c) / g.a
    y0 = (g.f - observer[1]) / g.a
    x1 = (target[0] - g.c) / g.a
    y1 = (g.f - target[1]) / g.a
    dx = x1 - x0
    dy = y1 - y0
    dist = np.hypot(dx, dy) * g.a
    if dist > 30000:
        return {"state": -2, "margin_m": None}
    events = [np.array([0.0, 1.0])]
    for a, b in [(x0, x1), (y0, y1)]:
        if a != b:
            edges = np.arange(np.floor(min(a, b)) + 1, np.ceil(max(a, b)))
            times = (edges - a) / (b - a)
            events.append(times[(times > 0) & (times < 1)])
    ts = np.unique(np.concatenate(events))
    mid = (ts[:-1] + ts[1:]) / 2
    rr = np.floor(y0 + dy * mid).astype(int)
    cc = np.floor(x0 + dx * mid).astype(int)
    sr, sc = int(np.floor(y0)), int(np.floor(x0))
    tr, tc = int(np.floor(y1)), int(np.floor(x1))
    pos = (ts[1:] - ts[:-1]) * dist > 1e-9
    assert np.isfinite(prepared.ground[rr[pos], cc[pos]]).all(), "Oracle requires finite terrain"
    if prepared.terrain_support is not None:
        assert prepared.terrain_support[
            rr[pos], cc[pos]
        ].all(), "Oracle requires declared terrain support"
    source = (rr == sr) & (cc == sc)
    endpoint = (rr == tr) & (cc == tc)
    eye = observer[2] + 1.7
    curve = 0.85714 / (2 * 6378137.0)
    slope = (prepared.ground[tr, tc] + 1 - eye - curve * dist * dist) / dist
    lo = ts[:-1] * dist
    hi = ts[1:] * dist
    d = np.clip(-slope / (2 * curve), lo, hi)
    surface = prepared.ground[rr, cc].astype(float) + (prepared.building[rr, cc] if roof else 0)
    margins = surface - eye - slope * d - curve * d * d
    eligible = pos & ~source & ~endpoint
    margins[~eligible] = -np.inf
    j = int(np.argmax(margins))
    maxm = float(margins[j])
    unknown = roof and (
        prepared.building_unknown[tr, tc]
        or np.any(prepared.building_unknown[rr[pos & ~source], cc[pos & ~source]])
    )
    state = 0 if maxm > 1e-7 else -1 if unknown else 1
    # High-precision independent slab intersection for the strongest apparent blocker.
    r, c = int(rr[j]), int(cc[j])
    with localcontext() as ctx:
        ctx.prec = 45

        def D(v):
            return Decimal.from_float(float(v))

        ox, oy = D(observer[0]), D(observer[1])
        tx, ty = D(target[0]), D(target[1])
        vx, vy = tx - ox, ty - oy
        length = (vx * vx + vy * vy).sqrt()
        a = Decimal(0)
        b = Decimal(1)
        left = D(g.c) + Decimal(c) * D(g.a)
        right = left + D(g.a)
        top = D(g.f) - Decimal(r) * D(g.a)
        bottom = top - D(g.a)
        for origin, delta, lower, upper in [(ox, vx, left, right), (oy, vy, bottom, top)]:
            if delta:
                v1, v2 = (lower - origin) / delta, (upper - origin) / delta
                a = max(a, min(v1, v2))
                b = min(b, max(v1, v2))
        eye_d = D(observer[2]) + Decimal("1.7")
        curve_d = Decimal("0.85714") / (2 * Decimal(6378137))
        sl = (D(prepared.ground[tr, tc]) + 1 - eye_d - curve_d * length * length) / length
        dl, dh = a * length, b * length
        dm = min(dh, max(dl, -sl / (2 * curve_d)))
        terrain = D(prepared.ground[r, c])
        height = D(prepared.building[r, c]) if roof else Decimal(0)
        exact = terrain + height - eye_d - sl * dm - curve_d * dm * dm
    return {
        "state": state,
        "margin_m": maxm,
        "decimal_blocker_margin_m": float(exact),
        "blocker_row": r,
        "blocker_col": c,
        "blocker_x": float(g.c + (c + 0.5) * g.a),
        "blocker_y": float(g.f - (r + 0.5) * g.a),
        "blocker_surface_m": float(surface[j]),
        "distance_m": float(dist),
        "blocker_distance_m": float(d[j]),
        "unknown_on_ray": bool(unknown),
        "cell_interval_m": float(hi[j] - lo[j]),
    }


def depth_oracle(prepared, observer, target, roof_state, rounded_ceiling=False):
    """Enumerate boundary intervals and quadratic roots, independently of DDA/depth helpers."""
    if roof_state != 1:
        return float(roof_state)
    g = prepared.transform
    pc = prepared.canopy
    x0 = (observer[0] - g.c) / g.a
    y0 = (g.f - observer[1]) / g.a
    x1 = (target[0] - g.c) / g.a
    y1 = (g.f - target[1]) / g.a
    dx = x1 - x0
    dy = y1 - y0
    distance = float(np.hypot(dx, dy) * g.a)
    if distance == 0:
        return 0.0
    sr, sc = int(np.floor(y0)), int(np.floor(x0))
    tr, tc = int(np.floor(y1)), int(np.floor(x1))

    def cleared(r, c):
        return (r == sr and c == sc) or np.hypot(
            g.c + (c + 0.5) * g.a - observer[0], g.f - (r + 0.5) * g.a - observer[1]
        ) <= 30

    if pc.missing_height[tr, tc] and not cleared(tr, tc):
        return -1.0
    events = [np.array([0.0, 1.0])]
    for a, b in [(x0, x1), (y0, y1)]:
        if a != b:
            ts = (np.arange(np.floor(min(a, b)) + 1, np.ceil(max(a, b))) - a) / (b - a)
            events.append(ts[(ts > 0) & (ts < 1)])
    ts = np.unique(np.concatenate(events))
    mid = (ts[:-1] + ts[1:]) / 2
    rr = np.floor(y0 + dy * mid).astype(int)
    cc = np.floor(x0 + dx * mid).astype(int)
    eye = observer[2] + 1.7
    curve = 0.85714 / (2 * 6378137.0)
    slope = (float(prepared.ground[tr, tc]) + 1 - eye - curve * distance**2) / distance
    depth = 0.0
    unknown = False
    for r, c, lo, hi in zip(rr, cc, ts[:-1] * distance, ts[1:] * distance):
        if hi - lo <= 1e-9 or (r == tr and c == tc) or cleared(r, c):
            continue
        if pc.missing_height[r, c]:
            unknown = True
            continue
        if pc.canopy[r, c] <= 0:
            continue
        height = (
            float(prepared.ground[r, c] + pc.canopy[r, c])
            if rounded_ceiling
            else float(prepared.ground[r, c]) + float(pc.canopy[r, c])
        )
        # Integrate the portions where height - sightline is positive.
        roots = np.roots([curve, slope, eye - height])
        cuts = [lo, hi] + [float(z.real) for z in roots if abs(z.imag) < 1e-12 and lo < z.real < hi]
        cuts.sort()
        length = 0.0
        for a, b in itertools.pairwise(cuts):
            m = (a + b) / 2
            if eye + slope * m + curve * m * m < height:
                length += b - a
        if length > 0:
            if pc.missing_class[r, c]:
                unknown = True
            else:
                depth += float(pc.coefficient[r, c]) * length
    return -1.0 if unknown else depth


@pytest.mark.parametrize(
    "kind",
    [
        "clear",
        "roof",
        "terrain",
        "canopy",
        "missing_roof",
        "missing_canopy",
        "missing_class",
        "unknown_then_blocked",
        "grazing",
        "unsupported",
    ],
)
def test_jump_geometry(kind, monkeypatch):
    dims = (512, 768)
    g = Affine(10, 0, 420000, 0, -10, 5490000)
    data = {
        k: np.zeros(dims, np.float32 if k in ["ground", "building", "canopy"] else bool)
        for k in [
            "ground",
            "building",
            "canopy",
            "building_unknown",
            "canopy_unknown",
            "building_present",
            "qualified_water",
        ]
    }
    data["transform"] = g
    classes = np.full(dims, 30, np.uint8)
    if kind == "roof":
        data["building"][256:260, 130:640] = 30
        data["building_present"] = data["building"] > 0
    if kind == "terrain":
        data["ground"][256:260, 130:640] = 30
    if kind in ["canopy", "missing_class"]:
        data["canopy"][256:260, 130:640] = 30
    if kind == "missing_class":
        classes[256:260, 130:640] = 0
    if kind == "missing_roof":
        data["building_unknown"][256:260, 130:640] = True
    if kind == "missing_canopy":
        data["canopy_unknown"][256:260, 130:640] = True
    if kind == "unknown_then_blocked":
        data["building_unknown"][180:185, :] = True
        data["ground"][300:305, :] = 40
    if kind == "grazing":
        data["ground"][256:260, 130:640] = np.float32(1.0)
    support = np.ones(dims, bool)
    if kind == "unsupported":
        support[256:260, 130:640] = False
    p = transmission.prepare_calculation(data, classes, terrain_support_mask=support)
    p = replace(p, ray_reference=g * Affine.translation(-384, -384))
    obs = np.array([[420005, 5489995, 0], [427675, 5484885, 0], [421280, 5488720, 0]], float)
    # Cardinal, diagonal, block corner and almost-corner endpoints, plus random rays.
    rng = np.random.default_rng(9)
    t = np.column_stack([420005 + rng.random(140) * 7670, 5484885 + rng.random(140) * 5110])
    t = np.vstack(
        [
            t,
            [
                [427675, 5489995],
                [420005, 5484885],
                [422565, 5487435],
                [422565 + 1e-8, 5487435],
                [422565 - 1e-8, 5487435],
            ],
        ]
    )
    raw = {}
    orig = fast._reduce_raw

    def capture(roof, bare, depth, *a):
        raw.update(roof=roof.copy(), bare=bare.copy(), depth=depth.copy())
        return orig(roof, bare, depth, *a)

    monkeypatch.setattr(fast, "_reduce_raw", capture)
    args = (p, obs, t, np.zeros(len(t), int), np.ones(len(t)), ["a"], 30000.0)
    if kind == "unsupported":
        with pytest.raises(ValueError):
            fast.calculate_chunked(*args, observer_workers=1)
        with pytest.raises(ValueError):
            fast.calculate_chunked(*args, observer_workers=1, block_jumps=True)
        return
    fast.calculate_chunked(*args, observer_workers=1)
    base = {k: v.copy() for k, v in raw.items()}
    transmission.calculate_prepared(*args, observer_workers=1, calculation_mode="block_jump")
    saved = {k: v.copy() for k, v in raw.items()}
    permutation = np.arange(len(t))[::-1]
    shuffled = (p, obs, t[permutation], args[3], args[4], args[5], args[6])
    transmission.calculate_prepared(*shuffled, observer_workers=4, calculation_mode="block_jump")
    for key in saved:
        np.testing.assert_array_equal(raw[key][:, permutation], saved[key])
    raw.update(saved)
    np.testing.assert_equal(raw["roof"], base["roof"])
    np.testing.assert_equal(raw["bare"], base["bare"])
    np.testing.assert_allclose(raw["depth"], base["depth"], atol=1e-7, rtol=1e-10)
    for i in range(len(obs)):
        for j in range(0, len(t), 7):
            assert raw["roof"][i, j] == oracle(p, obs[i], t[j], roof=True)["state"]
            assert raw["bare"][i, j] == oracle(p, obs[i], t[j], roof=False)["state"]
            assert (
                abs(raw["depth"][i, j] - depth_oracle(p, obs[i], t[j], int(raw["roof"][i, j])))
                < 1e-6
            )


def test_block_exit_randomized():
    from math import floor, hypot

    rng = np.random.default_rng(902)
    for _ in range(4000):
        ro, co = 1194, -834
        sr, sc = int(rng.integers(ro, ro + 768)), int(rng.integers(co, co + 1024))
        ox = 420000 + (sc + rng.random()) * 10
        oy = 5490000 - (sr + rng.random()) * 10
        dx, dy = rng.normal(size=2)
        norm = hypot(dx, dy)
        vx, vy = dx / norm, dy / norm
        sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
        t, r, c, nx, ny = _jump_block(
            sr, sc, ro, co, sx, sy, vx, vy, 420000.0, 5490000.0, ox, oy, 10.0, 30000.0
        )
        assert t > 0 and nx > t and ny > t
        # Independent midpoint immediately after the exit, bounded by the next crossing.
        mid = (t + min(nx, ny)) / 2
        assert c == floor((ox - 420000 + vx * mid) / 10)
        assert r == floor((5490000 - oy + vy * mid) / 10)
        assert (r - ro) // 128 != (sr - ro) // 128 or (c - co) // 128 != (sc - co) // 128


@pytest.mark.parametrize(
    "dx,dy", [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
)
def test_block_corner_and_cardinal_exits(dx, dy):
    import math

    # Exact block boundary source, including immediate zero-length exits.
    vx, vy = dx / math.hypot(dx, dy), dy / math.hypot(dx, dy)
    sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
    t, r, c, nx, ny = _jump_block(
        128, 128, 0, 0, sx, sy, vx, vy, 420000.0, 5490000.0, 421280.0, 5488720.0, 10.0, 30000.0
    )
    assert t >= 0 and nx > t and ny > t
    mid = (t + min(nx, ny)) / 2
    assert c == math.floor((1280 + vx * mid) / 10)
    assert r == math.floor((1280 + vy * mid) / 10)


def test_mode_default_and_implementation_identity(tmp_path):
    from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig
    from viewshed_toolkit.pipeline.contracts.transmission import (
        implementation_identity,
        payload_hash,
    )

    config = TransmissionTileConfig.model_validate(
        {
            "schema_version": 1,
            "method": "worldcover_chm_intersection_transmission_v2",
            "store_dir": str(tmp_path / "store"),
            "score_dir": str(tmp_path / "scores"),
            "provisioned_metadata": {"path": "m", "sha256": "0" * 64},
            "source_inventory": {"path": "s", "sha256": "0" * 64},
            "reporting_water": {"path": "w", "sha256": "0" * 64},
            "global_transform": [10.0, 0.0, 0.0, 0.0, -10.0, 0.0],
            "global_shape": [384, 384],
        }
    )
    assert config.calculation_mode == "block_jump"
    legacy = TransmissionTileConfig.model_validate(
        {**config.runtime(), "calculation_mode": "exact_fast"}
    )
    assert payload_hash(config.runtime()) != payload_hash(legacy.runtime())
    assert "pipeline/weights/transmission_jump_kernels.py" in implementation_identity()
    with pytest.raises(ValueError):
        TransmissionTileConfig.model_validate({**config.runtime(), "calculation_mode": "adaptive"})


@pytest.mark.parametrize(
    "dtype,pixel,radius",
    [(np.float64, 10.0, 30000.0), (np.float32, 20.0, 30000.0), (np.float32, 10.0, 30001.0)],
)
def test_jump_outside_validated_envelope_retains_incremental_path(dtype, pixel, radius):
    dims = (128, 256)
    data = {
        k: np.zeros(dims, np.float32 if k in ["ground", "canopy", "building"] else bool)
        for k in [
            "ground",
            "canopy",
            "building",
            "canopy_unknown",
            "building_unknown",
            "building_present",
            "qualified_water",
        ]
    }
    data["ground"] = np.zeros(dims, dtype)
    data["transform"] = Affine(pixel, 0, 0, 0, -pixel, 128 * pixel)
    p = transmission.prepare_calculation(data, np.full(dims, 30, np.uint8))
    args = (
        p,
        np.array([[pixel / 2, 127.5 * pixel, 0.0]]),
        np.array([[255.5 * pixel, pixel / 2]]),
        np.array([0]),
        np.array([1.0]),
        ["cell"],
        radius,
    )
    profile = {}
    actual, _ = transmission.calculate_prepared(
        *args, calculation_mode="block_jump", profile=profile
    )
    reference, _ = transmission.calculate_prepared(*args, calculation_mode="exact_fast")
    assert not profile["block_jumps"]
    assert actual["lower"].tobytes() == reference["lower"].tobytes()
