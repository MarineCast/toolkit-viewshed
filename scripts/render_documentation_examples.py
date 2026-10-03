"""Render local teaching artwork from the checked bundle; no model or acquisition."""

from __future__ import annotations

import html
from pathlib import Path

from build_documentation_examples import check, load_pairs, read


def number(value):
    if value is None:
        return "Unavailable"
    if value == 0:
        return "0 (modeled zero)"
    return f"{value:.3g}" if value < 0.001 else f"{value:.3f}"


def start(title, description, height=420):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 {height}" role="img" aria-label="{html.escape(title)}"><title>{html.escape(title)}</title><desc>{html.escape(description)}</desc><style>text{{font:15px system-ui,sans-serif;fill:#263c47}}.title{{font-size:21px;font-weight:650}}.small{{font-size:12px}}.land{{fill:#f0f1ed;stroke:#455d62;stroke-width:1}}.sea{{fill:#e9f4f8}}.axis{{fill:none;stroke:#647b84;stroke-width:1}}</style><rect width="960" height="{height}" rx="8" fill="#fff"/>'


def text(x, y, value, cls=""):
    return f'<text x="{x}" y="{y}" class="{cls}">{html.escape(str(value))}</text>'


def paths(geometry, project):
    if geometry["type"] == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        return ""
    return " ".join(
        "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in map(project, ring)) + " Z"
        for polygon in polygons
        for ring in polygon
    )


