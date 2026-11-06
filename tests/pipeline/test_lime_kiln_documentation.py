"""Presentation checks protect source selection, metric identity and neutral states."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from render_lime_kiln import METRICS, SOURCE, check, map_figure, records  # noqa: E402


def test_lime_kiln_uses_all_original_land_pairs_and_true_weight_fields():
    family = records()
    assert len(family) == 661
    assert all(p["source_h3"] == SOURCE and p["source_type"] == "land" for p in family)
    assert METRICS["terrain"][0] == "distance_weighted_los_support"
    assert METRICS["vegetation"][0] == "vegetation_attenuation"
    for pair in family:
        assert pair["distance_adjusted_viewability"] == pytest.approx(
            pair["distance_weighted_los_support"] * pair["vegetation_attenuation"], abs=1e-7
        )
    assert check()["regional_delivery_unchanged"]


@pytest.mark.parametrize("mobile", [False, True])
def test_each_map_retains_same_candidates_values_and_neutral_state(mobile):
    family = records()
    expected = {p["target_h3"]: p for p in family}
    for name, (field, _) in METRICS.items():
        root = ET.fromstring(map_figure(family, name, mobile))
        targets = {n.attrib["data-target"]: n for n in root.iter() if "data-target" in n.attrib}
        assert set(targets) == set(expected)
        for target, node in targets.items():
            row = expected[target]
            assert float(node.attrib["data-value"]) == row[field]
            if (
                name == "vegetation"
                and row["distance_weighted_los_support"] == 0
                and row["vegetation_attenuation"] == 1
            ):
                assert node.attrib["fill"] == "url(#neutral)"
                assert float(node.attrib["data-value"]) == 1
                assert node.attrib["data-state"] == row["vegetation_state"]
                assert node.attrib["data-display-state"] == "no_baseline_support"
            elif row[field] == 0:
                assert node.attrib["fill"] == "#dce0df"


def test_lighthouse_locator_is_in_selected_native_cell_and_matches_projection():
    import h3
    from pyproj import Transformer
    from render_lime_kiln import PLACE

    assert h3.latlng_to_cell(48.5158, -123.1525, 7) == SOURCE
    projected = Transformer.from_crs(4326, 32610, always_xy=True).transform(-123.1525, 48.5158)
    assert projected == pytest.approx(PLACE, abs=0.01)


def test_photo_locator_uses_published_geotag_and_keeps_unlocated_panorama_unpinned():
    import json
    import h3
    from pyproj import Transformer
    from render_lime_kiln import OUTPUT, shoreline_figure

    photos = json.loads((OUTPUT / "shoreline-sources.json").read_text())["photos"]
    assert photos[0]["camera"] is None
    camera = photos[1]["camera"]
    assert h3.latlng_to_cell(camera["lat"], camera["lon"], 7) == SOURCE
    expected = Transformer.from_crs(4326, 32610, always_xy=True).transform(
        camera["lon"], camera["lat"]
    )
    assert camera["epsg32610"] == pytest.approx(expected, abs=0.01)
    root = ET.fromstring(shoreline_figure())
    assert [n.attrib["data-photo"] for n in root.iter() if "data-photo" in n.attrib] == ["A"]


def test_regional_curve_and_centroid_distances_match_independent_calculation():
    import math
    import h3

    for row in records():
        lat1, lon1 = map(math.radians, h3.cell_to_latlng(SOURCE))
        lat2, lon2 = map(math.radians, h3.cell_to_latlng(row["target_h3"]))
        a = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        )
        distance = 6371.0088 * 2 * math.asin(math.sqrt(a))
        assert row["distance_km"] == pytest.approx(distance, abs=2e-6)
        expected = (
            (1 + math.exp(-9 / 2.7)) / (1 + math.exp((row["distance_km"] - 9) / 2.7))
            if row["distance_km"] <= 30
            else 0
        )
        assert row["distance_detection_weight"] == pytest.approx(expected, abs=1e-7)
    assert sum(row["distance_adjusted_viewability"] > 0 for row in records()) == 35


def test_regional_excerpt_rejects_edited_data_without_resealing(tmp_path, monkeypatch):
    import shutil
    import render_lime_kiln as renderer

    for name in ("regional-source.json", "source-pairs.json", "geometry.json"):
        shutil.copyfile(renderer.OUTPUT / name, tmp_path / name)
    with (tmp_path / "source-pairs.json").open("a") as stream:
        stream.write(" ")
    monkeypatch.setattr(renderer, "OUTPUT", tmp_path)
    with pytest.raises(ValueError, match="checksum mismatch"):
        renderer.records()
