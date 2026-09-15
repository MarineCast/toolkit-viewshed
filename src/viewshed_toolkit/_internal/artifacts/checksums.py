"""Canonical checksums for files and directory artifacts."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


def checksum_path(
    path: str | Path,
    *,
    logical_name: str | None = None,
) -> str:
    """Return the repository-standard checksum for a file or directory tree.

    ``logical_name`` lets a transaction checksum a staged file under its final
    destination name. Directory checksums always use paths relative to the
    directory root and therefore do not require an override.
    """

    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    if logical_name is not None and resolved.is_dir():
        raise ValueError("logical_name is supported only for file artifacts.")
    if logical_name is not None and Path(logical_name).name != logical_name:
        raise ValueError("logical_name must be a file name, not a path.")
    digest = hashlib.sha256()
    paths = (
        sorted(item for item in resolved.rglob("*") if item.is_file())
        if resolved.is_dir()
        else [resolved]
    )
    for item in paths:
        relative = (
            item.relative_to(resolved) if resolved.is_dir() else Path(logical_name or item.name)
        )
        digest.update(str(relative).encode("utf-8"))
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


__all__ = ["checksum_path"]


def _file_identity(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=64)
def _unchanged_file_digest(path_string: str, identity: tuple[int, ...], raw_bytes: bool) -> str:
    path = Path(path_string)
    if raw_bytes:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result = digest.hexdigest()
    else:
        result = checksum_path(path)
    if _file_identity(path) != identity:
        raise ValueError(f"Input changed while calculating checksum: {path}")
    return result


def checksum_unchanged_file(path: str | Path, *, raw_bytes: bool = False) -> str:
    """Reuse a file digest only while its filesystem change identity is unchanged.

    Inode and device detect replacement; ctime detects same-size rewrites even
    when callers restore mtime. The first read and every changed identity still
    use the full content checksum. Durable artifact checks use checksum_path.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValueError(f"Expected a regular file: {resolved}")
    identity = _file_identity(resolved)
    result = _unchanged_file_digest(str(resolved), identity, raw_bytes)
    if _file_identity(resolved) != identity:
        raise ValueError(f"Input changed while checking cached checksum: {resolved}")
    return result
