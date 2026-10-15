"""Single-pass preparation preserves generic formulas, coverage and array ownership."""

from dataclasses import fields

import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.weights import transmission
from viewshed_toolkit.pipeline.weights.transmission_stationary import stationary_compact


def inputs(shape=(37, 41)):
    rng = np.random.default_rng(811)
    d = dict(
        ground=rng.normal(4, 2, shape).astype("float32"),
        canopy=rng.integers(0, 256, shape, dtype="uint8"),
        building=rng.integers(0, 20, shape).astype("float32"),
        classes=rng.integers(0, 256, shape, dtype="uint8"),
        transform=Affine(10, 0, 500000, 0, -10, 5400000),
    )
    for k in ("canopy_unknown", "building_unknown", "building_present", "qualified_water"):
        d[k] = rng.random(shape) < 0.25
    d["canopy"][d["canopy_unknown"]] = 0
    return d


def compare(d, monkeypatch, **kwargs):
    fast = transmission.prepare_calculation(d, d["classes"], **kwargs)
    with monkeypatch.context() as mp:
        mp.setattr(transmission, "stationary_compact", lambda *args: None)
        generic = transmission.prepare_calculation(d, d["classes"], **kwargs)
    arrays = [
        (getattr(fast, k), getattr(generic, k))
        for k in ("ground", "building", "building_unknown", "terrain_support")
    ]
    arrays.extend(
        (getattr(fast.canopy, f.name), getattr(generic.canopy, f.name))
        for f in fields(fast.canopy)
        if f.name != "class_preparation_seconds"
    )
    for a, b in arrays:
        np.testing.assert_array_equal(a, b)
        if isinstance(a, np.ndarray):
            assert a.dtype == b.dtype
            assert a.tobytes() == b.tobytes()
            assert a.flags.owndata and not a.flags.writeable
            assert not any(np.shares_memory(a, v) for v in d.values() if isinstance(v, np.ndarray))
    assert fast.canopy_imputation == generic.canopy_imputation
    return fast


@pytest.mark.parametrize("policy", ["zero_if_missing", "retain_unknown"])
@pytest.mark.parametrize("gap", ["retain_unknown", "assume_zero_acquired"])
@pytest.mark.parametrize("sliced", [False, True])
def test_exact_fields_unknowns_and_source_support(policy, gap, sliced, monkeypatch):
    d = inputs()
    if sliced:
        d = {k: v[::2, ::2] if isinstance(v, np.ndarray) else v for k, v in d.items()}
    support = np.ones(d["ground"].shape, bool)
    support[0, 0] = False
    d["ground"][0, 0] = np.nan
    kwargs = dict(
        water_class_policy=policy,
        canopy_gap_policy=gap,
        canopy_gap_qualified=True,
        terrain_support_mask=support,
    )
    fast = compare(d, monkeypatch, **kwargs)
    saved = fast.canopy.coefficient.copy()
    d["canopy"][:] = 0
    np.testing.assert_array_equal(fast.canopy.coefficient, saved)


def test_exhaustive_compact_coefficients(monkeypatch):
    d = inputs((256, 256))
    d["canopy"], d["classes"] = np.indices((256, 256), dtype="uint8")
    for k in ("qualified_water", "canopy_unknown"):
        d[k][:] = False
    compare(d, monkeypatch)


@pytest.mark.parametrize("key", ["ground", "building", "canopy", "classes"])
def test_generic_dtype_fallback(key, monkeypatch):
    d = inputs()
    d[key] = d[key].astype("float64" if key != "classes" else "int16")
    assert stationary_compact(d, d["classes"], "retain_unknown") is None
    compare(d, monkeypatch)


@pytest.mark.parametrize("bad", [-1.0, np.nan, np.inf])
@pytest.mark.parametrize("water", [False, True])
def test_invalid_building_rejected_even_under_water(bad, water, monkeypatch):
    d = inputs()
    d["building"][0, 0] = bad
    d["qualified_water"][0, 0] = water
    for native in (True, False):
        with monkeypatch.context() as mp:
            if not native:
                mp.setattr(transmission, "stationary_compact", lambda *args: None)
            with pytest.raises(ValueError, match="Finite nonnegative"):
                transmission.prepare_calculation(d, d["classes"])


@pytest.mark.parametrize(
    "key", ["building", "canopy", "canopy_unknown", "qualified_water", "classes"]
)
def test_bad_shape_rejected(key):
    d = inputs()
    d[key] = d[key][:-1]
    with pytest.raises(ValueError):
        transmission.prepare_calculation(d, d["classes"])


def test_signed_zero_is_preserved(monkeypatch):
    d = inputs()
    d["building"][:] = -0.0
    d["qualified_water"][:] = False
    compare(d, monkeypatch)


def test_no_unknown_support_loss_or_shared_output(monkeypatch):
    d = inputs()
    d["building_unknown"][:] = True
    d["canopy_unknown"][:] = True
    d["canopy"][:] = 0
    d["qualified_water"][:] = False
    a = compare(d, monkeypatch, water_class_policy="retain_unknown")
    assert a.building_unknown.all() and a.canopy.missing_height.all()
    a.canopy.coefficient.flags.writeable = True
    a.canopy.coefficient[:] = 999
    b = compare(d, monkeypatch, water_class_policy="retain_unknown")
    assert (b.canopy.coefficient != 999).all()
