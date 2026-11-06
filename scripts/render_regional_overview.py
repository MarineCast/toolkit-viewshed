"""Render the full land-source regional aggregate from a sealed, offline excerpt."""

import argparse
import json
from pathlib import Path

from build_documentation_examples import digest, encode, read
from documentation_geometry import projected_viewport
from render_documentation_examples import paths
from render_documentation_walkthrough import color, label, svg_start
from render_source_examples import DARK

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/examples/region-overview"


def evidence():
    receipt = read(OUTPUT / "source.json")
    for name, sha in receipt["payload_sha256"].items():
        if digest((OUTPUT / name).read_bytes()) != sha:
            raise ValueError("Regional aggregate checksum mismatch")
    data = read(OUTPUT / "targets.json")
    if data["source_type"] != "land" or data["target_count"] != len(data["rows"]):
        raise ValueError("Regional aggregate role/count mismatch")
    if sum(r["candidate_count"] for r in data["rows"]) != data["candidate_pair_count"]:
        raise ValueError("Incomplete aggregate candidates")
    return data


def figure(mobile=False):
    data = evidence()
    g = read(OUTPUT / "geometry.json")
    rows = {r["target_h3"]: r for r in data["rows"]}
    width, height = (480, 850) if mobile else (960, 1100)
    project, scale = projected_viewport(g["bounds"], width, height - 180, padding=35)
    out = [
        svg_start(
            "Regional land viewability · strongest source per water target", width, height
        ).replace(
            "Precomputed San Juan Islands model evidence.",
            "Precomputed Vancouver Island–Columbia regional model evidence.",
        ),
        label(24, 34, "Regional land viewing support", "heading"),
        '<g transform="translate(0 60)">',
        f'<rect width="{width}" height="{height-180}" fill="#eaf2f3"/>',
        f'<clipPath id="region-clip"><rect width="{width}" height="{height-180}"/></clipPath><g clip-path="url(#region-clip)"><g data-map-layer="">',
        f'<path d="{paths(g["coast"],project)}" fill="#f6f4eb" stroke="#a5b1a9" stroke-width=".6" fill-rule="evenodd"/>',
    ]
    for f in g["cells"]:
        r = rows[f["id"]]
        value = r["maximum_valid_combined_weight"]
        fill = "#f4d7e6" if value is None else color(value)
        out.append(
            f'<path data-target="{f["id"]}" data-value="{value}" data-state="{r["state"]}" d="{paths(f["geometry"],project)}" fill="{fill}"><title>{f["id"]}: {value}; {r["valid_count"]}/{r["candidate_count"]} valid land candidates</title></path>'
        )
    # Approximate geographic label anchors in EPSG:32610; typography, not model inputs.
    for xy, text in (
        ((337000, 5520000), "Vancouver Island"),
        ((620000, 5410000), "British Columbia"),
        ((590000, 5230000), "Washington"),
        ((465000, 5120000), "Columbia River"),
    ):
        x, y = project(xy)
        if text in {"British Columbia", "Washington"}:
            out.append(
                label(
                    width - 18,
                    y,
                    "B.C." if mobile and text == "British Columbia" else text,
                    "place",
                    "end",
                )
            )
        else:
            out.append(label(x, y, text, "place", "middle"))
    out.extend(
        [
            "</g></g>",
            label(width - 20, 28, "N ↑", "small", "end"),
            f'<path d="M25,{height-220}h{100000*scale:.2f}" stroke="#253f46" stroke-width="3"/>',
            label(25, height - 235, "100 km", "small").replace(
                "<text ", '<text data-map-scale="" '
            ),
            "</g>",
        ]
    )
    y = height - 90
    for i in range(100):
        out.append(
            f'<rect x="{24+i*(width-48)/100:.2f}" y="{y}" width="{(width-48)/100+0.1:.2f}" height="15" fill="{color(i/99)}"/>'
        )
    out.extend(
        [
            label(24, y - 10, "Maximum valid combined weight", "small"),
            label(24, y + 37, "0", "small"),
            label(width - 24, y + 37, "1", "small", "end"),
            label(24, height - 14, "Gray = computed zero · blank = no target", "small"),
            "</svg>\n",
        ]
    )
    return "".join(out)


def contents():
    files = {}
    for mobile in (False, True):
        suffix = "-mobile" if mobile else ""
        value = figure(mobile)
        files["combined" + suffix + ".svg"] = value.encode()
        for before, after in DARK.items():
            value = value.replace(before, after)
        files["combined" + suffix + "-dark.svg"] = value.encode()
    files["manifest.json"] = encode(
        {
            "source_sha256": digest((OUTPUT / "source.json").read_bytes()),
            "files": {k: digest(v) for k, v in files.items()},
        }
    )
    return files


def check():
    for name, value in contents().items():
        if (OUTPUT / name).read_bytes() != value:
            raise ValueError(f"Stale regional overview: {name}")
    receipt = read(OUTPUT / "map-source.json")
    for name, sha in {**receipt["inputs"], **receipt["files"]}.items():
        if digest((OUTPUT / name).read_bytes()) != sha:
            raise ValueError(f"Stale regional HTML map: {name}")
    features = read(OUTPUT / "map-targets.geojson")["features"]
    rows = {r["target_h3"]: r for r in evidence()["rows"]}
    if len(features) != len(rows) or {f["id"] for f in features} != rows.keys():
        raise ValueError("Regional HTML map target population mismatch")
    for feature in features:
        properties = feature["properties"]
        if properties["source_type"] != "land" or any(
            properties[key] != value for key, value in rows[feature["id"]].items()
        ):
            raise ValueError("Regional HTML map changed original values")
    return {"regional_targets": evidence()["target_count"], "maps": 4}


def values():
    data = evidence()
    pictures = []
    for theme, suffix in [("light", ""), ("dark", "-dark")]:
        base = "../assets/examples/region-overview/combined"
        pictures.append(
            f'<picture class="figure-{theme}"><source media="(max-width:600px)" srcset="{base}-mobile{suffix}.svg" width="480" height="850"><img src="{base}{suffix}.svg" width="960" height="1100" alt="All {data["target_count"]:,} delivered water targets, colored by maximum valid combined land-source weight" loading="lazy"></picture>'
        )
    return {
        "region_map": "".join(pictures),
        "region_sources": f'{data["source_count"]:,}',
        "region_pairs": f'{data["candidate_pair_count"]:,}',
        "region_targets": f'{data["target_count"]:,}',
        "region_positive": f'{sum(r["maximum_valid_combined_weight"] is not None and r["maximum_valid_combined_weight"]>0 for r in data["rows"]):,}',
        "region_missing": str(sum(r["missing_count"] for r in data["rows"])),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        for name, value in contents().items():
            (OUTPUT / name).write_bytes(value)
    print(json.dumps(check()))
