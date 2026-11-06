"""Persistent tile publication, exact numerical preservation and failure boundaries."""

import json

import numpy as np
import pytest
from affine import Affine

from viewshed_toolkit.pipeline.contracts.transmission import DTYPES
from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
    PreparedTileStore,
    PreparedTileWriter,
)

GRID = Affine(10, 0, 500000, 0, -10, 5400000)
IDENTITY = {"inputs": {"terrain": "pinned"}, "recipe": "v2", "warp_parent": 1536}


def arrays():
    result = {name: np.zeros((384, 384), dtype=dtype) for name, dtype in DTYPES.items()}
    result["ground"][0, :3] = [np.nan, -0.0, 1.5]
    result["observer_ground"][0, 0] = 7
    result["building_unknown"][0, 0] = True
    result["classes"][0, :3] = [0, 255, 50]
    return result


def writer(path, **kwargs):
    return PreparedTileWriter(
        path, IDENTITY, GRID, (390, 390), [(0, 0), (384, 384)], max_bytes=20 * 1024**2, **kwargs
    )


def completed(path):
    with writer(path) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_tile(384, 384, arrays())
        owner.write_sidecar("plan.json", b'{"source":"pinned"}')
        owner.complete()
    return PreparedTileStore(path, expected_identity=IDENTITY, cache_bytes=4 * 1024**2)


def test_exact_layers_coordinates_and_readonly_window(tmp_path):
    store = completed(tmp_path / "store")
    tile = store.tile(0, 0)
    assert tile.attrs["crs"] == "EPSG:32610"
    assert tile.x.values[0] == 500005
    assert tile.y.values[0] == 5399995
    for name, expected in arrays().items():
        assert tile[name].values.tobytes() == expected.tobytes()
        assert tile[name].dims == ("y", "x")
        assert not tile[name].values.flags.writeable
    assert np.signbit(tile.ground.values[0, 1])
    assert np.isnan(tile.ground.values[0, 0])
    assert bool(tile.building_unknown.values[0, 0])
    assert tile.canopy.values[0, 0] == 0  # observed zero remains known separately
    edge = store.window(GRID * Affine.translation(384, 384), (6, 6))
    for name in DTYPES:
        assert edge[name].tobytes() == arrays()[name][:6, :6].tobytes()
        assert not edge[name].flags.writeable
    assert store.read_sidecar("plan.json") == b'{"source":"pinned"}'
    store.validate()
    assert store.bytes <= store.cache_bytes
    tile["ground"] = tile.ground * 0  # changing Dataset containers cannot poison cache
    tile.coords["x"] = tile.x.values + 1000
    tile.attrs["transform"][2] = -999
    tile.attrs["crs"] = "changed"
    fresh = store.tile(0, 0)
    assert np.isnan(fresh.ground.values[0, 0])
    assert fresh.x.values[0] == 500005
    assert fresh.attrs["transform"][2] == 500000
    assert fresh.attrs["crs"] == "EPSG:32610"
    assert not fresh.x.values.flags.writeable
    assert not fresh.y.values.flags.writeable
    window = store.window(GRID, (3, 3))
    assert window["ground"].tobytes() == arrays()["ground"][:3, :3].tobytes()
    # Even explicitly reopening write access affects only this caller's copy.
    fresh.ground.values.flags.writeable = True
    fresh.ground.values[:] = 88
    assert np.isnan(store.window(GRID, (3, 3))["ground"][0, 0])


