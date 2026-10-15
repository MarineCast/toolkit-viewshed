"""Bounded shared tile reads retain data, isolation and durable integrity."""

import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.contracts.transmission import DTYPES
from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
    PreparedTileStore,
    PreparedTileWriter,
)

G = Affine(10, 0, 500000, 0, -10, 5400000)


def make_store(path):
    keys = [(0, 0), (0, 384), (384, 0), (384, 384)]
    rng = np.random.default_rng(22)
    with PreparedTileWriter(
        path, {"test": "batch"}, G, (768, 768), keys, max_bytes=50 * 1024**2
    ) as w:
        for r, c in keys:
            data = {name: np.zeros((384, 384), dtype=dtype) for name, dtype in DTYPES.items()}
            data["ground"][:] = rng.normal(20, 4, (384, 384)).astype(np.float32)
            data["ground"][0, 0] = np.nan
            data["canopy_unknown"][0, 1] = True
            data["building_unknown"][0, 2] = True
            data["classes"][0, 3] = 255
            w.write_tile(r, c, data)
        w.complete()
    return PreparedTileStore(path, cache_bytes=0)


def test_batch_exact_bytes_and_one_decode_per_shared_tile(tmp_path):
    store = make_store(tmp_path / "tiles")
    requests = [(G, (600, 600)), (G * Affine.translation(3, 6), (600, 600))]
    baseline = [store.window(*request) for request in requests]
    before = store.telemetry["cache_misses"]
    store.prefetch_windows(requests, max_bytes=20 * 1024**2)
    assert store.telemetry["cache_misses"] - before == 4
    actual = [store.window(*request) for request in requests]
    assert store.telemetry["cache_misses"] - before == 4
    for a, b in zip(actual, baseline, strict=True):
        for name in DTYPES:
            assert a[name].tobytes() == b[name].tobytes()
            assert not a[name].flags.writeable
    actual[0]["ground"].flags.writeable = True
    actual[0]["ground"][:] = 999
    assert actual[1]["ground"].tobytes() == baseline[1]["ground"].tobytes()
    assert not store._prefetched


def test_prefetched_window_rejects_later_corruption(tmp_path):
    store = make_store(tmp_path / "tiles")
    requests = [(G, (600, 600)), (G * Affine.translation(3, 6), (600, 600))]
    store.prefetch_windows(requests, max_bytes=20 * 1024**2)
    store.window(*requests[0])
    path = tmp_path / "tiles" / "tile-0-0.npz"
    with path.open("ab") as f:
        f.write(b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        store.window(*requests[1])
    store.discard_prefetched()
    assert not store._prefetched


def test_batch_budget_and_invalid_grid_reject_before_read(tmp_path):
    store = make_store(tmp_path / "tiles")
    before = store.telemetry.copy()
    with pytest.raises(ValueError, match="byte cap"):
        store.prefetch_windows([(G, (600, 600))], max_bytes=10)
    with pytest.raises(ValueError, match="lattice"):
        store.prefetch_windows([(G * Affine.translation(1, 0), (600, 600))], max_bytes=20 * 1024**2)
    with pytest.raises(ValueError, match="distinct"):
        store.prefetch_windows([(G, (600, 600))] * 2, max_bytes=20 * 1024**2)
    assert store.telemetry == before


@pytest.mark.parametrize(
    "mode,size,memory,expected",
    [
        ("block_jump", 2, 1024**3, 1),
        ("block_jump", 1, 1024**3, 0),
        ("reference", 2, 1024**3, 0),
        ("block_jump", 2, 32 * 1024**2, 0),
    ],
)
def test_worker_batch_memory_fallback_and_order(mode, size, memory, expected):
    from types import SimpleNamespace
    from viewshed_toolkit.pipeline.api.transmission_tile_worker import _source_sequence

    class Store:
        def __init__(self):
            self.calls = []
            self.clears = 0

        def prefetch_windows(self, windows, *, max_bytes):
            self.calls.append(windows)

        def discard_prefetched(self):
            self.clears += 1

    store = Store()
    config = SimpleNamespace(
        source_batch_size=size,
        calculation_mode=mode,
        max_memory_bytes=memory,
        decoded_cache_bytes=0,
    )
    provider = SimpleNamespace(points=np.zeros((1, 3), np.int32), cells=np.array(["a"]))
    pending = [
        dict(
            source_h3=str(i),
            window_transform=list(G * Affine.translation(i * 3, 0))[:6],
            window_shape=[600, 600],
            required_tile_keys=[[0, 0], [0, 384]],
        )
        for i in range(3)
    ]
    actual = list(_source_sequence(store, pending, config, provider))
    assert actual == pending
    assert len(store.calls) == expected
    assert store.clears == 3 - expected


def test_worker_batch_generator_releases_on_close():
    from types import SimpleNamespace
    from viewshed_toolkit.pipeline.api.transmission_tile_worker import _source_sequence

    events = []
    store = SimpleNamespace(
        prefetch_windows=lambda *a, **k: events.append("prefetch"),
        discard_prefetched=lambda: events.append("clear"),
    )
    config = SimpleNamespace(
        source_batch_size=2,
        calculation_mode="block_jump",
        max_memory_bytes=1024**3,
        decoded_cache_bytes=0,
    )
    provider = SimpleNamespace(points=np.zeros((1, 3), np.int32), cells=np.array(["a"]))
    pending = [
        dict(
            window_transform=list(G * Affine.translation(i * 3, 0))[:6],
            window_shape=[600, 600],
            required_tile_keys=[[0, 0]],
        )
        for i in range(2)
    ]
    sequence = _source_sequence(store, pending, config, provider)
    assert next(sequence) == pending[0]
    sequence.close()
    assert events == ["prefetch", "clear"]


@pytest.mark.parametrize("size", [0, 3, True])
def test_batch_configuration_rejects_invalid_size(size):
    from pydantic import ValidationError
    from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig

    payload = dict(
        schema_version=1,
        method="worldcover_chm_intersection_transmission_v2",
        store_dir="a",
        score_dir="b",
        provisioned_metadata=dict(path="m", sha256="0" * 64),
        source_inventory=dict(path="s", sha256="0" * 64),
        reporting_water=dict(path="w", sha256="0" * 64),
        global_transform=[10.0, 0.0, 0.0, 0.0, -10.0, 0.0],
        global_shape=[384, 384],
    )
    assert TransmissionTileConfig.model_validate(payload).source_batch_size == 2
    with pytest.raises(ValidationError):
        TransmissionTileConfig.model_validate(dict(payload, source_batch_size=size))
