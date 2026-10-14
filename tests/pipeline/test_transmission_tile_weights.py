"""Persisted-layer and H3-index parity, independent dense scoring and nullable semantics."""

from types import SimpleNamespace

import h3
import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box, mapping

from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.contracts.transmission import DTYPES, digest
from viewshed_toolkit.pipeline.prepare.area.transmission import (
    WaterTargetProvider,
    endpoint_grid,
    required_support_mask,
    target_design,
    target_population,
)
from viewshed_toolkit.pipeline.prepare.area.transmission_index import (
    decode_water_index,
    encode_water_index,
)
from viewshed_toolkit.pipeline.prepare.elevation.transmission import extended_grid
from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
    PreparedTileStore,
    PreparedTileWriter,
)
from viewshed_toolkit.pipeline.prepare.transmission import observer_design
from viewshed_toolkit.pipeline.weights.transmission import calculate
from viewshed_toolkit.pipeline.weights.transmission_store import score_prepared_source


@pytest.mark.parametrize("state", ["clear", "unknown", "blocked"])
def test_readonly_store_matches_independent_dense_calculation(tmp_path, state):
    grid = Affine(10, 0, 499500, 0, -10, 5370500)
    size = (384, 384)
    data = {k: np.zeros(size, dtype=v) for k, v in DTYPES.items()}
    data["classes"][:] = 30
    data["observer_ground"][:] = 2
    land = box(499910, 5370000, 499940, 5370030)
    water = box(499950, 5369940, 500060, 5370070)
    if state == "unknown":
        data["building_unknown"][:] = True
    if state == "blocked":
        data["ground"][:, 44] = 60
        data["building_unknown"][:] = True
    # Outside required support retains rawNaN rather than forcing whole-tilefinite.
    data["ground"][0, 0] = np.nan
    old, oldshape = extended_grid(
        grid, np.array([[*land.bounds[:2], 0], [*land.bounds[2:], 0]]), 120.0
    )
    win, wh = endpoint_grid(old, land, water, 120.0)
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(
        land.centroid.x, land.centroid.y
    )
    source = dict(
        source_h3=h3.latlng_to_cell(lat, lon, 7),
        required_tile_keys=[[0, 0]],
        read_envelope={"bounds": [499000.0, 5369000.0, 501000.0, 5371000.0]},
        source_land=mapping(land),
        original_transform=list(old)[:6],
        original_shape=list(oldshape),
        window_transform=list(win)[:6],
        window_shape=list(wh),
    )
    root = tmp_path / "tiles"
    with PreparedTileWriter(
        root, {"fixture": "v1"}, grid, size, [(0, 0)], max_bytes=10 * 1024**2
    ) as writer:
        writer.write_tile(0, 0, data)
        writer.complete()
    store = PreparedTileStore(root)
    before = {p.name: digest(p) for p in root.iterdir()}
    provider = decode_water_index(encode_water_index(water, grid), water, grid)
    config = SimpleNamespace(
        max_distance_m=120.0,
        source_samples=15,
        target_stride=2,
        census_threshold=1000,
        observer_workers=1,
        maximum_sampling_error=0.1,
        p95_sampling_error=0.05,
        method="worldcover_chm_intersection_transmission_v2",
        distance_config=lambda: DistanceWeightConfig(),
    )
    frame, info = score_prepared_source(
        store, source, water, provider, config, census=True, water_class_policy="zero_if_missing"
    )
    window = store.window(win, wh)
    observers, _ = observer_design(source, window, samples=15)
    targets, rr, cc, _ = target_population(water, old, oldshape, observers, max_distance_m=120.0)
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(
        targets[:, 0], targets[:, 1]
    )
    cells, codes = np.unique(
        np.array([h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)]),
        return_inverse=True,
    )
    selected, weights = target_design(rr, cc, codes, 2, census_threshold=1000)
    support = required_support_mask(win, wh, land, water, 120.0)
    window["ray_reference_transform"] = old
    result, _ = calculate(
        window,
        window["classes"],
        observers,
        targets[selected],
        codes[selected],
        weights,
        cells,
        120.0,
        distance_config=DistanceWeightConfig(),
        terrain_support_mask=support,
    )
    for i, row in enumerate(frame.iter_rows(named=True)):
        assert row["lower"] == result["lower"][i]
        assert row["upper"] == result["upper"][i]
        assert row["average_viewability"] == result["score"][i]
        for key, value in result["diagnostics"][i].items():
            assert row[key] == value
    assert info["census_checked"]
    assert len(info["observers"]) == 15
    assert before == {p.name: digest(p) for p in root.iterdir()}
    other = SimpleNamespace(**vars(config))
    other.observer_workers = 4
    parallel, _ = score_prepared_source(
        store, source, water, provider, other, census=True, water_class_policy="zero_if_missing"
    )
    assert parallel.equals(frame)