def test_resume_and_completion_last(tmp_path):
    root = tmp_path / "store"
    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_sidecar("plan.json", b"{}")
        assert owner.has_sidecar("plan.json")
        assert not owner.has_sidecar("water-index.npz")
        assert owner.read_sidecar("plan.json") == b"{}"
        with pytest.raises(ValueError, match="Missing"):
            owner.complete()
    with pytest.raises(ValueError, match="Missing"):
        PreparedTileStore(root)
    with writer(root, resume=True) as owner:
        assert owner.has_tile(0, 0)
        owner.write_sidecar("plan.json", b"{}")
        assert owner.has_sidecar("plan.json")
        assert not owner.has_sidecar("water-index.npz")
        assert owner.read_sidecar("plan.json") == b"{}"
        with pytest.raises(ValueError, match="different"):
            owner.write_sidecar("plan.json", b"[]")
        with pytest.raises(ValueError, match="already"):
            owner.write_tile(0, 0, arrays())
        owner.write_tile(384, 384, arrays())
        owner.complete()
    PreparedTileStore(root).validate()
    with pytest.raises(ValueError, match="Unexpected"):
        writer(root, resume=True)


def test_tampering_even_cached_and_manifest_receipt(tmp_path):
    root = tmp_path / "store"
    store = completed(root)
    store.tile(0, 0)
    path = root / "tile-0-0.npz"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="checksum"):
        store.tile(0, 0)
    with pytest.raises(ValueError, match="checksum"):
        store.validate()


@pytest.mark.parametrize("entry", ["unknown.txt", "tile-0-0.npz.part"])
def test_resume_rejects_undeclared_interrupted_evidence(tmp_path, entry):
    root = tmp_path / "store"
    with writer(root):
        pass
    (root / entry).write_bytes(b"interrupted")
    with pytest.raises(ValueError, match="Unexpected"):
        writer(root, resume=True)
    assert (root / entry).read_bytes() == b"interrupted"


def test_identity_storage_lock_and_hostile_paths(tmp_path):
    root = tmp_path / "store"
    with writer(root) as owner:
        with pytest.raises(ValueError, match="Unexpected"):
            writer(root, resume=True)
        for name in ("../plan.json", "complete.json", "tile-0-0.json"):
            with pytest.raises(ValueError, match="Unsafe"):
                owner.write_sidecar(name, b"{}")
    with pytest.raises(ValueError, match="identity"):
        PreparedTileWriter(
            root, {"other": 1}, GRID, (390, 390), [(0, 0)], max_bytes=10000000, resume=True
        )
    small = tmp_path / "small"
    with (
        PreparedTileWriter(small, IDENTITY, GRID, (384, 384), [(0, 0)], max_bytes=100000) as owner,
        pytest.raises(ValueError, match="cap"),
    ):
        owner.write_tile(0, 0, arrays())
    assert not (small / "tile-0-0.npz").exists()


def test_reject_window_shift_and_missing_required_tile(tmp_path):
    store = completed(tmp_path / "store")
    for grid in (GRID * Affine.translation(1, 0), GRID * Affine.translation(0.001, 0)):
        with pytest.raises(ValueError, match="lattice"):
            store.window(grid, (3, 3))
    with pytest.raises(ValueError, match="not present"):
        store.window(GRID * Affine.translation(384, 0), (3, 3))
    with pytest.raises(ValueError, match="identity"):
        PreparedTileStore(tmp_path / "store", expected_identity={"changed": True})


def test_dtype_corrupt_completion_and_symlink_rejected(tmp_path):
    root = tmp_path / "store"
    with writer(root) as owner:
        bad = arrays()
        bad["classes"] = bad["classes"].astype("int32")
        with pytest.raises(ValueError, match="dtype"):
            owner.write_tile(0, 0, bad)
    store = completed(tmp_path / "complete")
    manifest = store.root / "complete.json"
    value = json.loads(manifest.read_text())
    value["tiles"].pop()
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="inventory"):
        PreparedTileStore(store.root)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="safe"):
        writer(link, resume=True)