def render(output):
    check(output)
    manifest = read(output / "manifest.json")
    pairs = {p["id"]: p for p in load_pairs(output)}
    lessons = read(output / "lessons.json")
    cells = {f["id"]: f for f in read(output / "cells.geojson")["features"]}
    coast = read(output / "inputs/coast.geojson")["features"]
    observers = read(output / "observer-samples.geojson")["features"]
    profiles = {p["pair_id"]: p for p in read(output / "profiles.json")}
    default = pairs[manifest["defaults"]["pair_id"]]
    ring = cells[default["source_h3"]]["geometry"]["coordinates"][0]
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)

    def project(point):
        return 270 + (point[0] - lon) * 3000, 210 - (point[1] - lat) * 4450

    def map_svg(pair_list, inverse=False, field="distance_adjusted_viewability", samples=False):
        pieces = [
            '<defs><clipPath id="clip-'
            + ident
            + '"><rect x="10" y="15" width="535" height="370" rx="5"/></clipPath></defs><g clip-path="url(#clip-'
            + ident
            + ')"><rect x="10" y="15" width="535" height="370" class="sea"/>'
        ]
        pieces.extend(
            f'<path class="land" fill-rule="evenodd" d="{paths(feature["geometry"],project)}"/>'
            for feature in coast
        )
        for pair in pair_list:
            cell = pair["source_h3" if inverse else "target_h3"]
            value = pair[field]
            color = (
                "#ecf0f1"
                if value == 0
                else (
                    "#8b9ca6"
                    if value is None
                    else f"rgb({int(214-185*value)},{int(237-123*value)},{int(244-100*value)})"
                )
            )
            pieces.append(
                f'<path d="{paths(cells[cell]["geometry"],project)}" fill="{color}" stroke="#647f8b" stroke-width=".6" fill-opacity=".9"/>'
            )
        for cell, label, color in (
            (default["source_h3"], "Observer area A", "#101f28"),
            (default["target_h3"], "Water area B", "#16768b"),
        ):
            pieces.append(
                f'<path d="{paths(cells[cell]["geometry"],project)}" fill="none" stroke="{color}" stroke-width="3"/>'
            )
            coords = cells[cell]["geometry"]["coordinates"][0][:-1]
            x, y = project(
                [sum(p[0] for p in coords) / len(coords), sum(p[1] for p in coords) / len(coords)]
            )
            pieces.append(text(x + 7, y - 8, label, "small"))
        if samples:
            for feature in observers:
                if (
                    feature["properties"]["source_type"] == "land"
                    and feature["properties"]["source_h3"] == default["source_h3"]
                ):
                    x, y = project(feature["geometry"]["coordinates"])
                    pieces.append(
                        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#111" stroke="#fff" stroke-width="1.5"/>'
                    )
        pieces.append("</g>")
        pieces.append(
            text(
                18,
                407,
                "Generalized real coastline · fixed 0\u20131 scale · no external tiles",
                "small",
            )
        )
        return "".join(pieces)

    def profile_svg(profile, x=565, y=90, width=370, height=215):
        values = profile["samples"]
        ceiling = max(max(v["canopy_surface_m"], v["ray_m"]) for v in values) * 1.1 + 2
        distance = values[-1]["distance_m"]
        pieces = [f'<path class="axis" d="M{x},{y} V{y+height} H{x+width}"/>']
        for field, color in (
            ("ground_m", "#5e6157"),
            ("canopy_surface_m", "#168782"),
            ("ray_m", "#253845"),
        ):
            points = " ".join(
                f'{x+v["distance_m"]/distance*width:.2f},{y+height-v[field]/ceiling*height:.2f}'
                for v in values
            )
            pieces.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>'
            )
        pieces.extend(
            [
                text(x, y - 10, f"Height, m (0\u2013{ceiling:.0f})", "small"),
                text(
                    x,
                    y + height + 25,
                    f"Observer → water endpoint · {distance/1000:.2f} km",
                    "small",
                ),
                text(
                    x, y + height + 47, "Gray: ground · teal: ground + trees · dark: ray", "small"
                ),
                text(
                    x,
                    y + height + 67,
                    "One explanatory path; cell values average many paths",
                    "small",
                ),
            ]
        )
        return "".join(pieces)

    preview = output / "previews"
    preview.mkdir(exist_ok=True)
    for lesson in lessons:
        ident = lesson["id"]
        selected = [pairs[key] for key in lesson["pair_ids"]]
        svg = start(
            ident.title() + " · real San Juan example",
            "Actual modeled data, not detection probabilities. Generation "
            + manifest["generation_id"],
        )
        if ident in {"inputs", "samples", "combined", "inverse"}:
            family = [
                p
                for p in pairs.values()
                if p["source_type"] == "land"
                and p["source_h3" if ident != "inverse" else "target_h3"]
                == default["source_h3" if ident != "inverse" else "target_h3"]
            ]
            svg += map_svg(
                family, inverse=ident == "inverse", samples=ident in {"inputs", "samples"}
            )
            if ident == "samples":
                svg += profile_svg(profiles[default["id"]])
            elif ident == "combined":
                svg += text(568, 58, "Selected pair: A → B", "title")
                for i, (label, field) in enumerate(
                    (
                        ("Ground + distance support", "distance_weighted_los_support"),
                        ("Retained after vegetation", "vegetation_attenuation"),
                        ("Combined modeled support", "distance_adjusted_viewability"),
                    )
                ):
                    value = default[field]
                    svg += (
                        text(568, 110 + i * 85, label)
                        + text(568, 136 + i * 85, number(value))
                        + f'<rect x="695" y="{122+i*85}" width="235" height="15" fill="#edf2f4"/><rect x="695" y="{122+i*85}" width="{235*value:.3f}" height="15" fill="#167e94"/>'
                    )
                svg += text(
                    568, 382, "Combined = integrated support \u00d7 retained vegetation", "small"
                )
            elif ident == "inverse":
                count = sum(p["distance_adjusted_viewability"] > 0 for p in family)
                svg += (
                    text(568, 80, "Sources included in this example", "title")
                    + text(568, 129, f"{count} land areas have positive support")
                    + text(568, 175, f"A → B = {number(default['distance_adjusted_viewability'])}")
                    + text(
                        568,
                        208,
                        f"B queried from A = {number(default['distance_adjusted_viewability'])}",
                    )
                    + text(568, 265, "Identical pair record; a different question")
                    + text(568, 300, "These are modeled areas, not people or boats")
                )
            else:
                svg += (
                    text(568, 60, "Mapped inputs, modeled areas", "title")
                    + text(568, 117, "Ground: height above the elevation datum")
                    + text(568, 159, "Trees: height above ground (2020)")
                    + text(568, 201, "Ground + trees: obstruction surface")
                    + text(568, 256, "Dots: actual modeled source samples")
                    + text(568, 303, "Outlines: observer area A and water area B")
                    + text(568, 351, "Sightings are not an input")
                )
        elif ident == "distance":
            curve = read(output / "distance-curve.json")
            svg += text(55, 38, "This model assigns less support as distance increases", "title")
            svg += '<path class="axis" d="M70,70 V330 H920"/>'
            points = " ".join(
                f'{70+v["distance_km"]/5*850:.2f},{330-v["weight"]*260:.2f}' for v in curve
            )
            svg += f'<polyline points="{points}" fill="none" stroke="#168497" stroke-width="3"/>'
            for i, pair in enumerate(selected):
                x, y = (
                    70 + pair["distance_km"] / 5 * 850,
                    330 - pair["distance_detection_weight"] * 260,
                )
                svg += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="#132b37"/>' + text(
                    100 + i * 430,
                    370,
                    f'{"Near" if i==0 else "Far"}: {pair["distance_km"]:.2f} km · diagnostic {number(pair["distance_detection_weight"])}',
                )
            svg += text(70, 60, "Diagnostic support (0\u20131)", "small") + text(
                420, 409, "Centroid distance, km (0\u20135)", "small"
            )
        elif ident == "terrain":
            for i, pair in enumerate(selected):
                x = 35 + i * 475
                svg += text(
                    x,
                    40,
                    ("Open bare-ground support" if i == 0 else "Zero bare-ground support"),
                    "title",
                )
                svg += text(x, 67, "Unweighted bare LOS: " + number(pair["line_of_sight_support"]))
                svg += profile_svg(profiles[pair["id"]], x=x, y=110, width=420, height=190)
        else:
            svg += text(35, 40, "Same pair, matched ground and canopy populations", "title")
            svg += profile_svg(profiles[default["id"]], x=35, y=95, width=475, height=220)
            for i, (label, field) in enumerate(
                (
                    ("Ground only: unweighted LOS", "line_of_sight_support"),
                    ("Ground + trees: unweighted LOS", "physical_viewability"),
                    ("Retained integrated support", "vegetation_attenuation"),
                )
            ):
                svg += text(555, 120 + i * 85, label) + text(
                    555, 148 + i * 85, number(default[field])
                )
            svg += text(
                555, 382, "Missing canopy is zero-height fallback, not treelessness", "small"
            )
        (preview / f"{ident}.svg").write_text(svg + "</svg>\n")
    # Input previews retain null pixels, units and declared display decimation.
    grid = read(output / "inputs/display-grid.json")
    for field, label, maximum in (
        ("ground", "Ground elevation, m", 400),
        ("canopy_height", "Tree height above ground, m", 60),
    ):
        rows = grid[field]
        height = len(rows)
        width = len(rows[0])
        svg = start(
            label, "Real prepared grid, sampled every fourth pixel; pink is missing", height=500
        )
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                fraction = min(1, max(0, value / maximum)) if value is not None else 0
                color = (
                    "#e17cab"
                    if value is None
                    else f"rgb({int(244-205*fraction)},{int(247-114*fraction)},{int(240-81*fraction)})"
                )
                svg += f'<rect x="{35+col*890/width:.2f}" y="{55+row*380/height:.2f}" width="{890/width+.05:.2f}" height="{380/height+.05:.2f}" fill="{color}"/>'
        svg += text(35, 35, label + f" · fixed display range 0\u2013{maximum}", "title") + text(
            35,
            466,
            "100 m modeled grid · 400 m display sampling · pink: unavailable input · no interpolation",
            "small",
        )
        (preview / f"{field}.svg").write_text(svg + "</svg>\n")
    return preview


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/assets/examples/san-juan"))
    print(render(parser.parse_args().output))
