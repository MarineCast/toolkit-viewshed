"""Render metric teaching artwork from the checked bundle; no model or acquisition."""

from __future__ import annotations

import html
from pathlib import Path

from build_documentation_examples import check, load_pairs, read
from documentation_geometry import grid_cell_corners, projected_viewport


def number(value):
    if value is None:
        return "Unavailable"
    if value == 0:
        return "0 (modeled zero)"
    return f"{value:.3g}" if value < 0.001 else f"{value:.3f}"


def start(title, description, height=500):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 {height}" role="img" aria-label="{html.escape(title)}"><title>{html.escape(title)}</title><desc>{html.escape(description)}</desc><style>text{{font:16px system-ui,sans-serif;fill:#263c47}}.title{{font-size:19px;font-weight:650}}.land{{fill:#f0f1ed;stroke:#455d62;stroke-width:.7}}.axis{{fill:none;stroke:#647b84;stroke-width:1}}</style><rect width="480" height="{height}" fill="#fff"/>'


def text(x, y, value, cls=""):
    return f'<text x="{x}" y="{y}" class="{cls}">{html.escape(str(value))}</text>'


def paths(geometry, project):
    polygons = (
        [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    )
    return " ".join(
        "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in map(project, ring[:-1])) + " Z"
        for polygon in polygons
        for ring in polygon
    )


def profile_svg(profile, title, offset=0):
    values = profile["samples"]
    ceiling = max(max(v["canopy_surface_m"], v["ray_m"]) for v in values) * 1.1 + 2
    distance = values[-1]["distance_m"]
    x, y, width, height = 45, 65 + offset, 410, 155
    pieces = [
        text(18, offset + 28, title, "title"),
        text(x, y - 12, f"Height, m · 0\u2013{ceiling:.0f}"),
        f'<path class="axis" d="M{x},{y} V{y+height} H{x+width}"/>',
    ]
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
            text(
                x, y + height + 24, f"P source sample → Q target endpoint · {distance/1000:.2f} km"
            ),
            text(x, y + height + 48, "Gray ground · teal trees · dark viewing ray"),
            text(x, y + height + 72, "Vertical exaggeration; axes use physical units"),
        ]
    )
    return "".join(pieces)