def test_failed_write_keeps_evidence_and_rejects_resume(tmp_path, monkeypatch):
    root = tmp_path / "store"

    def interrupted(handle, **values):
        handle.write(b"partial npz evidence")
        raise OSError("simulated interrupted write")

    with writer(root) as owner:
        monkeypatch.setattr(np, "savez_compressed", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            owner.write_tile(0, 0, arrays())
    assert (root / "tile-0-0.npz.part").read_bytes() == b"partial npz evidence"
    with pytest.raises(ValueError, match="Unexpected"):
        writer(root, resume=True)
    assert not (root / "complete.json").exists()


def test_sidecar_tamper_is_detected_without_reader_writes(tmp_path):
    store = completed(tmp_path / "store")
    before = {path.name for path in store.root.iterdir()}
    (store.root / "plan.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        store.read_sidecar("plan.json")
    assert {path.name for path in store.root.iterdir()} == before


def test_explicit_recovery_retains_committed_tiles_and_evidence(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        recover_prepared_store,
    )

    root = tmp_path / "store"
    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_sidecar("plan.json", b"{}")
    committed = (root / "tile-0-0.npz").read_bytes()
    (root / "writer.lock").write_text("999999999")
    (root / "tile-384-384.npz").write_bytes(b"unreceipted payload")
    (root / "tile-384-384.npz.part").write_bytes(b"partial")
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    result = recover_prepared_store(root, IDENTITY)
    assert result["status"] == "recovered"
    assert {entry["name"] for entry in result["removed"]} == {
        "writer.lock",
        "tile-384-384.npz",
        "tile-384-384.npz.part",
    }
    assert (root / "tile-0-0.npz").read_bytes() == committed
    assert (root / "plan.json").read_bytes() == b"{}"
    assert not (root / "writer.lock").exists()
    assert not (root / "recovery.lock").exists()
    with writer(root, resume=True) as owner:
        assert owner.has_tile(0, 0)
        evidence = json.loads(owner.read_sidecar("recovery.json"))
        assert evidence["events"][0]["identity"] == IDENTITY
        owner.write_tile(384, 384, arrays())
        owner.complete()
    PreparedTileStore(root).validate()


@pytest.mark.parametrize("pid", ["bad", "0", "-12", "123"])
def test_recovery_rejects_unreadable_or_live_pid(tmp_path, monkeypatch, pid):
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        recover_prepared_store,
    )

    root = tmp_path / "store"
    with writer(root):
        pass
    (root / "writer.lock").write_text(pid)
    (root / "tile-0-0.npz.part").write_bytes(b"evidence")
    monkeypatch.setattr("psutil.pid_exists", lambda pid: True)
    with pytest.raises(ValueError, match="PID"):
        recover_prepared_store(root, IDENTITY)
    assert (root / "writer.lock").read_text() == pid
    assert (root / "tile-0-0.npz.part").read_bytes() == b"evidence"


@pytest.mark.parametrize("unsafe", ["unknown", "corrupt", "symlink", "identity"])
def test_recovery_fails_before_deleting_on_bad_evidence(tmp_path, monkeypatch, unsafe):
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        recover_prepared_store,
    )

    root = tmp_path / "store"
    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
    (root / "writer.lock").write_text("999999999")
    (root / "tile-384-384.npz.part").write_bytes(b"partial")
    if unsafe == "unknown":
        (root / "unrelated.txt").write_bytes(b"preserve")
    elif unsafe == "corrupt":
        (root / "tile-0-0.npz").write_bytes(b"corrupt committed tile")
    elif unsafe == "symlink":
        (root / "tile-384-384.json.part").symlink_to(root / "store.json")
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    with pytest.raises(ValueError):
        recover_prepared_store(root, IDENTITY if unsafe != "identity" else {"wrong": 1})
    assert (root / "writer.lock").exists()
    assert (root / "tile-384-384.npz.part").read_bytes() == b"partial"
    assert not (root / "recovery.json").exists()
    assert not (root / "recovery.lock").exists()


def test_recovery_complete_refused_and_clean_incomplete_noop(tmp_path):
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        recover_prepared_store,
    )

    done = completed(tmp_path / "complete")
    with pytest.raises(ValueError, match="Completed"):
        recover_prepared_store(done.root, IDENTITY)
    root = tmp_path / "store"
    with writer(root):
        pass
    assert recover_prepared_store(root, IDENTITY)["status"] == "nothing_to_recover"
    assert not (root / "recovery.json").exists()


