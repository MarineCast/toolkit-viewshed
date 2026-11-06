"""Lossless persistent canonical tiles, explicit publication and read-only xarray views.

NPZ is the storage codec; xarray names and aligns layers without replacing canonical
Rasterio preparation or the native ray kernel. A completed store is immutable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import xarray as xr
from affine import Affine

from ...contracts.transmission import DTYPES
from ...contracts.transmission_store import (
    STORE_CONTRACT,
    STORE_CRS,
    TILE_SIZE,
    canonical_bytes,
    metadata_hash,
    tile_name,
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe store file: {path.name}")
    return json.loads(path.read_bytes())


def _pending_json(path: Path) -> Any:
    """Truncated atomic metadata is evidence, never authority for another filename."""
    try:
        return _read(path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _atomic(path: Path, payload: bytes, *, temporary_suffix: str = ".part") -> None:
    temporary = path.with_suffix(path.suffix + temporary_suffix)
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _spec(
    identity: dict[str, Any],
    grid: Affine,
    shape: tuple[int, int],
    keys: Any,
    *,
    max_bytes: int | None = None,
) -> Any:
    if not isinstance(identity, dict) or not identity:
        raise ValueError("Explicit nonempty scientific identity required")
    if grid.a != 10 or grid.e != -10 or grid.b or grid.d or not np.isfinite(tuple(grid)).all():
        raise ValueError("Finite north-up 10m lattice required")
    if len(shape) != 2 or any(type(n) is not int or n <= 0 or n % 3 for n in shape):
        raise ValueError("Positive 30m-nested global shape required")
    normalized = []
    for row, col in keys:
        tile_name(row, col)
        if row >= shape[0] or col >= shape[1]:
            raise ValueError("Tile outside domain")
        normalized.append([row, col])
    if not normalized or len({tuple(k) for k in normalized}) != len(normalized):
        raise ValueError("Nonempty unique required tiles needed")
    result = json.loads(
        canonical_bytes(
            dict(
                contract=STORE_CONTRACT,
                identity=identity,
                grid=list(grid)[:6],
                shape=list(shape),
                required_keys=sorted(normalized),
                tile_size=TILE_SIZE,
                crs=STORE_CRS,
                dtypes=DTYPES,
                codec={
                    "container": "npz",
                    "compression": "deflate",
                    "numpy": np.__version__,
                    "cf_decoding": False,
                },
            )
        )
    )
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Positive persisted storage cap required")
        result["max_bytes"] = max_bytes
    return result


def _arrays(arrays: Any) -> None:
    if set(arrays) != set(DTYPES):
        raise ValueError("Prepared tile fields mismatch")
    for name, dtype in DTYPES.items():
        value = arrays[name]
        if not isinstance(value, np.ndarray) or value.shape != (TILE_SIZE, TILE_SIZE):
            raise ValueError("Prepared tile shape mismatch")
        if value.dtype != np.dtype(dtype):
            raise ValueError("Prepared tile dtype mismatch")
    if (arrays["canopy"] == 255).any():
        raise ValueError("Reserved canopy255 requires zero lower bound and separate missing mask")


def _inventory(root: Path, keys: Any, *, writer: bool = False, complete: bool = False) -> None:
    allowed = {"store.json", "sidecars.json"}
    if (root / "sidecars.json").exists():
        sidecars = _read(root / "sidecars.json")
        for name in sidecars:
            _sidecar_name(name)
            allowed.add(name)
    if writer:
        allowed.add("writer.lock")
    if complete:
        allowed.add("complete.json")
    for row, col in keys:
        name = tile_name(row, col)
        allowed.update({name + ".npz", name + ".json"})
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.name not in allowed:
            raise ValueError(f"Unexpected or unsafe store entry: {path.name}")


def _sidecar_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]*\.(json|wkb|npz)", name)
        or name
        in {
            "store.json",
            "complete.json",
            "sidecars.json",
            "sidecar-intent.json",
            "recovery-intent.json",
        }
        or name.startswith("tile-")
    ):
        raise ValueError("Unsafe or reserved sidecar name")


def _clear_sidecar_intent(root: Path) -> None:
    (root / "sidecar-intent.json").unlink()
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sidecars(root: Path) -> Any:
    entries = _read(root / "sidecars.json")
    if not isinstance(entries, dict):
        raise ValueError("Malformed sidecar inventory")
    for name, receipt in entries.items():
        _sidecar_name(name)
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or unsafe sidecar")
        if receipt != dict(sha256=_hash(path), bytes=path.stat().st_size):
            raise ValueError("Sidecar checksum mismatch")
    return entries


def _tile_receipt(root: Path, row: int, col: int, spec_hash: str) -> Any:
    name = tile_name(row, col)
    receipt = _read(root / (name + ".json"))
    path = root / (name + ".npz")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or unsafe tile")
    if receipt != dict(
        row=row, col=col, spec_hash=spec_hash, sha256=_hash(path), bytes=path.stat().st_size
    ):
        raise ValueError("Prepared tile receipt/checksum mismatch")
    return receipt


def _decode_tile(root: Path, row: int, col: int) -> Any:
    """Decode only after the caller validates this load's durable receipt."""
    path = root / (tile_name(row, col) + ".npz")
    with np.load(path, allow_pickle=False) as loaded:
        arrays = {name: loaded[name] for name in loaded.files}
    _arrays(arrays)
    for value in arrays.values():
        value.flags.writeable = False
    return arrays