def render(output):
    check(output)
    manifest = read(output / "manifest.json")
    pairs = {p["id"]: p for p in load_pairs(output)}
    lessons = read(output / "lessons.json")
    geometry = read(output / "inputs/display-geometry.json")
    grid = read(output / "inputs/display-grid.json")
    cells = {f["id"]: f for f in geometry["cells"]["features"]}
    profiles = {p["pair_id"]: p for p in read(output / "profiles.json")}
    default = pairs[manifest["defaults"]["pair_id"]]
    project, scale = projected_viewport(geometry["bounds"], 480, 420)

    def map_svg(family, pair, inverse=False, samples=False):
        pieces = ['<rect width="480" height="420" fill="#e9f4f8"/>']
        pieces.extend(
            f'<path class="land" fill-rule="evenodd" d="{paths(f["geometry"], project)}"/>'
            for f in geometry["coast"]["features"]
        )
        for p in family:
            cell = p["source_h3" if inverse else "target_h3"]
            value = p["distance_adjusted_viewability"]
            color = (
                "none"
                if samples
                else (
                    "#ecf0f1"
                    if value == 0
                    else f"rgb({int(214-185*value)},{int(237-123*value)},{int(244-100*value)})"
                )
            )
            pieces.append(
                f'<path d="{paths(cells[cell]["geometry"], project)}" fill="{color}" fill-opacity=".8" stroke="#647f8b" stroke-width=".5"/>'
            )
        for cell, label, color in (
            (
                pair["source_h3"],
                "A" if pair["source_h3"] == default["source_h3"] else "S",
                "#101f28",
            ),
            (
                pair["target_h3"],
                "B" if pair["target_h3"] == default["target_h3"] else "T",
                "#16768b",
            ),
        ):
            pieces.append(
                f'<path d="{paths(cells[cell]["geometry"], project)}" fill="none" stroke="{color}" stroke-width="2"/>'
            )
            ring = cells[cell]["geometry"]["coordinates"][0][:-1]
            x, y = project(
                [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
            )
            pieces.append(text(x + 6, y - 6, label, "title"))
        if samples:
            pieces.extend(
                f'<path d="{paths(f["geometry"],project)}" fill="#e9ad56" fill-opacity=".35" stroke="#ac6816"/>'
                for f in geometry["active_sources"]["features"]
                if f["properties"]["source_h3"] == pair["source_h3"]
                and f["properties"]["source_type"] == pair["source_type"]
            )
            for f in geometry["observers"]["features"]:
                if (
                    f["properties"]["source_h3"] == pair["source_h3"]
                    and f["properties"]["source_type"] == pair["source_type"]
                ):
                    x, y = project(f["geometry"]["coordinates"])
                    pieces.append(
                        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#111" stroke="#fff"/>'
                    )
            for f in geometry["target_support"]["features"]:
                if (
                    f["properties"]["target_h3"] == pair["target_h3"]
                    and f["properties"]["source_type"] == pair["source_type"]
                ):
                    x, y = project(f["geometry"]["coordinates"])
                    pieces.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="#126e88"/>')
            profile = profiles.get(pair["id"])
            if profile:
                observer = next(
                    f
                    for f in geometry["observers"]["features"]
                    if f["id"] == profile["observer_id"]
                )
                end = next(p for p in geometry["profiles"] if p["pair_id"] == pair["id"])[
                    "endpoint"
                ]
                a, b = project(observer["geometry"]["coordinates"]), project(end)
                pieces.append(
                    f'<path d="M{a[0]:.2f},{a[1]:.2f} L{b[0]:.2f},{b[1]:.2f}" stroke="#a0471e" stroke-width="2" fill="none"/>'
                )
                for xy, label in ((a, "P"), (b, "Q")):
                    pieces.append(
                        f'<circle cx="{xy[0]:.2f}" cy="{xy[1]:.2f}" r="4" fill="#a0471e"/>'
                        + text(xy[0] + 7, xy[1] + 16, label)
                    )
        pieces.append(
            f'<path d="M25,395 h{5000*scale:.2f}" stroke="#172d37" stroke-width="3"/>'
            + text(25, 380, "5 km · north ↑")
        )
        return "".join(pieces)

    preview = output / "previews"
    preview.mkdir(exist_ok=True)
    for lesson in lessons:
        ident = lesson["id"]
        selected = [pairs[key] for key in lesson["pair_ids"]]
        svg = start(
            ident.title() + " · real San Juan example",
            "Actual modeled data, not detection probabilities. Metric plan maps, north up. Generation "
            + manifest["generation_id"],
            height=740 if ident == "samples" else 610 if ident in {"canopy", "terrain"} else 500,
        )
        if ident in {"inputs", "samples", "combined", "inverse"}:
            family = [
                p
                for p in pairs.values()
                if p["source_type"] == "land"
                and p["target_h3" if ident == "inverse" else "source_h3"]
                == default["target_h3" if ident == "inverse" else "source_h3"]
            ]
            svg += map_svg(
                family, default, inverse=ident == "inverse", samples=ident in {"inputs", "samples"}
            )
            if ident == "samples":
                svg += profile_svg(
                    profiles[default["id"]], "Worked example A → B · same endpoints", offset=430
                )
            else:
                svg += text(18, 450, "Worked example A → B", "title")
                line = (
                    "Inputs: ground, trees, coastline and samples"
                    if ident == "inputs"
                    else (
                        "Combined modeled support: "
                        + number(default["distance_adjusted_viewability"])
                        if ident == "combined"
                        else "Sources included in this example · same pair value"
                    )
                )
                svg += text(18, 480, line)
        elif ident == "distance":
            curve = read(output / "distance-curve.json")
            extent = manifest["curve_contract"]["extent_km"]
            svg += text(18, 32, "Distance diagnostic · an assumed rule", "title")
            svg += '<path class="axis" d="M45,75 V335 H455"/>'
            points = " ".join(
                f'{45+v["distance_km"]/extent*410:.2f},{335-v["weight"]*260:.2f}' for v in curve
            )
            svg += f'<polyline points="{points}" fill="none" stroke="#168497" stroke-width="3"/>'
            for i, p in enumerate(selected):
                svg += f'<circle cx="{45+p["distance_km"]/extent*410:.2f}" cy="{335-p["distance_detection_weight"]*260:.2f}" r="5" fill="#132b37"/>'
                svg += text(
                    18,
                    410 + i * 32,
                    f'{lesson["cases"][i]["title"]}: {p["distance_km"]:.2f} km · {number(p["distance_detection_weight"])}',
                )
            svg += text(45, 60, "Diagnostic support · 0\u20131") + text(
                45, 370, f"Centroid distance · 0\u2013{extent:g} km"
            )
        else:
            for i, p in enumerate(selected):
                svg += profile_svg(profiles[p["id"]], lesson["cases"][i]["title"], offset=i * 305)
        (preview / f"{ident}.svg").write_text(svg + "</svg>\n")
    for field, label, maximum in (
        ("ground", "Ground elevation, m", 400),
        ("canopy_height", "Tree height above ground, m", 60),
    ):
        svg = start(
            label,
            "Nearest decimation, original affine footprint and clipped edge blocks; pink is missing",
            height=540,
        )
        svg += '<g transform="translate(0 50)">'
        for row, values in enumerate(grid[field]):
            for col, value in enumerate(values):
                fraction = min(1, max(0, value / maximum)) if value is not None else 0
                color = (
                    "#e17cab"
                    if value is None
                    else f"rgb({int(244-205*fraction)},{int(247-114*fraction)},{int(240-81*fraction)})"
                )
                coords = " ".join(
                    f"{x:.2f},{y:.2f}"
                    for x, y in map(project, grid_cell_corners(grid, row, col)[:-1])
                )
                svg += f'<polygon data-sample="{row},{col}" points="{coords}" fill="{color}"/>'
        for f in geometry["coast"]["features"]:
            svg += f'<path d="{paths(f["geometry"],project)}" fill="none" stroke="#465e67" stroke-width=".7"/>'
        for cell, label, color in (
            (default["source_h3"], "A", "#101f28"),
            (default["target_h3"], "B", "#16768b"),
        ):
            svg += f'<path d="{paths(cells[cell]["geometry"],project)}" fill="none" stroke="{color}" stroke-width="2"/>'
            ring = cells[cell]["geometry"]["coordinates"][0][:-1]
            x, y = project(
                [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
            )
            svg += text(x - 14 if label == "A" else x + 7, y - 8, label, "title")
        svg += "</g>" + text(18, 30, label + f" · fixed 0\u2013{maximum}", "title")
        svg += text(
            18,
            495,
            f"{grid['analysis_resolution_m']} m model · {grid['display_resolution_m']} m samples",
        ) + text(18, 522, "Pink: missing · no averaging or smoothing")
        svg += f'<path d="M25,445 h{5000*scale:.2f}" stroke="#172d37" stroke-width="3"/>' + text(
            25, 430, "5 km · north ↑"
        )
        (preview / f"{field}.svg").write_text(svg + "</svg>\n")
    return preview


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/assets/examples/san-juan"))
    print(render(parser.parse_args().output))
