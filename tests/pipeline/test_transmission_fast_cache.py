import pytest
from affine import Affine
from shapely.geometry import box
from viewshed_toolkit.pipeline.prepare.area.transmission import required_support_mask
from viewshed_toolkit.pipeline.prepare.area.transmission_support_cache import SupportMaskCache


@pytest.mark.parametrize("case", ["contained", "edge", "outside"])
def test_cached_support_strict_edges_and_eviction(case):
    grid = Affine(10, 0, 0, 0, -10, 10000)
    water = box(100, 9500, 600, 9800)
    radius = 300
    cache = SupportMaskCache(water, grid, radius, max_bytes=384**2)
    active = (
        box(200, 9600, 250, 9650)
        if case == "contained"
        else box(850, 9300, 900, 9350) if case == "edge" else box(1500, 8000, 1550, 8050)
    )
    for r, c in [(0, 0), (384, 384), (0, 0)]:
        g = grid * Affine.translation(c, r)
        a = required_support_mask(g, (384, 384), active, water, radius)
        b = cache.mask(g, (384, 384), active, water, radius)
        assert a.tobytes() == b.tobytes()
        assert cache.bytes <= 384**2
