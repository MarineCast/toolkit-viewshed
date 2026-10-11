import sqlite3

import numpy as np
import pytest

from viewshed_toolkit import compose_above_ground_surface
from viewshed_toolkit.pipeline.providers.global_building_atlas import (
    iter_gba_subset,
    parse_height_record,
)
from viewshed_toolkit.pipeline.weights.vegetation.partial import partial_canopy_visibility


def test_max_not_sum_and_joint_missingness():
    canopy = np.array([[9, 0, 7, 0]], dtype="float32")
    roof = np.array([[12, 8, 0, 0]], dtype="float32")
    cu = np.array([[False, True, False, False]])
    bu = np.array([[False, False, True, True]])
    present = np.array([[True, True, False, False]])
    water = np.array([[False, False, False, True]])
    result = compose_above_ground_surface(
        canopy, cu, roof, bu, qualified_water=water, building_present=present
    )
    assert result.lower_height.tolist() == [[12, 8, 7, 0]]
    assert result.unknown.tolist() == [[False, True, True, False]]
    assert canopy.tolist() == [[9, 0, 7, 0]]  # Inputs are not mutated.


def test_water_conflict_is_unknown_not_absence():
    a = np.ones((1, 1), dtype="float32")
    yes = np.ones((1, 1), bool)
    no = ~yes
    result = compose_above_ground_surface(
        a, no, a * 30, no, qualified_water=yes, building_present=yes
    )
    assert result.water_conflicts[0, 0] and result.unknown[0, 0]
    assert not result.qualified_water[0, 0] and result.lower_height[0, 0] == 0


@pytest.mark.parametrize("bad", [-1, np.nan, np.inf])
def test_invalid_surface_lower_height(bad):
    a = np.array([[bad]])
    no = np.zeros((1, 1), bool)
    with pytest.raises(ValueError):
        compose_above_ground_surface(
            a, no, np.zeros((1, 1)), no, qualified_water=no, building_present=no
        )


def test_supported_roof_under_unknown_canopy_proves_blockage():
    ground = np.zeros((11, 11), dtype="float32")
    floor = ground.copy()
    unknown = np.zeros(ground.shape, bool)
    unknown[5, 6] = True
    floor[5, 6] = 100
    clearance = np.zeros(ground.shape, bool)
    clearance[5, 5] = True
    kwargs = dict(observer_row=5, observer_col=5, pixel_size_m=30, clearance_mask=clearance)
    legacy = partial_canopy_visibility(ground, floor, unknown, **kwargs)
    supported = partial_canopy_visibility(
        ground, floor, unknown, unknown_height_lower=floor, **kwargs
    )
    assert legacy[5, 8] == -1
    assert supported[5, 8] == 0
    assert supported[5, 6] == -1  # Unknown endpoint stays unknown.
    assert floor[5, 6] == 100  # Adjacent roof is never cleared.
    for upper in (100, 200, 1000):
        complete = floor.copy()
        complete[unknown] = upper
        exact = partial_canopy_visibility(ground, complete, np.zeros_like(unknown), **kwargs)
        assert exact[5, 8] == supported[5, 8]


def test_lower_height_validation():
    ground = np.zeros((3, 3))
    unknown = np.zeros(ground.shape, bool)
    for lower in (np.zeros((2, 2)), np.full(ground.shape, -1), np.full(ground.shape, np.nan)):
        with pytest.raises(ValueError):
            partial_canopy_visibility(
                ground,
                ground,
                unknown,
                observer_row=1,
                observer_col=1,
                pixel_size_m=30,
                unknown_height_lower=lower,
            )


def test_gba_missing_zero_and_variance():
    assert parse_height_record(None) == (None, None)
    assert parse_height_record({"height": -999, "var": -999}) == (None, None)
    assert parse_height_record({"height": 0, "var": 2}) == (0, 2)
    assert parse_height_record({"height": True, "var": float("inf")}) == (None, None)


def test_gba_readonly_join_and_missing_contract(tmp_path):
    path = tmp_path / "buildings.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE buildings(tile,key,source,id,region,height,variance,height_status,"
        "geometry_wkb,footprint_license,minx,maxx,miny,maxy)"
    )
    con.execute(
        "INSERT INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("tile", "s1r", "s", "1", "r", 0, 2, "predicted_height", b"wkb", "ODbL1.0", 0, 2, 0, 2),
    )
    con.commit()
    before = path.read_bytes()
    record = list(iter_gba_subset(path, (0, 0, 1, 1)))[0]
    assert record.height_m == 0 and record.provider_key == "s1r"
    assert list(iter_gba_subset(path, (3, 3, 4, 4))) == []  # No absence inference.
    assert path.read_bytes() == before
    con.execute("UPDATE buildings SET key='wrong'")
    con.commit()
    with pytest.raises(ValueError, match="identity mismatch"):
        list(iter_gba_subset(path, (0, 0, 1, 1)))
    con.close()
