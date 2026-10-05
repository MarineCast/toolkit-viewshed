"""Example-derived summaries preserve role identity, candidate coverage and missingness."""

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from render_documentation_walkthrough import BUNDLE, OUTPUT, check, target_summary  # noqa: E402


def pair(source, target="T", value=0.2, role="land"):
    return {
        "source_type": role,
        "source_h3": source,
        "target_h3": target,
        "distance_adjusted_viewability": value,
        "distance_adjusted_viewability_state": (
            "unavailable" if value is None else "derived_zero" if value == 0 else "positive"
        ),
    }


def test_summary_keeps_computed_zero_missing_and_no_contributor_distinct():
    records = [
        pair("A", "zero", 0),
        pair("B", "zero", 0),
        pair("A", "missing", None),
        pair("A", "partial", 0.3),
        pair("B", "partial", None),
    ]
    result = target_summary(records, ["zero", "missing", "partial", "outside"])
    rows = {r["target_h3"]: r for r in result["targets"]}
    assert rows["zero"]["maximum_valid_combined_weight"] == 0
    assert rows["zero"]["valid_count"] == 2
    assert rows["zero"]["state"] == "complete"
    assert rows["missing"]["maximum_valid_combined_weight"] is None
    assert rows["missing"]["missing_count"] == 1
    assert rows["missing"]["state"] == "unavailable"
    assert rows["outside"]["maximum_valid_combined_weight"] is None
    assert rows["outside"]["missing_count"] == 0
    assert rows["outside"]["outside_candidate_count"] == 2
    assert rows["outside"]["state"] == "outside_candidate_set"
    assert rows["partial"]["maximum_valid_combined_weight"] == 0.3
    assert rows["partial"]["valid_count"] == 1
    assert rows["partial"]["missing_count"] == 1
    assert rows["partial"]["state"] == "partial"


def test_summary_never_pools_roles_and_retains_tied_strongest_sources():
    records = [pair("A", value=0.1), pair("B", value=0.1), pair("A", value=0.9, role="water")]
    land = target_summary(records)
    assert land["targets"][0]["maximum_valid_combined_weight"] == 0.1
    assert land["targets"][0]["strongest_sources"] == ["A", "B"]
    assert land["targets"][0]["candidate_count"] == 2
    assert (
        target_summary(records, source_role="water")["targets"][0]["maximum_valid_combined_weight"]
        == 0.9
    )
    assert target_summary(list(reversed(records))) == land


def test_summary_rejects_duplicate_pairs_even_in_unselected_role():
    with pytest.raises(ValueError, match="Duplicate"):
        target_summary([pair("A", role="water"), pair("A", role="water")])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.01])
def test_summary_rejects_nonfinite_and_out_of_range_weights(value):
    with pytest.raises(ValueError):
        target_summary([pair("A", value=value)])


def test_summary_rejects_nonbaseline_scenarios_and_mismatched_states():
    record = pair("A")
    record["scenario"] = "another-run"
    with pytest.raises(ValueError, match="scenarios"):
        target_summary([record])
    record = pair("A", value=None)
    record["distance_adjusted_viewability_state"] = "derived_zero"
    with pytest.raises(ValueError, match="computed state"):
        target_summary([record])


def test_empty_summary_retains_explicit_target_as_unavailable():
    row = target_summary([], ["T"])["targets"][0]
    assert row["maximum_valid_combined_weight"] is None
    assert row["candidate_count"] == row["valid_count"] == row["missing_count"] == 0


def test_committed_presentation_has_original_scientific_identity_and_checked_assets():
    assert check()["static_figure_variants"] == 24
    science = json.loads((BUNDLE / "manifest.json").read_text())
    display = json.loads((OUTPUT / "manifest.json").read_text())
    summary = json.loads((OUTPUT / "target-summary.json").read_text())
    assert display["generation_id"] == summary["generation_id"] == science["generation_id"]
    assert (
        display["scientific_bundle_id"] == summary["scientific_bundle_id"] == science["bundle_id"]
    )
    assert summary["source_role"] == "land"
    assert len(summary["included_sources"]) == 84
    raw = json.loads((BUNDLE / "pairs.json").read_text())
    pairs = [dict(zip(raw["columns"], r, strict=True)) for r in raw["rows"]]
    for target in summary["targets"]:
        records = [
            p for p in pairs if p["source_type"] == "land" and p["target_h3"] == target["target_h3"]
        ]
        assert target["maximum_valid_combined_weight"] == max(
            (p["distance_adjusted_viewability"] for p in records), default=None
        )
        assert target["valid_count"] == len(records)


@pytest.mark.parametrize(
    "name", ["target-summary.json", "pair-map.svg", "profile-mobile-dark.svg", "manifest.json"]
)
def test_presentation_check_rejects_changed_artifacts(tmp_path, name):
    output = tmp_path / "presentation"
    shutil.copytree(OUTPUT, output)
    (output / name).write_bytes((output / name).read_bytes() + b" ")
    with pytest.raises(ValueError, match="differs"):
        check(output=output)


def test_guided_page_has_no_hidden_essential_chapters_or_execution():
    source = (ROOT / "docs/examples.md").read_text()
    assert source.count('<section class="chapter"') == 3
    assert source.count("<figure class=") == 6
    assert "{{contributors}}" in source
    assert source.index("<details") > source.index(
        "</section>", source.index('id="target-summary"')
    )
    assert "```" not in source
    assert "viewshed-explorer" not in source


def test_regional_maps_retain_every_exported_target_corner_without_clipping():
    import re
    import xml.etree.ElementTree as ET

    for name, width, height in (
        ("target-summary.svg", 960, 530),
        ("target-summary-mobile.svg", 480, 390),
    ):
        root = ET.parse(OUTPUT / name).getroot()
        targets = [node for node in root.iter() if "data-target" in node.attrib]
        assert len(targets) == 229
        for node in targets:
            coordinates = [float(n) for n in re.findall(r"-?\d+\.\d+", node.attrib["d"])]
            assert all(0 <= x <= width for x in coordinates[::2]), node.attrib["data-target"]
            assert all(0 <= y <= height for y in coordinates[1::2]), node.attrib["data-target"]