def test_water_index_integer_dictionary_exact_and_stale_geometry_rejected():
    grid = Affine(10, 0, 499500, 0, -10, 5370500)
    water = box(499950, 5369940, 500060, 5370070)
    expected = WaterTargetProvider(water, grid)
    payload = encode_water_index(water, grid)
    actual = decode_water_index(payload, water, grid)
    np.testing.assert_array_equal(actual.points, expected.points)
    np.testing.assert_array_equal(actual.cells, expected.cells)
    assert actual.points.dtype == np.int32 and not actual.points.flags.writeable
    with pytest.raises(ValueError, match="identity"):
        decode_water_index(payload, water.buffer(1), grid)


def test_approved_canopy_gap_imputation_preserves_raw_fields_and_other_uncertainty():
    from viewshed_toolkit.pipeline.weights.transmission import prepare_calculation

    dims = (5, 12)
    data = {k: np.zeros(dims, dtype=v) for k, v in DTYPES.items()}
    data["transform"] = Affine(10, 0, 0, 0, -10, 50)
    data["canopy"][1, 4] = 12
    data["canopy_unknown"][2, 5] = True
    data["building"][2, 7] = 8
    data["building_present"][2, 7] = True
    data["building_unknown"][2, 8] = True
    data["ground"][4, 11] = np.nan
    support = np.ones(dims, bool)
    support[4, 11] = False
    original = {
        key: value.tobytes() for key, value in data.items() if isinstance(value, np.ndarray)
    }
    classes = np.full(dims, 30, dtype="uint8")
    retained = prepare_calculation(data, classes, terrain_support_mask=support)
    imputed = prepare_calculation(
        data,
        classes,
        terrain_support_mask=support,
        canopy_gap_policy="assume_zero_acquired",
        canopy_gap_qualified=True,
    )
    assert retained.canopy.missing_height[2, 5]
    assert not imputed.canopy.missing_height.any()
    assert not imputed.canopy.input_unknown[2, 5]
    assert imputed.canopy.canopy.tobytes() == retained.canopy.canopy.tobytes()
    assert imputed.ground.tobytes() == retained.ground.tobytes()
    assert imputed.building.tobytes() == retained.building.tobytes()
    assert imputed.building_unknown.tobytes() == retained.building_unknown.tobytes()
    assert all(data[key].tobytes() == value for key, value in original.items())
    assert np.isnan(imputed.ground[4, 11])
    assert imputed.canopy_imputation["imputed_pixels"] == 1
    assert imputed.canopy_imputation["original_mask_retained_in_prepared_store"]
    assert retained.canopy_imputation["imputed_pixels"] == 0
    assert (
        imputed.canopy_imputation["imputed_mask_sha256"]
        != retained.canopy_imputation["imputed_mask_sha256"]
    )
    support[4, 11] = True
    with pytest.raises(ValueError, match="Required terrain"):
        prepare_calculation(
            data,
            classes,
            terrain_support_mask=support,
            canopy_gap_policy="assume_zero_acquired",
            canopy_gap_qualified=True,
        )


def test_canopy_imputation_requires_coverage_and_rejects_positive_unknown_floor():
    from viewshed_toolkit.pipeline.weights.transmission import prepare_calculation

    dims = (3, 12)
    data = {k: np.zeros(dims, dtype=v) for k, v in DTYPES.items()}
    data["transform"] = Affine(10, 0, 0, 0, -10, 30)
    classes = np.full(dims, 30, dtype="uint8")
    data["canopy_unknown"][1, 5] = True
    with pytest.raises(ValueError, match="coverage must be qualified"):
        prepare_calculation(data, classes, canopy_gap_policy="assume_zero_acquired")
    data["canopy"][1, 5] = 4
    with pytest.raises(ValueError, match="zero-floor"):
        prepare_calculation(
            data, classes, canopy_gap_policy="assume_zero_acquired", canopy_gap_qualified=True
        )