def test_recovery_of_interrupted_complete_and_sidecar_journal(tmp_path):
    import hashlib

    from viewshed_toolkit.pipeline.contracts.transmission_store import (
        STORE_CONTRACT,
        canonical_bytes,
        metadata_hash,
    )
    from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import (
        recover_prepared_store,
    )

    root = tmp_path / "store"
    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_tile(384, 384, arrays())
    sidecar = b"qualified new bytes"
    (root / "water-index.npz").write_bytes(sidecar)
    (root / "sidecars.json.part").write_bytes(
        canonical_bytes(
            {
                "water-index.npz": dict(
                    sha256=hashlib.sha256(sidecar).hexdigest(), bytes=len(sidecar)
                )
            }
        )
    )
    result = recover_prepared_store(root, IDENTITY)
    assert {entry["name"] for entry in result["removed"]} == {
        "water-index.npz",
        "sidecars.json.part",
    }
    assert not (root / "water-index.npz").exists()
    # A second interrupted completion records a second event, preserving previous evidence.
    spec = json.loads((root / "store.json").read_bytes())
    receipt_names = ["tile-0-0.json", "tile-384-384.json"]
    receipts = [json.loads((root / name).read_bytes()) for name in receipt_names]
    committed = json.loads((root / "sidecars.json").read_bytes())
    (root / "complete.json.part").write_bytes(
        canonical_bytes(
            dict(
                contract=STORE_CONTRACT,
                spec_hash=metadata_hash(spec),
                tiles=receipts,
                sidecars=committed,
            )
        )
    )
    result = recover_prepared_store(root, IDENTITY)
    assert [entry["name"] for entry in result["removed"]] == ["complete.json.part"]
    with writer(root, resume=True) as owner:
        assert len(json.loads(owner.read_sidecar("recovery.json"))["events"]) == 2
        owner.complete()
    PreparedTileStore(root).validate()


