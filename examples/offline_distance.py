"""A synthetic, offline first result through the production distance APIs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import polars as pl
import yaml
from shapely.geometry import box

from viewshed_toolkit import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    run_components,
    validate_distance_product,
)
from viewshed_toolkit.resources import default_config_path


def run(output: Path) -> dict[str, object]:
    output = output.resolve()
    # Never clear a directory or mix this illustrative data with a real run.
    output.mkdir(parents=True, exist_ok=False)
    raw = yaml.safe_load(default_config_path().read_text())
    raw.pop("area", None)
    raw["region"] = {
        "name": "synthetic_offline_coast",
        "bbox_wgs84": {"min_lon": -122.78, "min_lat": 48.12, "max_lon": -122.75, "max_lat": 48.14},
    }
    raw["distance_weight"]["hard_cutoff_km"] = 1
    raw["run"]["version"] = "synthetic_offline_v1"
    raw["h3"].update(source_resolution=8, target_resolution=8)
    raw["viewshed"].update(max_distance_m=1000, aoi_margin_m=100)
    raw["source_target_lookup"].update(max_distance_km_land=1, max_distance_km_water=1)
    raw["paths"].update(
        {
            "land_polygon_path": str(output / "land.geojson"),
            "water_polygon_path": str(output / "water.parquet"),
            "land_h3_path": str(output / "work/land.parquet"),
            "source_cells_path": str(output / "work/land.parquet"),
            "output_dir": str(output / "work"),
            "final_output_dir": str(output / "products"),
            "map_dir": str(output / "maps"),
        }
    )
    # Deliberately invented straight coastline; coordinates are illustrative.
    gpd.GeoDataFrame(geometry=[box(-122.81, 48.09, -122.765, 48.17)], crs=4326).to_file(
        output / "land.geojson"
    )
    gpd.GeoDataFrame(geometry=[box(-122.765, 48.09, -122.72, 48.17)], crs=4326).to_parquet(
        output / "water.parquet"
    )
    config = output / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    run_components(config, target="distance", run_id="synthetic-offline")
    pairs = build_pair_distances(config)
    profile = build_distance_profile(
        pairs,
        DistanceProfile(
            "five-km", selected_model="exponential", exponential_lambda_km=5, hard_cutoff_km=1
        ),
    )
    validate_distance_product(pairs)
    validate_distance_product(profile)
    frame = pl.read_parquet(profile)
    if not frame.height:
        raise ValueError("Example must produce at least one source-target pair")
    distance = frame["distance_km"].to_numpy()
    expected = np.where(distance <= 1, np.exp(-distance / 5), 0)
    np.testing.assert_allclose(frame["weight_distance"], expected, rtol=1e-6)
    result = {
        "valid": True,
        "provenance": "synthetic geometry; no observations, DEM, CHM or LOS computation",
        "rows": frame.height,
        "pairs": str(pairs),
        "profile": str(profile),
        "checks": ["production validators", "nonempty pairs", "exponential formula parity"],
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("work/offline-distance"),
        help="New directory; existing directories are never overwritten",
    )
    print(json.dumps(run(parser.parse_args().output), indent=2))
