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
    EXPORT.seal_manifest(manifest)
    EXPORT.write(output / "manifest.json", manifest)


@pytest.mark.parametrize(
    "field", ["analysis_resolution_m", "crs", "assumptions", "source_vintages"]
)
def test_checker_rejects_scientific_manifest_relabeling(tmp_path, field):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    manifest = EXPORT.read(output / "manifest.json")
    if field == "analysis_resolution_m":
        manifest[field] += 1
    elif field == "crs":
        manifest[field] = "EPSG:4326"
    elif field == "assumptions":
        manifest[field]["viewshed"]["observer_canopy_clearance_radius_m"] += 1
    else:
        manifest[field][0]["source_year"] = 1999
    EXPORT.write(output / "manifest.json", manifest)
    with pytest.raises(ValueError, match=r"semantic|identity|evidence|contract"):
        EXPORT.check(output)


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


@pytest.mark.parametrize("mutation", ["state", "canopy_kernel", "missing_evidence"])
def test_checker_rejects_inconsistent_scientific_evidence(tmp_path, mutation):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    data = EXPORT.read(output / "pairs.json")
    columns = data["columns"]
    row = next(row for row in data["rows"] if row[columns.index("source_type")] == "land")
    field, value = {
        "state": ("vegetation_state", "not_applicable"),
        "canopy_kernel": ("canopy_distance_weighted_los_support", 1.0),
        "missing_evidence": ("line_of_sight_support", None),
    }[mutation]
    row[columns.index(field)] = value
    EXPORT.write(output / "pairs.json", data)
    reseal(output)
    with pytest.raises(ValueError):
        EXPORT.check(output)


@pytest.mark.parametrize(
    "field",
    ["analysis_resolution_m", "crs", "assumptions", "source_vintages", "candidate_universe"],
)
def test_resigned_manifest_still_rejects_contradictory_evidence(tmp_path, field):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    manifest = EXPORT.read(output / "manifest.json")
    if field == "analysis_resolution_m":
        manifest[field] += 1
    elif field == "crs":
        manifest[field] = "EPSG:4326"
    elif field == "assumptions":
        manifest[field]["viewshed"]["observer_canopy_clearance_radius_m"] += 1
    elif field == "candidate_universe":
        manifest[field]["pairs"] += 1
    else:
        manifest[field][0]["source_year"] = 1999
    EXPORT.seal_manifest(manifest)
    EXPORT.write(output / "manifest.json", manifest)
    with pytest.raises(ValueError, match=r"evidence|contradicts"):
        EXPORT.check(output)


@pytest.mark.parametrize(
    "mutation",
    ["default", "role", "lesson", "near_far", "effect", "little", "inverse", "obstruction"],
)
def test_checker_rejects_invalid_references_and_teaching_claims(tmp_path, mutation):
    output = tmp_path / "bundle"
    shutil.copytree(BUNDLE, output)
    manifest = EXPORT.read(output / "manifest.json")
    lessons = EXPORT.read(output / "lessons.json")
    by_id = {lesson["id"]: lesson for lesson in lessons}
    if mutation == "default":
        manifest["defaults"]["pair_id"] = "absent"
    elif mutation == "role":
        manifest["defaults"]["source_type"] = "water"
    elif mutation == "lesson":
        lessons[0]["pair_ids"] = ["absent"]
    elif mutation == "near_far":
        by_id["distance"]["pair_ids"].reverse()
    elif mutation in {"effect", "little"}:
        by_id["canopy"]["pair_ids"] = [
            by_id["canopy"]["pair_ids"][1 if mutation == "effect" else 0]
        ] * 2
    elif mutation == "inverse":
        by_id["inverse"]["pair_ids"][0] = by_id["distance"]["pair_ids"][0]
    else:
        profiles = EXPORT.read(output / "profiles.json")
        blocked = by_id["terrain"]["pair_ids"][-1]
        for profile in profiles:
            if profile["pair_id"] == blocked:
                for sample in profile["samples"]:
                    sample["ground_m"] = sample["ray_m"] - 1
        EXPORT.write(output / "profiles.json", profiles)
    EXPORT.write(output / "lessons.json", lessons)
    EXPORT.write(output / "manifest.json", manifest)
    reseal(output)
    with pytest.raises(ValueError):
        EXPORT.check(output)


@pytest.mark.parametrize("model", ["logistic", "exponential", "piecewise"])
def test_offline_curve_oracle_matches_production_with_shorter_cutoff(model):
    from dataclasses import asdict

    import numpy as np

    from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
    from viewshed_toolkit.pipeline.weights.distance.compute import distance_weight_values

    config = DistanceWeightConfig(selected_model=model, hard_cutoff_km=2, normalize_at_zero=True)
    contract = {"settings": asdict(config), "extent_km": 2}
    points = np.linspace(0, 2.5, 101)
    actual = distance_weight_values(points, config, max_distance_km=2)
    assert [EXPORT.curve_weight(float(point), contract) for point in points] == pytest.approx(
        actual, abs=1e-6
    )


def test_semantic_export_rejects_nonfinite_and_preserves_timestamp_identity():
    manifest = EXPORT.read(BUNDLE / "manifest.json")
    identity = manifest["bundle_id"]
    manifest["rendered_at"] = "documentation timestamp"
    EXPORT.seal_manifest(manifest)
    assert manifest["bundle_id"] == identity
    manifest["analysis_resolution_m"] = float("nan")
    with pytest.raises(ValueError):
        EXPORT.seal_manifest(manifest)
