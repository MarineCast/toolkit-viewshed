"""Offline documentation checks use committed real derivatives, never acquisition."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "docs/assets/examples/san-juan"
SPEC = importlib.util.spec_from_file_location(
    "documentation_export", ROOT / "scripts/build_documentation_examples.py"
)
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


def reseal(output):
    manifest = EXPORT.read(output / "manifest.json")
    manifest["files"] = {
        name: EXPORT.digest((output / name).read_bytes()) for name in manifest["files"]
    }
    manifest["bundle_id"] = EXPORT.digest(
        EXPORT.encode({key: manifest[key] for key in ("export_contract", "generation_id", "files")})
    )
    EXPORT.write(output / "manifest.json", manifest)


def test_committed_real_bundle_checks_without_site_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts/build_documentation_examples.py"),
            "--check",
            "--output",
            str(BUNDLE),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["pairs"] == 5424


@pytest.mark.parametrize("mutation", ["bytes", "formula", "lesson", "index", "duplicate"])
def test_offline_checker_rejects_corrupt_bundle(tmp_path, mutation):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    if mutation == "bytes":
        (output / "coverage.json").write_text("{}")
    elif mutation == "formula":
        data = EXPORT.read(output / "pairs.json")
        data["rows"][0][data["columns"].index("distance_adjusted_viewability")] = 0.999
        EXPORT.write(output / "pairs.json", data)
        reseal(output)
    elif mutation == "lesson":
        data = EXPORT.read(output / "lessons.json")
        data[0]["assertions"][0]["value"] = 0.123456
        EXPORT.write(output / "lessons.json", data)
        reseal(output)
    elif mutation == "index":
        data = EXPORT.read(output / "indexes.json")
        data["inverse"] = {}
        EXPORT.write(output / "indexes.json", data)
        reseal(output)
    else:
        data = EXPORT.read(output / "pairs.json")
        data["rows"].append(data["rows"][0])
        EXPORT.write(output / "pairs.json", data)
        reseal(output)
    with pytest.raises(ValueError):
        EXPORT.check(output)


def test_docs_revision_is_informational_and_forward_inverse_share_exact_values(tmp_path):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    manifest = EXPORT.read(output / "manifest.json")
    old_id = manifest["bundle_id"]
    manifest["code_revision"] = "docs-only-commit"
    EXPORT.write(output / "manifest.json", manifest)
    assert EXPORT.check(output)["bundle_id"] == old_id
    pairs = {p["id"]: p for p in EXPORT.load_pairs(output)}
    indexes = EXPORT.read(output / "indexes.json")
    default = pairs[manifest["defaults"]["pair_id"]]
    assert default["id"] in indexes["forward"]["land:" + default["source_h3"]]
    assert default["id"] in indexes["inverse"]["land:" + default["target_h3"]]
    assert 0 < default["vegetation_attenuation"] < 1
    assert default["physical_viewability"] < default["line_of_sight_support"]
