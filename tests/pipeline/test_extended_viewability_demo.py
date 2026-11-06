"""Expanded reach preserves lattice, cutoff, curvature and input uncertainty."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box


def isolated_calculation(request):
    """Do not retain extra Numba variants against later native planner RSS caps."""
    if os.environ.get("VIEWSHED_ISOLATED_DEMO_TEST") == "1":
        return False
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", request.node.nodeid],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "VIEWSHED_ISOLATED_DEMO_TEST": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


@pytest.fixture
def demo(monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "extended_demo", scripts / "extend_viewability_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("sites", [[], ["missing"], ["Dungeness", "Dungeness"], "Dungeness"])
def test_invalid_site_selection_fails_before_output_creation(demo, tmp_path, sites):
    trial = tmp_path / "trial.json"
    trial.write_text(
        '{"contract":"bounded_worldcover_canopy_trial_v1","sites":[{"name":"Dungeness"}]}'
    )
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="unique known retained sites"):
        demo.run(
            dict(
                trial_report=str(trial),
                terrain_raster="unused",
                output_dir=str(output),
                radius_miles=15,
                sites=sites,
            )
        )
    assert not output.exists()


def test_fifteen_mile_grid_covers_all_observers_and_preserves_lattice(demo):
    old = Affine(10, 0, 485850, 0, -10, 5338200)
    observers = np.array([[490000.0, 5333000.0, 5.0], [491000.0, 5334000.0, 10.0]])
    radius = 15 * demo.METRES_PER_MILE
    transform, shape = demo.extended_grid(old, observers, radius)
    assert radius == 24140.16
    assert shape[0] % 3 == shape[1] % 3 == 0
    assert (transform.c - old.c) % 30 == (transform.f - old.f) % 30 == 0
    extent = box(transform.c, transform.f - shape[0] * 10, transform.c + shape[1] * 10, transform.f)
    for x, y, _ in observers:
        assert extent.contains(box(x - radius, y - radius, x + radius, y + radius))


def test_targets_beyond_old_five_km_reach_are_included(demo):
    transform = Affine(10, 0, 0, 0, -10, 90)
    observers = np.array([[15.0, 15.0, 0.0]])
    water = box(20000, 0, 20100, 90).union(box(25000, 0, 25100, 90))
    targets, _, _, _ = demo.target_population(
        water, transform, (9, 3000), observers, max_distance_m=15 * demo.METRES_PER_MILE
    )
    assert len(targets) > 0
    assert ((targets[:, 0] > 20000) & (targets[:, 0] < 20100)).all()


def test_worldcover_tile_boundary_does_not_overlap_or_shift(demo, tmp_path):
    assets = []
    for name, north, code in [("south", 48.0, 60), ("north", 48.01, 30)]:
        path = tmp_path / f"{name}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=200,
            height=100,
            count=1,
            dtype="uint8",
            nodata=0,
            crs=4326,
            transform=Affine(0.0001, 0, -123.43, 0, -0.0001, north),
        ) as dst:
            dst.write(np.full((100, 200), code, dtype="uint8"), 1)
        assets.append(dict(path=str(path)))
    x, y = Transformer.from_crs(4326, 32610, always_xy=True).transform(-123.42, 48)
    transform = Affine(10, 0, x - 300, 0, -10, y + 300)
    classes = demo.load_classes(assets, (60, 60), transform)
    rr, cc = np.indices(classes.shape)
    xx, yy = transform * (cc + 0.5, rr + 0.5)
    _, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(xx, yy)
    np.testing.assert_array_equal(classes, np.where(lat >= 48, 30, 60))


def test_previous_overlap_handles_smaller_reach_without_wrapped_indices(demo):
    previous = {"ground": np.arange(16).reshape(4, 4)}
    data = {"ground": np.zeros((2, 2), dtype=int)}
    demo.overlay_previous(
        data, previous, Affine(10, 0, 0, 0, -10, 40), Affine(10, 0, 10, 0, -10, 30)
    )
    np.testing.assert_array_equal(data["ground"], previous["ground"][1:3, 1:3])


def test_calculation_respects_configured_cutoff_and_preserves_land_unknown(demo, request):
    if isolated_calculation(request):
        return
    shape = (9, 20)
    data = {
        k: np.zeros(
            shape,
            dtype=(
                bool
                if "unknown" in k or k in ("building_present", "qualified_water")
                else "float32"
            ),
        )
        for k in [
            "ground",
            "canopy",
            "canopy_unknown",
            "building",
            "building_unknown",
            "building_present",
            "qualified_water",
        ]
    }
    data["transform"] = Affine(10, 0, 0, 0, -10, 90)
    classes = np.full(shape, 30, dtype="uint8")
    observers, targets = np.array([[15.0, 45.0, 0.0]]), np.array([[135.0, 45.0]])
    args = (data, classes, observers, targets, np.array([0]), np.ones(1), np.array(["water"]))
    short, _ = demo.calculate(*args, 100)
    assert short["out_of_range"][0] == 1
    clear, _ = demo.calculate(*args, 200)
    assert clear["lower"][0] == clear["upper"][0] == 1
    data["canopy_unknown"][4, 7] = True
    uncertain, _ = demo.calculate(*args, 200)
    assert uncertain["unknown"][0] == 1 and uncertain["score"][0] is None
    classes[4, 7] = 80
    water, _ = demo.calculate(*args, 200)
    assert water["lower"][0] == water["upper"][0] == 1
    data["building_unknown"][4, 7] = True
    roof, _ = demo.calculate(*args, 200)
    assert roof["unknown"][0] == 1


def test_extended_range_does_not_disable_earth_curvature(demo, request):
    if isolated_calculation(request):
        return
    shape = (9, 2500)
    data = {
        k: np.zeros(
            shape,
            dtype=(
                bool
                if "unknown" in k or k in ("building_present", "qualified_water")
                else "float32"
            ),
        )
        for k in [
            "ground",
            "canopy",
            "canopy_unknown",
            "building",
            "building_unknown",
            "building_present",
            "qualified_water",
        ]
    }
    data["transform"] = Affine(10, 0, 0, 0, -10, 90)
    result, _ = demo.calculate(
        data,
        np.full(shape, 80, dtype="uint8"),
        np.array([[15.0, 45.0, 0.0]]),
        np.array([[20015.0, 45.0]]),
        np.array([0]),
        np.ones(1),
        np.array(["water"]),
        15 * demo.METRES_PER_MILE,
    )
    assert result["out_of_range"][0] == 0
    assert result["lower"][0] == result["upper"][0] == 0


@pytest.mark.parametrize("radius", [0, -1, float("nan"), float("inf")])
def test_invalid_distance_fails(demo, radius):
    with pytest.raises(ValueError, match="positive radius"):
        demo.extended_grid(Affine(10, 0, 0, 0, -10, 90), np.array([[15.0, 15.0, 0.0]]), radius)
