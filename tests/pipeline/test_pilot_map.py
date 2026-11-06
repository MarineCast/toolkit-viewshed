"""Single-entrypoint presentation contracts; no regional inputs or LOS."""

import json
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer

from viewshed_toolkit import export_pilot_map, export_raster_overlay_asset
from viewshed_toolkit.pipeline.visualization.comparison_maps import prepare_comparison_payload


def pilot_payload(tmp_path):
    transform = Affine(30, 0, 489500, 0, -30, 5333500)
    to_geo = Transformer.from_crs(32610, 4326, always_xy=True).transform
    data = dict(
        viewpoints={},
        composite_sources={},
        source_polygons={},
        points={},
        bounds=[-123.04, 48.1, -122.98, 48.18],
        land={"type": "FeatureCollection", "features": []},
        primary_visibility="above_ground_obstruction",
        raster_overlays=dict(sites={}, product_sites={}),
    )
    arrays = dict(
        dem=np.full((80, 80), 2.5, dtype="float32"),
        dem_original_delta=np.zeros((80, 80), dtype="float32"),
        chm_raw=np.full((80, 80), 12, dtype="uint8"),
        chm_lower=np.full((80, 80), 12, dtype="float32"),
        building=np.zeros((80, 80), dtype="float32"),
        joint=np.full((80, 80), 12, dtype="float32"),
        flags=np.zeros((80, 80), dtype="uint8"),
    )
    for i, name in enumerate(["Victoria", "Dungeness", "New coastal cell"]):
        source = "stu"[i]
        lon, lat = to_geo(490000 + i * 200, 5333000)
        polygon = [
            [lon + x, lat + y]
            for x, y in [
                (-0.002, -0.002),
                (0.002, -0.002),
                (0.002, 0.002),
                (-0.002, 0.002),
                (-0.002, -0.002),
            ]
        ]
        samples = [
            dict(sample_id=f"{source}_sample_{j:03d}", lon=lon + j * 0.0001, lat=lat + j * 0.0001)
            for j in range(5)
        ]
        data["composite_sources"][source] = dict(name=name, samples=samples)
        data["source_polygons"][source] = polygon
        row = dict(
            source_h3=source,
            target_h3="a",
            cell="a",
            polygon=polygon,
            n_observers=5,
            target_sample_count=20,
            canopy_clear_observations=40,
            canopy_zero_observations=40,
            canopy_unknown_observations=20,
            bare_clear_observations=60,
            obstruction_clear_observations=40,
            obstruction_zero_observations=40,
            obstruction_unknown_observations=20,
            weight_terrain=0.6,
            weight_distance=0.3,
            weight_obstruction_combined=None,
            obstruction_combined_lower=0.1,
            obstruction_combined_upper=0.3,
        )
        data["viewpoints"][source] = dict(rows=[row])
        asset = tmp_path / "raster_inputs" / (source + ".js")
        asset.parent.mkdir(exist_ok=True)
        export_raster_overlay_asset(
            source,
            arrays,
            transform=transform,
            crs=32610,
            output_path=asset,
            provenance={"source": name},
            preview_pixels=32,
        )
        data["raster_overlays"]["sites"][source] = dict(
            name=name, path="raster_inputs/" + source + ".js"
        )
        data["raster_overlays"]["product_sites"][source] = source
    samples = [p for source in data["composite_sources"].values() for p in source["samples"]]
    data["bounds"] = [
        min(p["lon"] for p in samples) - 0.015,
        min(p["lat"] for p in samples) - 0.015,
        max(p["lon"] for p in samples) + 0.015,
        max(p["lat"] for p in samples) + 0.015,
    ]
    return data


def test_single_entrypoint_preserves_population_and_has_no_dropdowns(tmp_path):
    data = pilot_payload(tmp_path)
    before = deepcopy(data)
    path = export_pilot_map(data, tmp_path / "map.html")
    html = path.read_text()
    exported = json.loads(
        re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)[1]
    )
    expected = prepare_comparison_payload(before)
    for key in ("viewpoints", "source_rows", "target_rows", "results", "composite_sources"):
        assert exported[key] == expected[key]
    assert data == before
    assert not re.search(r"<select|<nav|<header", html)
    assert "height:100dvh" in html and "overflow:hidden" in html
    assert exported["map_contract"] == "single_pilot_layer_map_v2"
    assert exported["default_obstruction_metric"] == "ui_obstruction_mean"
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_real_single_map_interactions_and_native_asset_loading(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for map interaction qualification")
    path = export_pilot_map(pilot_payload(tmp_path), tmp_path / "map.html")
    script = Path(__file__).resolve().parents[2] / "scripts/check_pilot_map.js"
    result = subprocess.run(
        [node, str(script), str(path)], capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(result.stdout)["status"] == "passed"