def test_canopy_zero_does_not_resolve_missing_building_height():
    from viewshed_toolkit.pipeline.weights.transmission import (
        calculate_prepared,
        prepare_calculation,
    )

    dims = (3, 12)
    data = {k: np.zeros(dims, dtype=v) for k, v in DTYPES.items()}
    data["transform"] = Affine(10, 0, 0, 0, -10, 30)
    data["canopy_unknown"][1, 5] = True
    data["building_unknown"][1, 6] = True
    prepared = prepare_calculation(
        data,
        np.full(dims, 30, dtype="uint8"),
        canopy_gap_policy="assume_zero_acquired",
        canopy_gap_qualified=True,
    )
    result, _ = calculate_prepared(
        prepared,
        np.array([[15.0, 15.0, 0.0]]),
        np.array([[95.0, 15.0]]),
        np.array([0]),
        np.ones(1),
        ["fixture"],
        120.0,
    )
    assert result["score"] == [None]
    assert result["unknown"][0] > 0


def test_height_bin_coefficients_are_bit_exact_and_fractional_inputs_preserved():
    from viewshed_toolkit.pipeline.weights.vegetation.classified import (
        HEIGHT_BREAKS_M,
        HEIGHT_OBSTRUCTION,
        class_obstruction,
    )
    from viewshed_toolkit.pipeline.weights.vegetation.defaults import ESA_WORLDCOVER_CLASS_WEIGHTS

    values = np.r_[np.arange(255), [1.25, 1.999, 2.0, 4.999, 5.0, 14.999, 15.0, 255.25]]
    canopy = np.tile(values, (len(ESA_WORLDCOVER_CLASS_WEIGHTS) + 2, 1))
    classes = np.repeat(
        np.array([0, *ESA_WORLDCOVER_CLASS_WEIGHTS, 255], dtype="uint8")[:, None],
        len(values),
        axis=1,
    )
    height_score = np.select(
        [canopy < b for b in HEIGHT_BREAKS_M],
        HEIGHT_OBSTRUCTION[:-1],
        default=HEIGHT_OBSTRUCTION[-1],
    )
    expected = np.zeros(canopy.shape, dtype="float64")
    missing = np.ones(canopy.shape, bool)
    for code, rule in ESA_WORLDCOVER_CLASS_WEIGHTS.items():
        selected = classes == code
        expected[selected] = np.maximum(
            height_score[selected] * rule["obstruction_multiplier"], rule["obstruction_floor"]
        )
        missing[selected] = False
    missing |= (classes == 80) & (canopy > 0)
    actual, unknown = class_obstruction(canopy, classes)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(unknown, missing)
    assert actual.dtype == np.float64
    assert canopy[0, -8] == 1.25


def test_uint8_and_float32_canopy_have_identical_ray_weights():
    from viewshed_toolkit.pipeline.weights.transmission import (
        calculate_prepared,
        prepare_calculation,
    )

    dims = (3, 12)
    data = {k: np.zeros(dims, dtype=v) for k, v in DTYPES.items()}
    data["transform"] = Affine(10, 0, 0, 0, -10, 30)
    data["ground"][:] = np.float32(1.1)
    data["canopy"][1, 4:7] = [2, 5, 15]
    options = (
        np.array([[15.0, 15.0, float(np.float32(1.1))]]),
        np.array([[95.0, 15.0]]),
        np.array([0]),
        np.ones(1),
        ["fixture"],
        120.0,
    )
    classes = np.full(dims, 30, dtype="uint8")
    compact = prepare_calculation(data, classes)
    original = dict(data, canopy=data["canopy"].astype("float32"))
    reference = prepare_calculation(original, classes)
    a, _ = calculate_prepared(compact, *options, distance_config=DistanceWeightConfig())
    b, _ = calculate_prepared(reference, *options, distance_config=DistanceWeightConfig())
    assert a["score"] == b["score"] and a["diagnostics"] == b["diagnostics"]
    for key in ["lower", "upper", "unknown", "out_of_range"]:
        np.testing.assert_array_equal(a[key], b[key])
