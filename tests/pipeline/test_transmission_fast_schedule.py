import numpy as np, pytest
from affine import Affine
from viewshed_toolkit.pipeline.weights import transmission, rays
from viewshed_toolkit.pipeline.weights.vegetation import classified
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.weights.transmission_fast import calculate_chunked


@pytest.mark.parametrize("chunks", [1, 2, 4])
@pytest.mark.parametrize(
    "kind",
    [
        "clear",
        "roof",
        "canopy",
        "missing_roof",
        "unknown_canopy",
        "curvature",
        "range",
        "terrain",
        "unknown_roof_terrain",
    ],
)
def test_chunk_layout_and_exact_reduction(monkeypatch, chunks, kind):
    dims = (128, 256)
    data = {
        "ground": np.zeros(dims, np.float32),
        "building": np.zeros(dims, np.float32),
        "building_unknown": np.zeros(dims, bool),
        "building_present": np.zeros(dims, bool),
        "qualified_water": np.zeros(dims, bool),
        "canopy": np.zeros(dims, np.uint8),
        "canopy_unknown": np.zeros(dims, bool),
        "transform": Affine(10, 0, 0, 0, -10, 1280),
    }
    classes = np.full(dims, 30, np.uint8)
    if kind == "roof":
        data["building"][:, 120:125] = 20
        data["building_present"][:, 120:125] = True
    if kind == "canopy":
        data["canopy"][:, 120:125] = 25
    if kind == "missing_roof":
        data["building_unknown"][:, 120:125] = True
    if kind == "unknown_canopy":
        data["canopy_unknown"][:, 120:125] = True
    if kind in {"terrain", "unknown_roof_terrain"}:
        data["ground"][:, 120:125] = 20
    if kind == "unknown_roof_terrain":
        data["building_unknown"][:, 100:110] = True
    if kind == "curvature":
        data["ground"][:, 130:135] = np.float32(0.00001)
    prepared = transmission.prepare_calculation(data, classes)
    observers = np.array([[15.0, 1265.0, 0.0], [35.0, 1245.0, 0.0]])
    targets = np.column_stack((np.linspace(45, 2555, 81), np.linspace(1235, 15, 81)))
    codes = np.arange(81) % 3
    weights = np.arange(81) % 4 + 1
    cells = ["a", "b", "c"]
    radius = 1000.0 if kind == "range" else 5000.0
    distance = DistanceWeightConfig()
    native = rays._rays
    dep = classified._path_depth
    raw = {"rays": {}, "depth": {}}

    def capture(*args):
        out = native(*args)
        role = "roof" if args[1] is prepared.building else "bare"
        raw["rays"][(role, args[4], args[5])] = out.copy()
        return out

    def capture_depth(*args):
        out = dep(*args)
        raw["depth"][(args[7], args[8])] = out.copy()
        return out

    monkeypatch.setattr(rays, "_rays", capture)
    monkeypatch.setattr(classified, "_path_depth", capture_depth)
    a, _ = transmission.calculate_prepared(
        prepared,
        observers,
        targets,
        codes,
        weights,
        cells,
        radius,
        distance_config=distance,
        observer_workers=2,
    )
    monkeypatch.setattr(rays, "_rays", native)
    monkeypatch.setattr(classified, "_path_depth", dep)
    b, _ = calculate_chunked(
        prepared,
        observers,
        targets,
        codes,
        weights,
        cells,
        radius,
        distance_config=distance,
        observer_workers=2,
        chunks_per_observer=chunks,
        raw_expected=raw,
    )
    for key in ["lower", "upper", "unknown", "out_of_range"]:
        assert a[key].tobytes() == b[key].tobytes()
    assert a["score"] == b["score"] and a["diagnostics"] == b["diagnostics"]

    selected = np.array([80, 1, 5, 12, 18, 40])
    shared, _ = calculate_chunked(
        prepared,
        observers,
        targets,
        codes,
        weights,
        cells,
        radius,
        distance_config=distance,
        observer_workers=2,
        chunks_per_observer=chunks,
        sampled_subset=(selected, weights[selected]),
    )
    separate, _ = transmission.calculate_prepared(
        prepared,
        observers,
        targets[selected],
        codes[selected],
        weights[selected],
        cells,
        radius,
        distance_config=distance,
    )
    for key in ["lower", "upper", "unknown", "out_of_range"]:
        assert shared[key].tobytes() == a[key].tobytes()
        assert shared["sampled_result"][key].tobytes() == separate[key].tobytes()
    assert shared["sampled_result"]["diagnostics"] == separate["diagnostics"]
    assert shared["sampled_result"]["score"] == separate["score"]


