from __future__ import annotations

from pathlib import Path

import yaml

from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.config.paths import DEFAULT_CONFIG
from viewshed_toolkit.resources import default_config_path

CANONICAL_CONFIG = Path("configs/salish_sea.yaml")


def test_checkout_config_matches_packaged_default() -> None:
    packaged = default_config_path()

    assert sorted(Path("configs").glob("*.yaml")) == [
        CANONICAL_CONFIG,
        Path("configs/salish_sea_case_study.yaml"),
    ]
    assert DEFAULT_CONFIG == packaged
    assert yaml.safe_load(CANONICAL_CONFIG.read_text()) == yaml.safe_load(packaged.read_text())


def test_canonical_config_uses_model_area_bbox() -> None:
    app = load_app_config(CANONICAL_CONFIG)

    assert app.region.name == "model_area"
    assert app.region.bbox_wgs84 == {
        "min_lon": -125.8,
        "min_lat": 46.85,
        "max_lon": -121.6,
        "max_lat": 50.0,
    }
    assert app.h3.source_resolution == 7
    assert app.h3.target_resolution == 7
    static_maps = app.raw_config["static_maps"]
    assert static_maps["enabled"] is True
    assert static_maps["selected_location"] == {
        "latitude": 48.135238,
        "longitude": -122.767230,
    }
    assert app.run.write_maps is False
    assert app.paths.map_dir == Path("outputs/effort/viewshed").resolve()
    assert app.paths.output_dir == Path("data/tmp/viewshed/h3r7").resolve()
    assert app.paths.final_output_dir == Path("data/processed/domain/human/viewshed/RES7").resolve()
