"""Independent physical-scale and affine regressions for documentation plan maps."""

import importlib
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def geometry(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("documentation_geometry")


@pytest.mark.parametrize(
    "affine, lengths",
    [
        ([100, 0, 500000, 0, -100, 5400000], [400, 400]),
        ([50, 0, 500000, 0, -100, 5400000], [200, 400]),
        ([60, -80, 500000, 80, 60, 5400000], [400, 400]),
    ],
)
@pytest.mark.parametrize("dimensions", [(480, 420), (720, 420), (390, 844)])
def test_metric_edges_preserve_physical_ratio_at_any_viewport(
    geometry, affine, lengths, dimensions
):
    grid = {"affine": affine, "source_shape": [11, 13], "display_stride": 4}
    corners = geometry.grid_cell_corners(grid, 0, 0)
    project, scale = geometry.projected_viewport(geometry.grid_bounds(grid), *dimensions)
    screen = list(map(project, corners))
    assert math.dist(screen[0], screen[1]) == pytest.approx(lengths[0] * scale)
    assert math.dist(screen[1], screen[2]) == pytest.approx(lengths[1] * scale)
    assert geometry.sample_spacing(grid) == pytest.approx(lengths)
    assert corners[1] == [affine[2] + 4 * affine[0], affine[5] + 4 * affine[3]]


def test_partial_edge_block_never_extends_original_footprint(geometry):
    grid = {"affine": [100, 0, 0, 0, -100, 0], "source_shape": [11, 13], "display_stride": 4}
    assert geometry.grid_cell_corners(grid, 2, 3) == [
        [1200, -800],
        [1300, -800],
        [1300, -1100],
        [1200, -1100],
        [1200, -800],
    ]
    assert geometry.grid_bounds(grid) == [0, -1100, 1300, 0]
    with pytest.raises(ValueError):
        geometry.grid_cell_corners(grid, 3, 0)


def test_published_projection_matches_canonical_geometry_within_one_pixel(geometry):
    import json

    from pyproj import Transformer

    bundle = ROOT / "docs/assets/examples/san-juan"
    display = json.loads((bundle / "inputs/display-geometry.json").read_text())
    transformer = Transformer.from_crs(4326, display["crs"], always_xy=True)
    project, _scale = geometry.projected_viewport(display["bounds"], 720, 420)
    for name, original in [
        ("observers", "observer-samples.geojson"),
        ("target_support", "target-support.geojson"),
    ]:
        raw = json.loads((bundle / original).read_text())["features"]
        for source, target in zip(raw, display[name]["features"], strict=True):
            assert (
                math.dist(
                    project(transformer.transform(*source["geometry"]["coordinates"])),
                    project(target["geometry"]["coordinates"]),
                )
                < 1
            )
