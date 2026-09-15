"""Unchanged-input caching retains content identities and detects hidden rewrites."""

import hashlib
import os

import pytest

from viewshed_toolkit._internal.artifacts import checksums
from viewshed_toolkit.pipeline.prepare.elevation.cache import input_signature


@pytest.mark.parametrize("raw_bytes", [False, True])
def test_cached_checksum_reuses_reads_and_detects_restored_mtime(tmp_path, raw_bytes):
    path = tmp_path / "input.bin"
    path.write_bytes(b"aaaa")
    checksums._unchanged_file_digest.cache_clear()
    expected = hashlib.sha256(b"aaaa").hexdigest() if raw_bytes else checksums.checksum_path(path)
    assert checksums.checksum_unchanged_file(path, raw_bytes=raw_bytes) == expected
    assert checksums.checksum_unchanged_file(path, raw_bytes=raw_bytes) == expected
    assert checksums._unchanged_file_digest.cache_info().hits == 1
    original = path.stat()
    path.write_bytes(b"bbbb")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    changed = checksums.checksum_unchanged_file(path, raw_bytes=raw_bytes)
    assert changed != expected
    assert checksums._unchanged_file_digest.cache_info().misses == 2
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"cccc")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    assert checksums.checksum_unchanged_file(path, raw_bytes=raw_bytes) != changed


def test_raster_input_signature_keeps_original_raw_digest(tmp_path):
    path = tmp_path / "input.tif"
    path.write_bytes(b"raster bytes")
    assert input_signature(path)["sha256"] == hashlib.sha256(b"raster bytes").hexdigest()