@pytest.mark.parametrize("name", ["plan.json", "reporting-water.wkb", "water-index.npz"])
@pytest.mark.parametrize(
    "stage", ["during-intent", "before-payload", "after-payload", "after-journal"]
)
def test_sidecar_intention_recovers_every_publication_boundary(tmp_path, monkeypatch, name, stage):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    payload = b"exact pinned sidecar content"
    original = module._atomic

    def interrupted(path, contents, **kwargs):
        if stage == "during-intent" and path.name == "sidecar-intent.json":
            path.with_suffix(".json.part").write_bytes(contents)
            raise OSError("interrupted intention publication")
        if stage == "before-payload" and path.name == name:
            raise OSError("interrupted before payload publication")
        original(path, contents, **kwargs)
        if stage == "after-payload" and path.name == name:
            raise OSError("interrupted after payload publication")
        if stage == "after-journal" and path.name == "sidecars.json":
            raise OSError("interrupted before intention clear")

    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_tile(384, 384, arrays())
        monkeypatch.setattr(module, "_atomic", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            owner.write_sidecar(name, payload)
        with pytest.raises(ValueError, match="Unexpected"):
            owner.complete()
    monkeypatch.setattr(module, "_atomic", original)
    with pytest.raises(ValueError, match="Unexpected"):
        writer(root, resume=True)
    result = module.recover_prepared_store(root, IDENTITY)
    assert result["status"] == "recovered"
    removed = {entry["name"] for entry in result["removed"]}
    assert any(entry.startswith("sidecar-intent.json") for entry in removed)
    if stage == "after-journal":
        assert name not in removed
        assert (root / name).read_bytes() == payload
    elif stage == "after-payload":
        assert name in removed
        assert not (root / name).exists()
    with writer(root, resume=True) as owner:
        owner.write_sidecar(name, payload)
        owner.complete()
    store = PreparedTileStore(root)
    assert store.read_sidecar(name) == payload
    store.validate()


def test_sidecar_intention_cannot_authorize_unknown_or_changed_bytes(tmp_path):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root) as owner:
        intent = dict(name="plan.json", spec_hash=owner.spec_hash, sha256="0" * 64, bytes=7)
    (root / "sidecar-intent.json").write_text(json.dumps(intent))
    (root / "plan.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        module.recover_prepared_store(root, IDENTITY)
    assert (root / "plan.json").read_bytes() == b"changed"
    assert (root / "sidecar-intent.json").exists()
    (root / "plan.json").unlink()
    (root / "unrelated.npz").write_bytes(b"preserve unrelated")
    with pytest.raises(ValueError, match="Unexpected"):
        module.recover_prepared_store(root, IDENTITY)
    assert (root / "unrelated.npz").read_bytes() == b"preserve unrelated"
    assert (root / "sidecar-intent.json").exists()


@pytest.mark.parametrize("temporary", ["sidecar-intent.json.part", "sidecars.json.part"])
def test_recovery_preserves_truncated_atomic_metadata_evidence(tmp_path, temporary):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root):
        pass
    fragment = b'{"incomplete":'
    (root / temporary).write_bytes(fragment)
    result = module.recover_prepared_store(root, IDENTITY)
    assert [entry["name"] for entry in result["removed"]] == [temporary]
    assert result["removed"][0]["bytes"] == len(fragment)
    assert not (root / temporary).exists()
    with writer(root, resume=True) as owner:
        assert (
            json.loads(owner.read_sidecar("recovery.json"))["events"][0]["removed"]
            == result["removed"]
        )


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize(
    "stage",
    [
        "intent-part",
        "after-intent",
        "payload-part",
        "after-payload",
        "journal-part",
        "after-journal",
        "during-cleanup",
    ],
)
def test_recovery_transaction_retries_every_boundary(tmp_path, monkeypatch, previous, stage):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root) as owner:
        owner.write_tile(0, 0, arrays())
        owner.write_tile(384, 384, arrays())
        owner.write_sidecar("plan.json", b"pinned plan")
    if previous:
        (root / "tile-0-0.npz.part").write_bytes(b"previous interrupted bytes")
        module.recover_prepared_store(root, IDENTITY)
    committed_tile = (root / "tile-0-0.npz").read_bytes()
    interrupted = root / "tile-384-384.npz.part"
    interrupted.write_bytes(b"interrupted current bytes")
    original_atomic = module._atomic
    original_unlink = type(root).unlink
    tripped = False

    def atomic(path, payload, **kwargs):
        for kind, name in [
            ("intent", "recovery-intent.json"),
            ("payload", "recovery.json"),
            ("journal", "sidecars.json"),
        ]:
            if stage == kind + "-part" and path.name == name:
                suffix = kwargs.get("temporary_suffix", ".part")
                path.with_suffix(path.suffix + suffix).write_bytes(payload[:23])
                raise OSError("injected recovery interruption")
        original_atomic(path, payload, **kwargs)
        for kind, name in [
            ("intent", "recovery-intent.json"),
            ("payload", "recovery.json"),
            ("journal", "sidecars.json"),
        ]:
            if stage == "after-" + kind and path.name == name:
                raise OSError("injected recovery interruption")

    def unlink(path, *args, **kwargs):
        nonlocal tripped
        original_unlink(path, *args, **kwargs)
        if stage == "during-cleanup" and path == interrupted and not tripped:
            tripped = True
            raise OSError("injected recovery interruption")

    monkeypatch.setattr(module, "_atomic", atomic)
    monkeypatch.setattr(type(root), "unlink", unlink)
    with pytest.raises(OSError, match="injected"):
        module.recover_prepared_store(root, IDENTITY)
    monkeypatch.setattr(module, "_atomic", original_atomic)
    monkeypatch.setattr(type(root), "unlink", original_unlink)
    result = module.recover_prepared_store(root, IDENTITY)
    assert result["status"] == "recovered"
    assert not interrupted.exists()
    assert not (root / "recovery-intent.json").exists()
    assert (root / "tile-0-0.npz").read_bytes() == committed_tile
    assert (root / "plan.json").read_bytes() == b"pinned plan"
    with writer(root, resume=True) as owner:
        events = json.loads(owner.read_sidecar("recovery.json"))["events"]
        assert len(events) == (2 if previous else 1)
        owner.complete()
    PreparedTileStore(root).validate()


