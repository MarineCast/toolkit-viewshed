"""Export geographic display geometry for the regional Leaflet map; never run the model."""

import argparse
import json

import h3
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from build_documentation_examples import digest, encode, read
from render_documentation_walkthrough import color
from render_regional_overview import OUTPUT, evidence


def contents():
    data = evidence()
    features = []
    for row in data["rows"]:
        ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(row["target_h3"])]
        ring.append(ring[0])
        features.append(
            {
                "type": "Feature",
                "id": row["target_h3"],
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    **row,
                    "source_type": "land",
                    "fill": (
                        "#f4d7e6"
                        if row["maximum_valid_combined_weight"] is None
                        else color(row["maximum_valid_combined_weight"])
                    ),
                },
            }
        )
    project = Transformer.from_crs(32610, 4326, always_xy=True)
    coast = shape(read(OUTPUT / "geometry.json")["coast"])
    files = {
        "map-targets.geojson": encode({"type": "FeatureCollection", "features": features}),
        "map-coast.geojson": encode(mapping(transform(project.transform, coast))),
        "map-components.json": (OUTPUT / "map-components.json").read_bytes(),
    }
    files["map-source.json"] = encode(
        {
            "contract": "regional_leaflet_display_v1",
            "crs": "OGC:CRS84",
            "coordinates": "longitude, latitude in degrees; native H3 R7 boundaries",
            "source_type": "land",
            "target_count": len(features),
            "inputs": {
                name: digest((OUTPUT / name).read_bytes())
                for name in ("targets.json", "geometry.json", "source.json")
            },
            "files": {name: digest(value) for name, value in files.items()},
            "versions": {"h3": h3.__version__},
            "interpretation": "Display export only. Original maximum valid combined weights and coverage states are unchanged.",
        }
    )
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, value in contents().items():
        if args.check:
            if (OUTPUT / name).read_bytes() != value:
                raise ValueError(f"Stale geographic display export: {name}")
        else:
            (OUTPUT / name).write_bytes(value)
    print(json.dumps({"geographic_targets": evidence()["target_count"], "check": args.check}))