def _load_tile(root: Path, row: int, col: int, spec_hash: str) -> tuple[Any, Any]:
    receipt = _tile_receipt(root, row, col, spec_hash)
    return _decode_tile(root, row, col), receipt


class PreparedTileWriter:
    """Single owner writer; failures remain visible and require explicit recovery."""

    def __init__(
        self,
        root: Any,
        identity: dict[str, Any],
        grid: Affine,
        shape: tuple[int, int],
        required_keys: Any,
        *,
        max_bytes: int,
        resume: bool = False,
    ) -> None:
        self.root = Path(root)
        self.spec = _spec(identity, grid, shape, required_keys, max_bytes=max_bytes)
        self.spec_hash = metadata_hash(self.spec)
        self.keys = {tuple(key) for key in self.spec["required_keys"]}
        self.max_bytes = max_bytes
        self.closed = True
        if type(max_bytes) is not int or max_bytes <= 0 or self.root.is_symlink():
            raise ValueError("Positive storage cap and safe root required")
        if resume:
            if _read(self.root / "store.json") != self.spec:
                raise ValueError("Store identity mismatch")
            _inventory(self.root, self.keys)
            _sidecars(self.root)
            for row, col in self.keys:
                name = tile_name(row, col)
                present = [(self.root / (name + suffix)).exists() for suffix in (".npz", ".json")]
                if any(present) and not all(present):
                    raise ValueError("Unreceipted interrupted tile requires explicit recovery")
                if all(present):
                    _load_tile(self.root, row, col, self.spec_hash)
        else:
            self.root.mkdir(parents=True, exist_ok=False)
        with (self.root / "writer.lock").open("xb") as handle:
            handle.write(str(os.getpid()).encode())
        self.closed = False
        try:
            if not resume:
                self._reserve(len(canonical_bytes(self.spec)) + 4096)
                _atomic(self.root / "store.json", canonical_bytes(self.spec))
                _atomic(self.root / "sidecars.json", b"{}")
            self._reserve(0)
        except BaseException:
            self.close()
            raise

    def _reserve(self, additional: int) -> None:
        used = sum(path.stat().st_size for path in self.root.iterdir())
        if used + additional > self.max_bytes:
            raise ValueError("Prepared store storage cap exceeded")

    def has_tile(self, row: int, col: int) -> bool:
        if (row, col) not in self.keys:
            raise ValueError("Tile not declared in store")
        name = tile_name(row, col)
        if not (self.root / (name + ".json")).exists():
            if (self.root / (name + ".npz")).exists():
                raise ValueError("Unreceipted tile")
            return False
        _load_tile(self.root, row, col, self.spec_hash)
        return True

    def write_tile(self, row: int, col: int, arrays: Any) -> None:
        if self.closed:
            raise ValueError("Writer closed")
        if self.has_tile(row, col):
            raise ValueError("Tile already published; no overwrite")
        _arrays(arrays)
        reserve = sum(array.nbytes + 512 for array in arrays.values()) + 8192
        self._reserve(reserve)
        name = tile_name(row, col)
        path = self.root / (name + ".npz")
        temporary = path.with_suffix(".npz.part")
        with temporary.open("xb") as handle:
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > reserve - 4096:
            raise ValueError("Tile exceeded worst-case reservation")
        sha, size = _hash(temporary), temporary.stat().st_size
        os.replace(temporary, path)
        receipt = dict(row=row, col=col, spec_hash=self.spec_hash, sha256=sha, bytes=size)
        _atomic(self.root / (name + ".json"), canonical_bytes(receipt))

    def has_sidecar(self, name: str) -> bool:
        _sidecar_name(name)
        return name in _sidecars(self.root)

    def read_sidecar(self, name: str) -> bytes:
        _sidecar_name(name)
        if name not in _sidecars(self.root):
            raise ValueError("Sidecar missing")
        return (self.root / name).read_bytes()

    def write_sidecar(self, name: str, payload: bytes) -> None:
        if self.closed or not isinstance(payload, bytes):
            raise ValueError("Open writer and bytes payload required")
        _sidecar_name(name)
        intent_path = self.root / "sidecar-intent.json"
        if any(
            path.exists() or path.is_symlink()
            for path in (intent_path, intent_path.with_suffix(".json.part"))
        ):
            raise ValueError("Unresolved sidecar intention requires explicit recovery")
        entries = _sidecars(self.root)
        receipt = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
        if name in entries:
            if entries[name] != receipt:
                raise ValueError("Sidecar already published with different bytes")
            return
        entries[name] = receipt
        journal = canonical_bytes(entries)
        intention = canonical_bytes(dict(name=name, spec_hash=self.spec_hash, **receipt))
        self._reserve(len(payload) + len(journal) + len(intention) + 4096)
        # Intention is durable before the payload becomes visible. Recovery may
        # therefore qualify every named unreceipted sidecar without broad cleanup.
        _atomic(intent_path, intention)
        _atomic(self.root / name, payload)
        _atomic(self.root / "sidecars.json", journal)
        _clear_sidecar_intent(self.root)

    def complete(self) -> None:
        if self.closed:
            raise ValueError("Writer closed")
        _inventory(self.root, self.keys, writer=True)
        receipts = []
        for row, col in sorted(self.keys):
            _, receipt = _load_tile(self.root, row, col, self.spec_hash)
            receipts.append(receipt)
        payload = canonical_bytes(
            dict(
                contract=STORE_CONTRACT,
                spec_hash=self.spec_hash,
                tiles=receipts,
                sidecars=_sidecars(self.root),
            )
        )
        self._reserve(len(payload))
        _atomic(self.root / "complete.json", payload)
        self.close()

    def close(self) -> None:
        if not self.closed:
            (self.root / "writer.lock").unlink()
            self.closed = True

    def __enter__(self) -> PreparedTileWriter:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class PreparedTileStore:
    """Completed store reader: bounded RAM LRU, no writes or automatic rebuilding."""

    def __init__(
        self, root: Any, *, expected_identity: Any = None, cache_bytes: int = 64 * 1024**2
    ) -> None:
        self.root = Path(root)
        if self.root.is_symlink() or type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("Safe store root and nonnegative memory cap required")
        self.spec = _read(self.root / "store.json")
        if self.spec.get("contract") != STORE_CONTRACT:
            raise ValueError(
                "Unsupported prepared store version; rebuild compact uint8 canopy store"
            )
        normalized = _spec(
            self.spec["identity"],
            Affine(*self.spec["grid"]),
            tuple(self.spec["shape"]),
            self.spec["required_keys"],
            max_bytes=self.spec.get("max_bytes"),
        )
        if normalized != self.spec:
            raise ValueError("Unsupported or malformed store contract")
        if expected_identity is not None and canonical_bytes(expected_identity) != canonical_bytes(
            self.spec["identity"]
        ):
            raise ValueError("Store scientific identity mismatch")
        self.spec_hash = metadata_hash(self.spec)
        complete = _read(self.root / "complete.json")
        self.keys = {tuple(key) for key in self.spec["required_keys"]}
        _inventory(self.root, self.keys, complete=True)
        if (
            set(complete) != {"contract", "spec_hash", "tiles", "sidecars"}
            or complete["contract"] != STORE_CONTRACT
        ):
            raise ValueError("Malformed completion manifest")
        if complete["spec_hash"] != self.spec_hash:
            raise ValueError("Completion identity mismatch")
        receipts = complete["tiles"]
        if [(r["row"], r["col"]) for r in receipts] != sorted(self.keys):
            raise ValueError("Completion tile inventory mismatch")
        if complete["sidecars"] != _sidecars(self.root):
            raise ValueError("Completion sidecar inventory mismatch")
        self.manifest = dict(self.spec, completion=complete)
        self.sidecars = complete["sidecars"]
        self.receipts = {(r["row"], r["col"]): r for r in receipts}
        self.grid, self.shape = Affine(*self.spec["grid"]), tuple(self.spec["shape"])
        self.cache_bytes = cache_bytes
        self.cache: OrderedDict[tuple[int, int], xr.Dataset] = OrderedDict()
        self.bytes = 0
        self._prefetched: dict[Any, tuple[dict[str, Any], float]] = {}
        self.last_window_prefetch_seconds = 0.0
        self._window_uses: dict[Any, Any] = {}
        self._window_number = 0
        self.telemetry = dict(
            hash_seconds=0.0,
            decode_seconds=0.0,
            hash_checks=0,
            cache_hits=0,
            cache_misses=0,
            decoded_bytes=0,
        )

    def plan_windows(self, windows: Any) -> None:
        """Hint upcoming read sets without changing order, bytes or integrity checks.

        Stale/skipped hints affect only eviction priority. All reads still validate
        durable receipts. The schedule holds keys/indices, never raster arrays.
        """
        uses: dict[Any, Any] = defaultdict(deque)
        for i, keys in enumerate(windows):
            for key in set(tuple(k) for k in keys):
                if key not in self.keys:
                    raise ValueError("Planned tile absent from completed store")
                uses[key].append(i)
        self._window_uses = dict(uses)
        self._window_number = 0

    def _next_use(self, key: Any) -> float:
        uses = self._window_uses.get(key)
        return uses[0] if uses else math.inf

    def _tile(self, row: int, col: int) -> xr.Dataset:
        tile_name(row, col)
        key = row, col
        if key not in self.keys:
            raise ValueError("Tile not present in completed store")
        # Validate durable bytes even on a RAM hit; never hide external corruption.
        started = time.perf_counter()
        receipt = _tile_receipt(self.root, row, col, self.spec_hash)
        self.telemetry["hash_seconds"] += time.perf_counter() - started
        self.telemetry["hash_checks"] += 1
        if receipt != self.receipts[key]:
            raise ValueError("Tile differs from completion manifest")
        if key in self.cache:
            self.telemetry["cache_hits"] += 1
            dataset = self.cache.pop(key)
            self.cache[key] = dataset
            return dataset
        self.telemetry["cache_misses"] += 1
        started = time.perf_counter()
        arrays = _decode_tile(self.root, row, col)
        self.telemetry["decode_seconds"] += time.perf_counter() - started
        self.telemetry["decoded_bytes"] += sum(value.nbytes for value in arrays.values())
        transform = self.grid * Affine.translation(col, row)
        dataset = xr.Dataset(
            {name: (("y", "x"), value) for name, value in arrays.items()},
            coords={
                "x": transform.c + (np.arange(TILE_SIZE) + 0.5) * 10,
                "y": transform.f - (np.arange(TILE_SIZE) + 0.5) * 10,
            },
            attrs=dict(crs=STORE_CRS, transform=list(transform)[:6], row=row, col=col),
        )
        for value in dataset.variables.values():
            value.values.flags.writeable = False
        size = sum(value.nbytes for value in dataset.variables.values())
        while self.cache and self.bytes + size > self.cache_bytes:
            if self._window_uses:
                victim = max(self.cache, key=self._next_use)
                if self._next_use(key) >= self._next_use(victim):
                    return dataset
                old = self.cache.pop(victim)
            else:
                _, old = self.cache.popitem(last=False)
            self.bytes -= sum(value.nbytes for value in old.variables.values())
        if size <= self.cache_bytes:
            self.cache[key] = dataset
            self.bytes += size
        return dataset

    def tile(self, row: int, col: int) -> xr.Dataset:
        """Return isolated metadata and arrays, never a mutable cache alias.

        cache_bytes counts only the private retained chunks. A public tile adds one
        chunk-sized copy owned by the caller; window assembly reads the private
        chunk directly and accounts separately for its caller-owned output arrays.
        """
        dataset = self._tile(row, col).copy(deep=True)
        for value in dataset.variables.values():
            value.values.flags.writeable = False
        return dataset

    def read_sidecar(self, name: str) -> bytes:
        _sidecar_name(name)
        if name not in self.sidecars or _sidecars(self.root) != self.sidecars:
            raise ValueError("Sidecar missing or changed")
        return (self.root / name).read_bytes()

    def validate(self) -> None:
        _inventory(self.root, self.keys, complete=True)
        if _sidecars(self.root) != self.sidecars:
            raise ValueError("Completed sidecars changed")
        for row, col in sorted(self.keys):
            arrays, receipt = _load_tile(self.root, row, col, self.spec_hash)
            if receipt != self.receipts[row, col]:
                raise ValueError("Tile differs from completed manifest")
            del arrays

    def _window_spec(self, grid: Affine, shape: tuple[int, int]) -> Any:
        height, width = shape
        if any(type(n) is not int or n <= 0 or n % 3 for n in shape):
            raise ValueError("30m-nested source shape required")
        row_float, col_float = (self.grid.f - grid.f) / 10, (grid.c - self.grid.c) / 10
        row0, col0 = round(row_float), round(col_float)
        if (
            grid.a != 10
            or grid.e != -10
            or grid.b
            or grid.d
            or row0 % 3
            or col0 % 3
            or abs(row_float - row0) > 1e-8
            or abs(col_float - col0) > 1e-8
            or min(row0, col0) < 0
            or row0 + height > self.shape[0]
            or col0 + width > self.shape[1]
        ):
            raise ValueError("Source window outside or shifted off canonical lattice")
        keys = {
            (row, col)
            for row in range(row0 // TILE_SIZE * TILE_SIZE, row0 + height, TILE_SIZE)
            for col in range(col0 // TILE_SIZE * TILE_SIZE, col0 + width, TILE_SIZE)
        }
        if not keys <= self.keys:
            raise ValueError("Tile not present in completed store")
        return grid, shape, row0, col0, keys

    def _advance_windows(self, count: int) -> None:
        if self._window_uses:
            last = self._window_number + count - 1
            for uses in self._window_uses.values():
                while uses and uses[0] <= last:
                    uses.popleft()
            self._window_number += count

    def _assemble_windows(self, specs: Any) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = [
            {name: np.empty(spec[1], dtype=dtype) for name, dtype in DTYPES.items()}
            for spec in specs
        ]
        keys = sorted(set().union(*(spec[4] for spec in specs)))
        keys.sort(key=lambda key: key not in self.cache)
        self._advance_windows(len(specs))
        for row, col in keys:
            dataset = self._tile(row, col)
            for spec, arrays in zip(specs, outputs, strict=True):
                _, (height, width), row0, col0, required = spec
                if (row, col) not in required:
                    continue
                r0, c0 = max(row, row0), max(col, col0)
                r1 = min(row + TILE_SIZE, row0 + height)
                c1 = min(col + TILE_SIZE, col0 + width)
                for name in DTYPES:
                    arrays[name][r0 - row0 : r1 - row0, c0 - col0 : c1 - col0] = dataset[
                        name
                    ].values[r0 - row : r1 - row, c0 - col : c1 - col]
        for spec, arrays in zip(specs, outputs, strict=True):
            for value in arrays.values():
                value.flags.writeable = False
            arrays["transform"] = spec[0]
        return outputs

    def prefetch_windows(self, windows: Any, *, max_bytes: int) -> None:
        """Decode overlapping tiles once for a bounded batch of owned windows.

        Every consumed window rechecks its durable tile receipts. No prefetched
        array can hide corruption introduced after batch assembly.
        """
        if self._prefetched:
            raise ValueError("Previous raster batch has not been consumed")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("Nonnegative raster batch byte cap required")
        specs = [self._window_spec(grid, tuple(shape)) for grid, shape in windows]
        keys = [(spec[0], spec[1]) for spec in specs]
        if not specs or len(specs) > 2 or len(set(keys)) != len(keys):
            raise ValueError("One or two distinct source windows required")
        pixels = sum(spec[1][0] * spec[1][1] for spec in specs)
        required = pixels * sum(np.dtype(dtype).itemsize for dtype in DTYPES.values())
        if required > max_bytes:
            raise ValueError("Raster batch exceeds its byte cap")
        started = time.perf_counter()
        outputs = self._assemble_windows(specs)
        elapsed = time.perf_counter() - started
        self._prefetched = {
            key: (output, elapsed * spec[1][0] * spec[1][1] / pixels)
            for key, spec, output in zip(keys, specs, outputs, strict=True)
        }

    def discard_prefetched(self) -> None:
        """Release only in-memory batch buffers; never touch durable artifacts."""
        self._prefetched.clear()

    def window(self, grid: Affine, shape: tuple[int, int]) -> dict[str, Any]:
        self.last_window_prefetch_seconds = 0.0
        height, width = shape
        shape = height, width
        spec = self._window_spec(grid, shape)
        key = grid, shape
        if key not in self._prefetched:
            return self._assemble_windows([spec])[0]
        # Keep integrity checks at the point of consumption, including tiles
        # evicted from the decoded cache since the batch was assembled.
        for row, col in sorted(spec[4]):
            started = time.perf_counter()
            receipt = _tile_receipt(self.root, row, col, self.spec_hash)
            self.telemetry["hash_seconds"] += time.perf_counter() - started
            self.telemetry["hash_checks"] += 1
            if receipt != self.receipts[row, col]:
                raise ValueError("Tile differs from completion manifest")
        output, self.last_window_prefetch_seconds = self._prefetched.pop(key)
        return output


def recover_prepared_store(root: Any, expected_identity: dict[str, Any]) -> dict[str, Any]:
    """Explicitly recover an incomplete store after its owner has exited.

    Never steal a live lock or infer an undeclared cleanup path. Validate completed
    receipts before recording evidence and removing only uncommitted declared files.
    A PID that exists (including reuse) is conservatively treated as a live owner.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Safe existing store root required")
    spec = _read(root / "store.json")
    normalized = _spec(
        spec["identity"],
        Affine(*spec["grid"]),
        tuple(spec["shape"]),
        spec["required_keys"],
        max_bytes=spec.get("max_bytes"),
    )
    if normalized != spec or canonical_bytes(expected_identity) != canonical_bytes(
        spec["identity"]
    ):
        raise ValueError("Recovery scientific identity mismatch")
    if "max_bytes" not in spec:
        raise ValueError("Recovery requires persisted storage cap")
    if (root / "complete.json").exists():
        raise ValueError("Completed stores cannot be recovered")
    lock = root / "writer.lock"
    if lock.exists() or lock.is_symlink():
        if lock.is_symlink() or not lock.is_file():
            raise ValueError("Unsafe writer lock")
        try:
            raw_pid = lock.read_text()
            if not re.fullmatch(r"[1-9][0-9]*", raw_pid):
                raise ValueError("Unreadable writer PID")
            pid = int(raw_pid)
            if psutil.pid_exists(pid):
                raise ValueError("Writer PID is alive or reused; recovery refused")
        except (OSError, OverflowError) as exc:
            raise ValueError("Writer PID cannot be qualified") from exc
    with _recovery_owner(root):
        return _recover_incomplete(root, spec)


def _recover_incomplete(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    if (root / "recovery-intent.json").exists():
        return _finish_recovery(root, spec)
    lock = root / "writer.lock"
    spec_hash = metadata_hash(spec)
    keys = [tuple(key) for key in spec["required_keys"]]
    committed = _sidecars(root)
    sidecar_names = set(committed)
    allowed = {"store.json", "sidecars.json", "writer.lock", "recovery.lock"} | sidecar_names
    discard: set[Path] = set()
    for interrupted_intent in root.iterdir():
        if _recovery_intent_temporary(interrupted_intent.name):
            if interrupted_intent.is_symlink() or not interrupted_intent.is_file():
                raise ValueError("Unsafe interrupted recovery intention")
            fragment = interrupted_intent.read_bytes()
            prefix = b'{"previous_sidecars":'
            if not (prefix.startswith(fragment) or fragment.startswith(prefix)):
                raise ValueError("Malformed interrupted recovery intention")
            discard.add(interrupted_intent)
            allowed.add(interrupted_intent.name)
    intention_paths = [root / "sidecar-intent.json", root / "sidecar-intent.json.part"]
    present_intentions = [path for path in intention_paths if path.exists()]
    if len(present_intentions) > 1:
        raise ValueError("Ambiguous sidecar intention evidence")
    if present_intentions:
        intent_path = present_intentions[0]
        if intent_path.name.endswith(".part"):
            intent = _pending_json(intent_path)
        else:
            intent = _read(intent_path)
            if intent is None:
                raise ValueError("Malformed published sidecar intention")
        allowed.add(intent_path.name)
        discard.add(intent_path)
        if intent is not None and (
            not isinstance(intent, dict)
            or set(intent)
            != {
                "name",
                "spec_hash",
                "sha256",
                "bytes",
            }
        ):
            raise ValueError("Malformed sidecar intention")
        if intent is not None:
            _sidecar_name(intent["name"])
            if (
                intent["spec_hash"] != spec_hash
                or type(intent["bytes"]) is not int
                or intent["bytes"] < 0
                or not isinstance(intent["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", intent["sha256"])
            ):
                raise ValueError("Sidecar intention identity mismatch")
            name = intent["name"]
            expected_receipt = dict(sha256=intent["sha256"], bytes=intent["bytes"])
            if name in committed and committed[name] != expected_receipt:
                raise ValueError("Sidecar intention conflicts with committed evidence")
            sidecar_names.add(name)
            allowed.update({name, intent_path.name})
            discard.add(intent_path)
            payload = root / name
            if payload.exists():
                if payload.is_symlink() or expected_receipt != dict(
                    sha256=_hash(payload), bytes=payload.stat().st_size
                ):
                    raise ValueError("Uncommitted sidecar payload differs from intention")
                if name not in committed:
                    discard.add(payload)
            elif name in committed:
                raise ValueError("Committed sidecar payload missing")
    pending = root / "sidecars.json.part"
    if pending.exists():
        journal = _pending_json(pending)
        if journal is not None and (
            not isinstance(journal, dict) or any(journal.get(k) != v for k, v in committed.items())
        ):
            raise ValueError("Interrupted sidecar journal changed committed evidence")
        for name, receipt in (journal or {}).items():
            _sidecar_name(name)
            if set(receipt) != {"sha256", "bytes"}:
                raise ValueError("Malformed interrupted sidecar receipt")
            sidecar_names.add(name)
            if name not in committed:
                payload = root / name
                if payload.exists():
                    if payload.is_symlink() or receipt != dict(
                        sha256=_hash(payload), bytes=payload.stat().st_size
                    ):
                        raise ValueError("Interrupted sidecar evidence mismatch")
                    discard.add(payload)
        discard.add(pending)
        allowed.add(pending.name)
        allowed.update(sidecar_names)
    for name in sidecar_names:
        temporary = root / (name + ".part")
        if temporary.exists():
            discard.add(temporary)
            allowed.add(temporary.name)
    receipts = []
    for row, col in keys:
        name = tile_name(row, col)
        payload, receipt = root / (name + ".npz"), root / (name + ".json")
        allowed.update({payload.name, receipt.name})
        if receipt.exists():
            _, evidence = _load_tile(root, row, col, spec_hash)
            receipts.append(evidence)
        elif payload.exists():
            discard.add(payload)
        for suffix in (".npz.part", ".json.part"):
            temporary = root / (name + suffix)
            if temporary.exists():
                discard.add(temporary)
                allowed.add(temporary.name)
    complete_part = root / "complete.json.part"
    if complete_part.exists():
        attempted = _read(complete_part)
        expected = dict(
            contract=STORE_CONTRACT, spec_hash=spec_hash, tiles=receipts, sidecars=committed
        )
        if len(receipts) != len(keys) or attempted != expected:
            raise ValueError("Interrupted completion manifest mismatch")
        discard.add(complete_part)
        allowed.add(complete_part.name)
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.name not in allowed:
            raise ValueError(f"Unexpected or unsafe recovery entry: {path.name}")
    if lock.exists():
        # Check again immediately before changing durable evidence.
        if psutil.pid_exists(int(lock.read_text())):
            raise ValueError("Writer PID became alive; recovery refused")
        discard.add(lock)
    entries = [
        dict(name=path.name, sha256=_hash(path), bytes=path.stat().st_size)
        for path in sorted(discard)
    ]
    evidence = dict(
        spec_hash=spec_hash,
        identity=spec["identity"],
        removed=entries,
        validated_tiles=receipts,
    )
    if not entries:
        return dict(status="nothing_to_recover", **evidence)
    history = []
    if "recovery.json" in committed:
        history = json.loads((root / "recovery.json").read_bytes())["events"]
    history.append(evidence)
    payload = canonical_bytes(dict(contract=STORE_CONTRACT, events=history))
    journal = dict(committed)
    journal["recovery.json"] = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    intention = canonical_bytes(
        dict(
            spec_hash=spec_hash,
            previous_sidecars=committed,
            recovery_document=dict(contract=STORE_CONTRACT, events=history),
            uncommitted_sidecars=sorted(sidecar_names - set(committed)),
        )
    )
    used = sum(path.stat().st_size for path in root.iterdir())
    if (
        used + len(intention) + len(payload) + len(canonical_bytes(journal)) + 4096
        > spec["max_bytes"]
    ):
        raise ValueError("Recovery evidence exceeds persisted storage cap")
    _atomic(root / "recovery-intent.json", intention, temporary_suffix=f".{uuid.uuid4().hex}.part")
    return _finish_recovery(root, spec)


@contextmanager
def _recovery_owner(root: Path) -> Any:
    """Serialize stale-PID qualification without replacing another recovery's lock."""
    import fcntl

    path = root / "recovery.lock"
    created = False
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    succeeded = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Unsafe recovery lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Recovery owner is active") from exc
        opened = os.fstat(fd)
        named = path.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("Recovery lock changed during qualification")
        if created:
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
        else:
            raw_pid = os.read(fd, 64).decode()
            if not re.fullmatch(r"[1-9][0-9]*", raw_pid):
                raise ValueError("Unreadable recovery PID")
            try:
                if psutil.pid_exists(int(raw_pid)):
                    raise ValueError("Recovery PID is alive or reused; recovery refused")
            except (OSError, OverflowError) as exc:
                raise ValueError("Recovery PID cannot be qualified") from exc
        yield
        succeeded = True
    finally:
        # Keep stale lock evidence on rejection, and never unlink a replacement inode.
        try:
            named = path.stat(follow_symlinks=False)
            opened = os.fstat(fd)
            if (created or succeeded) and (named.st_dev, named.st_ino) == (
                opened.st_dev,
                opened.st_ino,
            ):
                path.unlink()
        finally:
            os.close(fd)


def _finish_recovery(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate then roll forward the one durable recovery intention, idempotently."""
    intent_path = root / "recovery-intent.json"
    intent = _read(intent_path)
    spec_hash = metadata_hash(spec)
    if (
        not isinstance(intent, dict)
        or set(intent)
        != {"spec_hash", "previous_sidecars", "recovery_document", "uncommitted_sidecars"}
        or intent["spec_hash"] != spec_hash
    ):
        raise ValueError("Recovery intention identity mismatch")
    before = intent["previous_sidecars"]
    document = intent["recovery_document"]
    if (
        not isinstance(before, dict)
        or not isinstance(document, dict)
        or set(document) != {"contract", "events"}
        or document["contract"] != STORE_CONTRACT
        or not isinstance(document["events"], list)
        or not document["events"]
    ):
        raise ValueError("Malformed recovery intention")
    payload = canonical_bytes(document)
    receipt = dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    after = dict(before, **{"recovery.json": receipt})
    journal = _read(root / "sidecars.json")
    if journal != before and journal != after:
        raise ValueError("Recovery intention conflicts with sidecar journal")
    if "recovery.json" in before:
        previous = canonical_bytes(dict(contract=STORE_CONTRACT, events=document["events"][:-1]))
        if before["recovery.json"] != dict(
            sha256=hashlib.sha256(previous).hexdigest(), bytes=len(previous)
        ):
            raise ValueError("Recovery intention changed prior evidence")
    elif len(document["events"]) != 1:
        raise ValueError("Recovery intention has uncommitted prior evidence")
    for name, expected in before.items():
        _sidecar_name(name)
        path = root / name
        actual = dict(sha256=_hash(path), bytes=path.stat().st_size) if path.is_file() else None
        if path.is_symlink() or (
            actual != expected and not (name == "recovery.json" and actual == receipt)
        ):
            raise ValueError("Recovery sidecar checksum mismatch")
    recovery = root / "recovery.json"
    if recovery.exists() and (
        recovery.is_symlink()
        or dict(sha256=_hash(recovery), bytes=recovery.stat().st_size)
        not in [before.get("recovery.json"), receipt]
    ):
        raise ValueError("Recovery evidence differs from intention")
    if journal == after and (not recovery.is_file() or _hash(recovery) != receipt["sha256"]):
        raise ValueError("Committed recovery evidence missing or changed")
    evidence = document["events"][-1]
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"spec_hash", "identity", "removed", "validated_tiles"}
        or evidence["spec_hash"] != spec_hash
        or evidence["identity"] != spec["identity"]
        or not isinstance(evidence["removed"], list)
    ):
        raise ValueError("Malformed recovery evidence")
    uncommitted = intent["uncommitted_sidecars"]
    if not isinstance(uncommitted, list) or len(set(uncommitted)) != len(uncommitted):
        raise ValueError("Malformed recovery sidecar names")
    for name in uncommitted:
        _sidecar_name(name)
        if name in before or name == "recovery.json":
            raise ValueError("Recovery cannot remove committed sidecars")
    removable = {
        "writer.lock",
        "sidecar-intent.json",
        "sidecar-intent.json.part",
        "sidecars.json.part",
        "complete.json.part",
        "recovery-intent.json.part",
    } | set(uncommitted)
    removable.update(name + ".part" for name in set(before) | set(uncommitted))
    allowed = {
        "store.json",
        "sidecars.json",
        "recovery.lock",
        "recovery-intent.json",
        "recovery.json",
    } | set(before)
    tiles = []
    for row, col in spec["required_keys"]:
        name = tile_name(row, col)
        removable.update({name + ".npz", name + ".npz.part", name + ".json.part"})
        if (root / (name + ".json")).exists():
            _, tile = _load_tile(root, row, col, spec_hash)
            tiles.append(tile)
            allowed.update({name + ".npz", name + ".json"})
            removable.remove(name + ".npz")
    if tiles != evidence["validated_tiles"]:
        raise ValueError("Recovery committed tiles changed")
    names = []
    for entry in evidence["removed"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"name", "sha256", "bytes"}
            or not isinstance(entry["name"], str)
            or (entry["name"] not in removable and not _recovery_intent_temporary(entry["name"]))
            or type(entry["bytes"]) is not int
            or entry["bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise ValueError("Malformed recovery removal evidence")
        name = entry["name"]
        names.append(name)
        allowed.add(name)
        path = root / name
        if path.exists() and (
            path.is_symlink()
            or not path.is_file()
            or dict(sha256=_hash(path), bytes=path.stat().st_size)
            != dict(sha256=entry["sha256"], bytes=entry["bytes"])
        ):
            raise ValueError("Interrupted evidence changed during recovery")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate recovery removal evidence")
    journal_bytes = canonical_bytes(after)
    temporaries = [
        (root / "recovery.json.part", payload),
        (root / "sidecars.json.recovery-part", journal_bytes),
    ]
    for path, expected_bytes in temporaries:
        if path.exists():
            if (
                path.is_symlink()
                or not path.is_file()
                or not expected_bytes.startswith(path.read_bytes())
            ):
                raise ValueError("Interrupted recovery publication differs from intention")
            allowed.add(path.name)
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.name not in allowed:
            raise ValueError(f"Unexpected or unsafe recovery entry: {path.name}")
    lock = root / "writer.lock"
    if lock.exists() and psutil.pid_exists(int(lock.read_text())):
        raise ValueError("Writer PID became alive; recovery refused")
    publish_payload = not recovery.exists() or _hash(recovery) != receipt["sha256"]
    publish_journal = journal != after
    used = sum(path.stat().st_size for path in root.iterdir())
    temporary_bytes = sum(path.stat().st_size for path, _ in temporaries if path.exists())
    additional = (len(payload) if publish_payload else 0) + (
        len(journal_bytes) if publish_journal else 0
    )
    if used - temporary_bytes + additional + 4096 > spec["max_bytes"]:
        raise ValueError("Recovery evidence exceeds persisted storage cap")
    # Every file has been qualified before the first mutation. Retrying does not append
    # another event, and missing declared removals mean a prior cleanup already succeeded.
    for path, _ in temporaries:
        if path.exists():
            path.unlink()
    if publish_payload:
        _atomic(recovery, payload)
    if publish_journal:
        _atomic(root / "sidecars.json", journal_bytes, temporary_suffix=".recovery-part")
    for entry in evidence["removed"]:
        path = root / entry["name"]
        if path.exists():
            if path.is_symlink() or _hash(path) != entry["sha256"]:
                raise ValueError("Interrupted evidence changed during recovery")
            path.unlink()
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
        intent_path.unlink()
        os.fsync(fd)
    finally:
        os.close(fd)
    return dict(status="recovered", **evidence)


def _recovery_intent_temporary(name: str) -> bool:
    return bool(re.fullmatch(r"recovery-intent\.json\.(?:[0-9a-f]{32}\.)?part", name))
