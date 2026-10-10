import numpy as np
import pytest
from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_canopy_visibility


def run(g, c, u, **kw):
    clear = np.zeros(g.shape, bool)
    clear[g.shape[0] // 2, g.shape[1] // 2] = True
    return partial_canopy_visibility(
        g,
        c,
        u,
        observer_row=g.shape[0] // 2,
        observer_col=g.shape[1] // 2,
        pixel_size_m=30,
        clearance_mask=clear,
        **kw,
    )


def test_unknown_propagation_and_blocked_certainty():
    g = np.zeros((11, 11))
    c = g.copy()
    u = np.zeros(g.shape, bool)
    u[5, 6] = True
    r = run(g, c, u)
    assert r[5, 8] == -1 and r[5, 2] == 1
    assert r[5, 6] == -1  # Unknown target is not a supported ground endpoint.
    c[5, 7] = 100
    assert run(g, c, u)[5, 9] == 0
    assert run(g, c, u, max_distance_m=60)[5, 9] == 0


def test_unknown_observer_and_clearance():
    g = np.zeros((7, 7))
    u = np.zeros(g.shape, bool)
    u[3, 3] = True
    a = partial_canopy_visibility(g, g, u, observer_row=3, observer_col=3, pixel_size_m=30)
    assert np.all(a == -1)
    assert np.all(run(g, g, u) == 1)


def test_gdal_parity_and_interval_enclosure():
    gdal = pytest.importorskip("osgeo.gdal")
    gdal.UseExceptions()
    rng = np.random.default_rng(41)
    g = rng.uniform(0, 20, (31, 31)).astype("float32")
    c = rng.uniform(0, 15, g.shape).astype("float32")
    c[::2, ::2] = 0
    c[15, 15] = 0
    unknown = np.zeros(g.shape, bool)
    unknown[13:15, 17:20] = True
    exact = run(g, c, np.zeros(g.shape, bool))
    ds = gdal.GetDriverByName("MEM").Create("", 31, 31, 1, gdal.GDT_Float64)
    ds.SetGeoTransform([400000, 30, 0, 5400000, 0, -30])
    ds.SetProjection("EPSG:32610")
    ds.GetRasterBand(1).WriteArray(g.astype(float) + c)
    v = gdal.ViewshedGenerate(
        ds.GetRasterBand(1),
        "MEM",
        "",
        [],
        400465,
        5399535,
        1.7,
        1,
        1,
        0,
        0,
        255,
        0.85714,
        gdal.GVM_Edge,
        30000,
    )
    assert np.array_equal(exact[c == 0], v.ReadAsArray()[c == 0])
    bounds = run(g, c, unknown)
    for height in (0, 2, 80, 1000000):
        alt = c.copy()
        alt[unknown] = height
        full = run(g, alt, np.zeros(g.shape, bool))
        assert np.all(full[bounds != -1] == bounds[bounds != -1])


@pytest.mark.parametrize("bad", [np.nan, -1, np.inf])
def test_invalid_known_canopy(bad):
    g = np.zeros((3, 3))
    c = g.copy()
    c[0, 0] = bad
    with pytest.raises(ValueError):
        run(g, c, np.zeros(g.shape, bool))


def test_partial_kernel_uses_full_population():
    from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_kernel_summary

    args = dict(
        known_weight_sum=2.0,
        unknown_weight_sum=3.0,
        observer_count=5,
        equivalent_water_pixels=10.0,
        bare_kernel=0.5,
    )
    r = partial_kernel_summary(**args)
    assert r["combined_lower"] == 0.04 and r["combined_upper"] == 0.1
    assert r["weight_combined"] is None and r["weight_vegetation"] is None
    args["unknown_weight_sum"] = 0
    r = partial_kernel_summary(**args)
    assert r["weight_combined"] == 0.04 and r["weight_vegetation"] == 0.08
    args.update(known_weight_sum=0, unknown_weight_sum=5)
    assert partial_kernel_summary(**args)["weight_combined"] is None
    args["bare_kernel"] = 0
    r = partial_kernel_summary(**args)
    assert r["weight_combined"] == 0 and r["weight_vegetation"] is None
    args["equivalent_water_pixels"] = 0
    with pytest.raises(ValueError):
        partial_kernel_summary(**args)


def test_shape_and_parameter_validation():
    g = np.zeros((3, 3))
    u = np.zeros(g.shape, bool)
    with pytest.raises(ValueError):
        run(g, g, u, max_distance_m=0)
    with pytest.raises(ValueError):
        run(g, g[:2], u)


def test_rotation_determinism():
    rng = np.random.default_rng(7)
    g = rng.uniform(0, 5, (9, 9))
    c = rng.uniform(0, 8, g.shape)
    u = rng.random(g.shape) < 0.15
    first = run(g, c, u)
    assert np.array_equal(first, np.rot90(run(np.rot90(g), np.rot90(c), np.rot90(u)), 3))


def test_mixed_water_does_not_erase_native_unknown_land():
    from viewshed_toolkit.pipeline.prepare.elevation.canopy import qualify_partial_canopy_grid

    heights = np.array([[np.nan, np.nan, 7.0, 0.0]])
    unknown = np.array([[False, True, True, False]])
    water = np.array([[True, True, True, False]])
    h, u = qualify_partial_canopy_grid(heights, unknown, water)
    assert h.tolist() == [[0.0, 0.0, 0.0, 0.0]]
    assert u.tolist() == [[False, True, True, False]]
    assert np.isnan(heights[0, 0])  # No caller mutation.
    with pytest.raises(ValueError):
        qualify_partial_canopy_grid(heights, unknown[:, :2], water)
