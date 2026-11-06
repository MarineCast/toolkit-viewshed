import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit import direct_ray_visibility


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_cell_scores.py"
    spec = importlib.util.spec_from_file_location("cell_benchmark", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_score_is_mean_visibility_over_all_observer_target_pairs():
    m = module()
    # Observer A sees all of cell 0; B sees none. Cell 1 is out of range for both.
    states = np.array([[1, 1, -2], [0, 0, -2]])
    result = m.aggregate(states, np.array([0, 0, 1]), np.array([1, 3, 2]), 2)
    assert result["score"] == [0.5, 0.0]
    assert result["out_of_range"].tolist() == [0, 1]
    # Union-visible fraction would be 1 for cell 0, a different quantity.
    assert result["lower"].tolist() == [0.5, 0]


def test_unknown_support_keeps_complete_score_null_and_full_population_bounds():
    result = module().aggregate(np.array([[1, -1, 0]]), np.array([0, 0, 0]), np.array([1, 2, 1]), 1)
    assert result["score"] == [None]
    assert result["lower"].tolist() == [0.25]
    assert result["upper"].tolist() == [0.75]
    assert result["unknown"].tolist() == [0.5]


def test_stratified_targets_preserve_irregular_cell_populations_and_order():
    m = module()
    rows = np.array([0, 0, 1, 1, 0, 1])
    cols = np.array([0, 1, 0, 1, 2, 2])
    codes = np.array([0, 0, 0, 0, 1, 1])
    selected, weights = m.target_design(rows, cols, codes, 2)
    assert weights.tolist() == [4, 2]
    assert np.bincount(codes[selected], weights=weights).tolist() == [4, 2]
    permutation = np.array([5, 2, 0, 4, 3, 1])
    other, other_weights = m.target_design(
        rows[permutation], cols[permutation], codes[permutation], 2
    )
    assert np.array_equal(permutation[other], selected)
    assert np.array_equal(other_weights, weights)
    full, full_weights = m.target_design(rows, cols, codes, 1)
    assert set(full) == set(range(6)) and np.all(full_weights == 1)


def test_small_target_cells_receive_full_census_instead_of_sparse_estimates():
    m = module()
    rows = np.array([0, 0, 1, 1, 0, 1])
    cols = np.array([0, 1, 0, 1, 2, 2])
    codes = np.array([0, 0, 0, 0, 1, 1])
    selected, weights = m.target_design(rows, cols, codes, 2, census_threshold=3)
    assert weights.tolist() == [4, 1, 1]
    assert np.bincount(codes[selected], weights=weights).tolist() == [4, 2]


def test_joint_cell_scores_match_explicit_height_envelope_with_missing_endpoints():
    from viewshed_toolkit import direct_ray_envelope_visibility

    m = module()
    ground = np.zeros((9, 9))
    canopy = ground.copy()
    canopy[4, 5] = 20
    unknown = np.zeros((9, 9), bool)
    unknown[4, 8] = True
    roof = ground.copy()
    roof[4, 2] = 20
    transform = Affine(10, 0, 0, 0, -10, 90)
    targets = np.array([[85, 45], [5, 45]])
    data = dict(
        ground=ground,
        canopy=canopy,
        canopy_unknown=unknown,
        building=roof,
        building_unknown=np.zeros((9, 9), bool),
        building_present=roof > 0,
        qualified_water=np.zeros((9, 9), bool),
        transform=transform,
    )
    result, _, diagnostics = m.calculate(
        data, np.array([[45, 45, 0]]), targets, np.array([0, 1]), np.ones(2), 2, "direct"
    )
    # Both adjacent roofs and uncertain target endpoints survive observer canopy clearance.
    expected = direct_ray_envelope_visibility(
        ground,
        roof,
        np.maximum(roof, canopy),
        unknown,
        transform=transform,
        observer_xy=(45, 45),
        targets_xy=targets,
        observer_ground_elevation_m=0,
    )
    assert expected.tolist() == [-1, 0]
    assert result["joint"]["score"] == [None, 0]
    assert diagnostics["monotonic_conflicts"] == 0


@pytest.mark.parametrize(
    "states,weights,count",
    [
        (np.array([[2]]), [1], 1),
        (np.array([[1]]), [0], 1),
        (np.array([[1]]), [1], 2),
        (np.empty((0, 1)), [1], 1),
    ],
)
def test_invalid_or_empty_support_fails(states, weights, count):
    with pytest.raises(ValueError):
        module().aggregate(states, np.array([0]), np.asarray(weights), count)


def test_conservative_coarsening_preserves_unknown_without_inventing_terrain_resolution():
    m = module()
    ground = np.repeat(np.repeat(np.array([[0, 5], [10, 15]]), 3, 0), 3, 1)
    unknown = np.zeros((6, 6), bool)
    unknown[0, 0] = True
    roof = np.zeros((6, 6))
    roof[2, 2] = 12
    result = m.coarsen(
        dict(
            ground=ground,
            building=roof,
            building_unknown=unknown,
            qualified_water=~unknown,
            transform=Affine(10, 0, 0, 0, -10, 60),
        )
    )
    assert result["ground"].tolist() == [[0, 5], [10, 15]]
    assert result["building"][0, 0] == 12
    assert result["building_unknown"][0, 0] and not result["qualified_water"][0, 0]
    assert result["transform"].a == 30


def test_native_gdal_and_direct_rays_agree_on_flat_open_land_and_wall():
    pytest.importorskip("osgeo.gdal")
    m = module()
    ground = np.zeros((9, 9))
    lower = ground.copy()
    unknown = np.zeros((9, 9), bool)
    geometry = dict(
        transform=Affine(10, 0, 500000, 0, -10, 5400000),
        observer_xy=(500045, 5399955),
        targets_xy=np.array([[500085, 5399955], [500005, 5399955]]),
        observer_ground_elevation_m=0,
    )
    for roof, expected in ((0, [1, 1]), (20, [0, 1])):
        lower[4, 5] = roof
        assert m.gdal_visibility(ground, lower, unknown, **geometry).tolist() == expected
        assert direct_ray_visibility(ground, lower, unknown, **geometry).tolist() == expected
    lower[4, 5] = 0
    unknown[4, 5] = True
    assert m.gdal_visibility(ground, lower, unknown, **geometry).tolist() == [-1, 1]
    assert not lower.any()  # Native wrapper must not mutate shared obstacle arrays.
    lower[4, 5] = 20
    assert m.gdal_visibility(ground, lower, unknown, **geometry).tolist() == [0, 1]


def test_configuration_rejects_unknown_fields_before_output_creation(tmp_path):
    with pytest.raises(ValueError, match="Unknown"):
        module().run(dict(output_dir=str(tmp_path / "unused"), typo=True))
    assert not (tmp_path / "unused").exists()


def test_prepared_cache_mismatch_is_rejected_before_loading_surface(tmp_path):
    (tmp_path / "Test.npz").write_bytes(b"wrong surface")
    (tmp_path / "Test.json").write_text(
        json.dumps(dict(cache_contract="bounded_cell_surface_cache_v1", cache_npz_sha256="stale"))
    )
    with pytest.raises(ValueError, match="checksum"):
        module().run(
            dict(input_dir=str(tmp_path), output_dir=str(tmp_path / "result"), sites=["Test"])
        )