@pytest.mark.parametrize("pending", [False, True])
def test_recovery_rejects_unknown_files_without_mutating_evidence(tmp_path, monkeypatch, pending):
    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root):
        pass
    (root / "tile-0-0.npz.part").write_bytes(b"interrupted")
    if pending:
        original = module._atomic

        def interrupted(path, payload, **kwargs):
            original(path, payload, **kwargs)
            if path.name == "recovery.json":
                raise OSError("injected")

        monkeypatch.setattr(module, "_atomic", interrupted)
        with pytest.raises(OSError):
            module.recover_prepared_store(root, IDENTITY)
        monkeypatch.setattr(module, "_atomic", original)
    (root / "unrelated.npz").write_bytes(b"preserve unknown file")
    (root / "recovery.lock").write_text("999999999")
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError, match="Unexpected"):
        module.recover_prepared_store(root, IDENTITY)
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


@pytest.mark.parametrize("pid", ["live", "dead", "bad", "0"])
def test_recovery_lock_requires_qualified_dead_owner(tmp_path, monkeypatch, pid):
    import os

    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root):
        pass
    lock = root / "recovery.lock"
    lock.write_text(str(os.getpid()) if pid == "live" else "999999999" if pid == "dead" else pid)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    if pid == "dead":
        assert module.recover_prepared_store(root, IDENTITY)["status"] == "nothing_to_recover"
        assert not lock.exists()
    else:
        with pytest.raises(ValueError, match=r"recovery PID|Recovery PID"):
            module.recover_prepared_store(root, IDENTITY)
        assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_recovery_lock_refuses_active_advisory_owner(tmp_path):
    import fcntl

    import viewshed_toolkit.pipeline.prepare.elevation.transmission_store as module

    root = tmp_path / "store"
    with writer(root):
        pass
    lock = root / "recovery.lock"
    lock.write_text("999999999")
    with lock.open("r+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="active"):
            module.recover_prepared_store(root, IDENTITY)
    assert lock.read_text() == "999999999"


def test_uint8_canopy_roundtrip_and_explicit_old_store_rejection(tmp_path):
    import json

    store = completed(tmp_path / "store")
    assert store.tile(0, 0).canopy.dtype == np.uint8
    assert store.window(GRID, (3, 3))["canopy"].dtype == np.uint8
    spec_path = tmp_path / "store/store.json"
    spec = json.loads(spec_path.read_text())
    spec["contract"] = "viewshed_direct_transmission_tile_store_v1"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="Unsupported prepared store version"):
        PreparedTileStore(tmp_path / "store")


def test_compact_writer_never_truncates_fractional_canopy_or_persists_sentinel(tmp_path):
    with writer(tmp_path / "store") as owner:
        fields = arrays()
        fields["canopy"] = fields["canopy"].astype("float32")
        fields["canopy"][0, 0] = 1.25
        with pytest.raises(ValueError, match="dtype mismatch"):
            owner.write_tile(0, 0, fields)
        fields = arrays()
        fields["canopy"][0, 0] = 255
        with pytest.raises(ValueError, match="Reserved canopy255"):
            owner.write_tile(0, 0, fields)


