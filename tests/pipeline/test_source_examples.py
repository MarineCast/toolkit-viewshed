"""Source selection, shared structure and full-region aggregation invariants."""

import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import h3
import pyarrow as pa
import pytest
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from build_regional_overview import aggregate  # noqa: E402
from render_lime_kiln import METRICS, map_figure, records  # noqa: E402
from render_source_examples import ASSETS, cases, check, markdown  # noqa: E402
from build_documentation_examples import read  # noqa: E402


def test_three_places_share_chapters_and_keep_their_original_records():
    headings = []
    for key, c in cases().items():
        text = markdown(key)
        headings.append([line for line in text.splitlines() if line.startswith("## ")])
        assert "{{" not in text
        assert text.count('class="chapter"') == 7
        assert 'class="example-hero"' in text
        assert text.count(".example-tab") == 4
        assert text.count('aria-current="page"') == 1
        source = c.get("source", "8728d1061ffffff")
        rows = records(ASSETS / key, source)
        assert len(rows) == {"lime-kiln": 661, "everett": 242, "larrabee": 285}[key]
        assert (
            sum(r["distance_adjusted_viewability"] > 0 for r in rows)
            == {"lime-kiln": 35, "everett": 45, "larrabee": 0}[key]
        )
    assert headings[0] == headings[1] == headings[2]
    assert "highest terrain weight" in markdown("larrabee")
    assert check()["source_walkthroughs"] == 3


@pytest.mark.parametrize("key", ["everett", "larrabee"])
def test_source_centroids_and_all_map_values_match_retained_geometry(key):
    c = cases()[key]
    g = read(ASSETS / key / "geometry.json")
    rows = records(ASSETS / key, c["source"])
    lat, lon = h3.cell_to_latlng(c["source"])
    assert c["place"] == pytest.approx(
        Transformer.from_crs(4326, 32610, always_xy=True).transform(lon, lat), abs=0.01
    )
    for name, (field, _) in METRICS.items():
        svg = ET.fromstring(
            map_figure(
                rows,
                name,
                geometry=g,
                source=c["source"],
                place=c["place"],
                place_name=c["name"],
                labels=[],
            )
        )
        targets = {
            n.attrib["data-target"]: float(n.attrib["data-value"])
            for n in svg.iter()
            if "data-target" in n.attrib
        }
        assert targets == {r["target_h3"]: r[field] for r in rows}


def test_aggregate_retains_missing_zero_and_candidate_counts():
    table = pa.table(
        {
            "source_h3": ["a", "b", "c", "d"],
            "target_h3": ["x", "x", "y", "z"],
            "weight_static_viewability": [0.2, None, 0.0, None],
        }
    )
    rows = {r["target_h3"]: r for r in aggregate(table)}
    assert rows["x"]["maximum_valid_combined_weight"] == 0.2
    assert rows["x"]["state"] == "partial"
    assert rows["x"]["candidate_count"] == 2 and rows["x"]["missing_count"] == 1
    assert rows["y"]["maximum_valid_combined_weight"] == 0 and rows["y"]["state"] == "complete"
    assert (
        rows["z"]["maximum_valid_combined_weight"] is None and rows["z"]["state"] == "unavailable"
    )
    assert aggregate(table.take(pa.array([3, 1, 0, 2]))) == aggregate(table)
    with pytest.raises(ValueError, match="Duplicate"):
        aggregate(pa.concat_tables([table, table]))


def test_full_regional_target_population_and_render_integrity():
    from render_regional_overview import check as check_region, evidence, figure

    data = evidence()
    assert data["source_type"] == "land"
    assert data["source_count"] == 26094
    assert data["candidate_pair_count"] == 5555318
    assert len({r["target_h3"] for r in data["rows"]}) == 13461
    assert sum(r["missing_count"] for r in data["rows"]) == 0
    assert check_region()["regional_targets"] == 13461
    # Navigation wrappers may change presentation, never the native target values.
    svg = ET.fromstring(figure())
    displayed = {
        node.attrib["data-target"]: float(node.attrib["data-value"])
        for node in svg.iter()
        if "data-target" in node.attrib
    }
    assert displayed == {
        row["target_h3"]: row["maximum_valid_combined_weight"] for row in data["rows"]
    }
    assert sum("data-map-layer" in node.attrib for node in svg.iter()) == 1


def test_html_map_preserves_native_boundaries_values_and_valid_geojson_rings():
    from render_regional_overview import OUTPUT, evidence

    rows = {row["target_h3"]: row for row in evidence()["rows"]}
    features = read(OUTPUT / "map-targets.geojson")["features"]
    assert len(features) == len(rows)
    assert {feature["id"] for feature in features} == rows.keys()
    for feature in features:
        props = feature["properties"]
        assert props["source_type"] == "land"
        assert all(props[key] == value for key, value in rows[feature["id"]].items())
        ring = feature["geometry"]["coordinates"][0]
        expected = [[lon, lat] for lat, lon in h3.cell_to_boundary(feature["id"])]
        assert ring[0] == ring[-1]
        # Native H3/libm may differ by a few ulps between macOS and Linux.
        # This absolute tolerance is about 0.1 micrometre in latitude; values,
        # identity, vertex order and stored ring closure remain exact checks.
        for actual, vertex in zip(ring, [*expected, expected[0]], strict=True):
            assert actual == pytest.approx(vertex, rel=0, abs=1e-12)
        assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in ring)


def test_standalone_html_map_has_all_area_shortcuts_and_local_dependencies():
    from html.parser import HTMLParser
    from render_regional_overview import OUTPUT

    class MapParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.options = []
            self.local_assets = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "option":
                self.options.append(attrs["value"])
            if tag in {"link", "script"}:
                self.local_assets.append(attrs.get("href", attrs.get("src")))

    parser = MapParser()
    parser.feed((OUTPUT / "map.html").read_text())
    assert parser.options == ["region", "lime", "everett", "larrabee"]
    assert all((OUTPUT / path).is_file() for path in parser.local_assets)
