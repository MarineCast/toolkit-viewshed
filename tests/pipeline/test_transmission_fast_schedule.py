import numpy as np, pytest
from affine import Affine
from viewshed_toolkit.pipeline.weights import transmission, rays
from viewshed_toolkit.pipeline.weights.vegetation import classified
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
from viewshed_toolkit.pipeline.weights.transmission_fast import calculate_chunked


@pytest.mark.parametrize("chunks", [1, 2, 4])
@pytest.mark.parametrize(
    "kind", ["clear", "roof", "canopy", "missing_roof", "unknown_canopy", "curvature"]
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
    if kind == "curvature":
        data["ground"][:, 130:135] = np.float32(0.00001)
    prepared = transmission.prepare_calculation(data, classes)
    observers = np.array([[15.0, 1265.0, 0.0], [35.0, 1245.0, 0.0]])
    targets = np.column_stack((np.linspace(45, 2555, 81), np.linspace(1235, 15, 81)))
    codes = np.arange(81) % 3
    weights = np.arange(81) % 4 + 1
    cells = ["a", "b", "c"]
    radius = 5000.0
    distance = DistanceWeightConfig()
    native = rays._rays
    dep = classified._path_depth
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
    )
    for key in ["lower", "upper", "unknown", "out_of_range"]:
        assert a[key].tobytes() == b[key].tobytes()
    assert a["score"] == b["score"] and a["diagnostics"] == b["diagnostics"]


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