@pytest.mark.parametrize("cache_bytes", [0, 4 * 1024**2])
def test_each_tile_read_hashes_once_and_cache_hits_still_validate(
    tmp_path, monkeypatch, cache_bytes
):
    from viewshed_toolkit.pipeline.prepare.elevation import transmission_store as module

    root = tmp_path / "store"
    completed(root)
    store = PreparedTileStore(root, cache_bytes=cache_bytes)
    original = module._hash
    paths = []

    def record(path):
        paths.append(path)
        return original(path)

    monkeypatch.setattr(module, "_hash", record)
    store.tile(0, 0)
    store.tile(0, 0)
    assert paths == [root / "tile-0-0.npz"] * 2
    assert store.telemetry["hash_checks"] == 2
    assert store.telemetry["cache_hits"] == (1 if cache_bytes else 0)
    assert store.telemetry["cache_misses"] == (1 if cache_bytes else 2)
    assert store.bytes <= cache_bytes
    (root / "tile-0-0.npz").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        store.tile(0, 0)


@pytest.mark.parametrize("corrupt", [False, True])
def test_window_consumes_cached_overlap_before_eviction_and_checks_its_bytes(tmp_path, corrupt):
    root = tmp_path / "store"
    with PreparedTileWriter(
        root, IDENTITY, GRID, (384, 1152), [(0, 0), (0, 384), (0, 768)], max_bytes=20 * 1024**2
    ) as owner:
        for col in [0, 384, 768]:
            fields = arrays()
            fields["observer_ground"][:] = col
            owner.write_tile(0, col, fields)
        owner.complete()
    reference = PreparedTileStore(root, cache_bytes=0).window(GRID, (384, 1152))
    store = PreparedTileStore(root, cache_bytes=4 * 1024**2)
    store.tile(0, 768)
    if corrupt:
        path = root / "tile-0-768.npz"
        path.write_bytes(path.read_bytes() + b"changed")
        with pytest.raises(ValueError, match="checksum"):
            store.window(GRID, (384, 1152))
        assert store.telemetry["cache_misses"] == 1
        return
    actual = store.window(GRID, (384, 1152))
    for name in DTYPES:
        assert actual[name].tobytes() == reference[name].tobytes()
        assert not actual[name].flags.writeable
    assert store.telemetry["cache_hits"] == 1
    assert store.telemetry["cache_misses"] == 3  # priming plus two window misses
    assert store.telemetry["hash_checks"] == 4
    assert store.bytes <= store.cache_bytes


def test_future_tile_reuse_preserves_bytes_budget_and_corruption_checks(tmp_path):
    root = tmp_path / "planned"
    keys = [(0, i * 384) for i in range(4)]
    with PreparedTileWriter(
        root, IDENTITY, GRID, (384, 1536), keys, max_bytes=20 * 1024**2
    ) as owner:
        for i, key in enumerate(keys):
            a = arrays()
            a["ground"][:] = i
            owner.write_tile(*key, a)
        owner.complete()
    size = sum(a.nbytes for a in arrays().values()) + 384 * 16
    regular = PreparedTileStore(root, cache_bytes=size)
    planned = PreparedTileStore(root, cache_bytes=size)
    windows = [keys, [keys[0]], [keys[0]], [keys[0]]]
    planned.plan_windows(windows)
    for ks in windows:
        grid = GRID * Affine.translation(ks[0][1], 0)
        shape = (384, len(ks) * 384)
        actual, expected = planned.window(grid, shape), regular.window(grid, shape)
        for field in DTYPES:
            assert actual[field].tobytes() == expected[field].tobytes()
        assert planned.bytes <= size
    assert planned.telemetry["cache_misses"] < regular.telemetry["cache_misses"]
    assert planned.telemetry["hash_checks"] == regular.telemetry["hash_checks"] == 7
    from viewshed_toolkit.pipeline.contracts.transmission_store import tile_name

    path = root / (tile_name(*keys[0]) + ".npz")
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        planned.window(GRID, (384, 384))


def test_invalid_lookahead_cannot_reference_missing_tile(tmp_path):
    store = completed(tmp_path / "store")
    with pytest.raises(ValueError, match="absent"):
        store.plan_windows([[(0, 384)]])