@pytest.mark.parametrize("workers,chunks", [(True, 4), (9, 4), (8, 0), (8, 5)])
def test_fast_config_rejects_invalid_scheduling(workers, chunks):
    from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TransmissionTileConfig.model_validate(
            dict(
                schema_version=1,
                method="worldcover_chm_intersection_transmission_v2",
                store_dir="a",
                score_dir="b",
                provisioned_metadata=dict(path="m", sha256="0" * 64),
                source_inventory=dict(path="s", sha256="0" * 64),
                reporting_water=dict(path="w", sha256="0" * 64),
                global_transform=[10.0, 0.0, 0.0, 0.0, -10.0, 0.0],
                global_shape=[384, 384],
                observer_workers=workers,
                target_chunks=chunks,
            )
        )


@pytest.mark.parametrize("cache", [-1, True, 257 * 1024**2])
def test_decoded_cache_rejects_invalid_budget(cache):
    from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TransmissionTileConfig.model_validate(
            dict(
                schema_version=1,
                method="worldcover_chm_intersection_transmission_v2",
                store_dir="a",
                score_dir="b",
                provisioned_metadata=dict(path="m", sha256="0" * 64),
                source_inventory=dict(path="s", sha256="0" * 64),
                reporting_water=dict(path="w", sha256="0" * 64),
                global_transform=[10.0, 0.0, 0.0, 0.0, -10.0, 0.0],
                global_shape=[384, 384],
                decoded_cache_bytes=cache,
            )
        )


@pytest.mark.parametrize("xy", [(5.0, 105.0), (125.0, 5.0), (35.0, 75.0), (34.99, 75.01)])
def test_compact_clearance_edges_radius_and_reference_offsets(monkeypatch, xy):
    from dataclasses import replace

    dims = (11, 13)
    grid = Affine(10, 0, 0, 0, -10, 110)
    data = dict(
        ground=np.zeros(dims, np.float32),
        building=np.zeros(dims, np.float32),
        building_unknown=np.zeros(dims, bool),
        building_present=np.zeros(dims, bool),
        qualified_water=np.zeros(dims, bool),
        canopy=np.full(dims, 20, np.uint8),
        canopy_unknown=np.ones(dims, bool),
        transform=grid,
    )
    prepared = transmission.prepare_calculation(data, np.full(dims, 30, np.uint8))
    prepared = replace(prepared, ray_reference=grid * Affine.translation(-384, -384))
    observers = np.array([[*xy, 0.0]])
    targets = np.array([xy, (5.0, 105.0), (125.0, 5.0), (65.0, 75.0), (65.01, 75.0)])
    raw = {"rays": {}, "depth": {}}
    native, dep = rays._rays, classified._path_depth

    def capture(*args):
        out = native(*args)
        raw["rays"][
            ("roof" if args[1] is prepared.building else "bare", args[4], args[5])
        ] = out.copy()
        return out

    def capture_depth(*args):
        out = dep(*args)
        raw["depth"][(args[7], args[8])] = out.copy()
        return out

    monkeypatch.setattr(rays, "_rays", capture)
    monkeypatch.setattr(classified, "_path_depth", capture_depth)
    args = (
        prepared,
        observers,
        targets,
        np.zeros(len(targets), int),
        np.ones(len(targets)),
        ["cell"],
        1000.0,
    )
    reference, _ = transmission.calculate_prepared(*args, distance_config=DistanceWeightConfig())
    monkeypatch.setattr(rays, "_rays", native)
    monkeypatch.setattr(classified, "_path_depth", dep)
    profile = {}
    actual, _ = calculate_chunked(
        *args, raw_expected=raw, profile=profile, distance_config=DistanceWeightConfig()
    )
    for key in ["lower", "upper", "unknown", "out_of_range"]:
        assert reference[key].tobytes() == actual[key].tobytes()
    assert profile["clearance_bytes"] <= 81


@pytest.mark.parametrize(
    "selection,weights",
    [([0, 0], [1, 1]), ([-1], [1]), ([2], [1]), ([0.0], [1]), ([0], [0]), ([0], [float("nan")])],
)
def test_invalid_shared_sample_rejected_before_traversal(selection, weights):
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="sample indices"):
        calculate_chunked(
            SimpleNamespace(ground=None, canopy=None, transform=None, ray_reference=None),
            np.array([[0.0, 0.0, 0.0]]),
            np.array([[1.0, 1.0], [2.0, 2.0]]),
            np.zeros(2, int),
            np.ones(2),
            ["a"],
            1000.0,
            sampled_subset=(np.asarray(selection), np.asarray(weights)),
        )
