"""Deterministic static atlas from checked, committed records; never runs the model.

Presentation has a separate identity. Neither model records nor their manifest are rewritten.
All maps use recorded EPSG:32610 geometry and an explicit, shared linear 0-1 scale.
"""

from __future__ import annotations

import argparse
import html
import math
from pathlib import Path

from build_documentation_examples import check as check_bundle
from build_documentation_examples import digest, encode, load_pairs, read
from documentation_geometry import projected_viewport
from render_documentation_examples import paths

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "docs/assets/examples/san-juan"
OUTPUT = ROOT / "docs/assets/examples/walkthrough"
METRIC = "distance_adjusted_viewability"
# Label anchors, WGS84 -> EPSG:32610. Names/relative positions checked against the
# BLM San Juan Islands monument map. Anchors are typography, not new model geometry.
LABELS = (
    (493356.766, 5376325.725, "San Juan Island"),
    (505888.795, 5391886.980, "Orcas Island"),
    (508501.944, 5367435.756, "Lopez Island"),
    (502949.640, 5381880.428, "Shaw Island"),
)
LABEL_SOURCE = "https://www.blm.gov/sites/default/files/orwa-rac-sanjuan-map.pdf"


def target_summary(pairs, target_ids=None, source_role="land"):
    """Maximum of valid combined pair weights, retaining the candidate population.

    Absent pairs are outside the candidate set, never imputed as zero or counted
    as missing candidate records. A partial maximum retains its missing count.
    Roles are never pooled. Validate identity BEFORE filtering or aggregation.
    """
    if source_role not in {"land", "water"}:
        raise ValueError("Unknown source role")
    seen = set()
    for pair in pairs:
        key = (pair["source_type"], pair["source_h3"], pair["target_h3"])
        if any(not part for part in key) or key[0] not in {"land", "water"}:
            raise ValueError("Invalid pair identity")
        if key in seen:
            raise ValueError("Duplicate pair identity")
        seen.add(key)
        if pair.get("scenario", "baseline") != "baseline":
            raise ValueError("Cannot pool scenarios")
        value = pair[METRIC]
        state = pair[METRIC + "_state"]
        if value is not None:
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Invalid combined pair weight")
            if state != ("positive" if value > 0 else "derived_zero"):
                raise ValueError("Combined value/state mismatch")
        elif state in {"positive", "derived_zero"}:
            raise ValueError("Missing weight has a computed state")
    population = sorted({p["source_h3"] for p in pairs if p["source_type"] == source_role})
    targets = sorted(set(target_ids if target_ids is not None else (p["target_h3"] for p in pairs)))
    rows = []
    for target in targets:
        candidates = sorted(
            (p for p in pairs if p["source_type"] == source_role and p["target_h3"] == target),
            key=lambda p: p["source_h3"],
        )
        valid = [p for p in candidates if p[METRIC] is not None]
        maximum = max((p[METRIC] for p in valid), default=None)
        missing = len(candidates) - len(valid)
        rows.append(
            {
                "target_h3": target,
                "maximum_valid_combined_weight": maximum,
                "state": (
                    "outside_candidate_set"
                    if not candidates
                    else "unavailable" if not valid else "partial" if missing else "complete"
                ),
                "candidate_count": len(candidates),
                "valid_count": len(valid),
                "missing_count": missing,
                "outside_candidate_count": len(population) - len(candidates),
                "contributing_sources": [p["source_h3"] for p in valid],
                "strongest_sources": [p["source_h3"] for p in valid if p[METRIC] == maximum],
            }
        )
    return {
        "operator": "maximum_valid_combined_pair_weight",
        "metric": METRIC,
        "source_role": source_role,
        "included_sources": population,
        "coverage_scope": "Included candidate records only; not all possible viewpoints",
        "targets": rows,
    }


def label(x, y, value, cls="", anchor="start"):
    return f'<text x="{x:.2f}" y="{y:.2f}" class="{cls}" text-anchor="{anchor}">{html.escape(str(value))}</text>'


def svg_start(title, width, height):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
<title>{html.escape(title)}</title><desc>Precomputed San Juan Islands model evidence. Physical viewing support, not probability.</desc>
<style>text{{font:20px system-ui,sans-serif;fill:#253f46}}.heading{{font-size:25px;font-weight:650}}.place{{font-size:18px;fill:#53645b;paint-order:stroke;stroke:#f6f4eb;stroke-width:5px;stroke-linejoin:round}}.anchor{{font-size:23px;font-weight:750;paint-order:stroke;stroke:white;stroke-width:5px}}.small{{font-size:17px}}.axis{{stroke:#748b8e;fill:none}}</style>
<defs><pattern id="missing" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#f4d7e6"/><path d="M0 8L8 0" stroke="#a45e85"/></pattern></defs>
<rect width="{width}" height="{height}" fill="#ffffff"/>"""


def color(value):
    if value is None:
        return "url(#missing)"
    if value == 0:
        return "#dce0df"
    # A single linear scale, unchanged between metrics, roles and figures.
    low, high = (224, 244, 237), (8, 102, 91)
    return "#" + "".join(f"{round(a + (b-a)*value):02x}" for a, b in zip(low, high, strict=True))


def center(geometry):
    ring = geometry["coordinates"][0][:-1]
    return [sum(p[i] for p in ring) / len(ring) for i in (0, 1)]


def map_svg(data, title, weights=None, samples=False, regional=False, mobile=False):
    geometry, default = data["geometry"], data["default"]
    cells = {f["id"]: f for f in geometry["cells"]["features"]}
    width, map_height = (480, 440) if mobile else (960, 580)
    height = map_height + ((190 if mobile else 155) if weights is not None else 80)
    bounds = data["regional_bounds"] if regional else data["local_bounds"]
    project, scale = projected_viewport(bounds, width, map_height - 50, padding=28)
    out = [
        svg_start(title, width, height),
        '<g transform="translate(0 50)">',
        f'<rect width="{width}" height="{map_height-50}" fill="#eaf2f3"/>',
        f'<clipPath id="mapclip"><rect width="{width}" height="{map_height-50}"/></clipPath>',
        '<g clip-path="url(#mapclip)">',
    ]
    out.extend(
        f'<path d="{paths(f["geometry"],project)}" fill="#f6f4eb" stroke="#a5b1a9" stroke-width="1" fill-rule="evenodd"/>'
        for f in geometry["coast"]["features"]
    )
    for cell in data["target_ids"]:
        value = weights.get(cell) if weights is not None else None
        included = weights is not None and cell in weights
        out.append(
            f'<path data-target="{cell}" data-state="{"candidate" if included else "outside"}" d="{paths(cells[cell]["geometry"],project)}" fill="{color(value) if included else "none"}" stroke="{"#6a8d86" if included else "#cbd7d7"}" stroke-width="{.8 if included else .4}"/>'
        )
    if regional:
        out.extend(
            f'<path d="{paths(f["geometry"],project)}" fill="#bb913e" fill-opacity=".18" stroke="none"/>'
            for f in geometry["active_sources"]["features"]
            if f["properties"]["source_type"] == "land"
        )
    if samples:
        out.extend(
            f'<path d="{paths(f["geometry"],project)}" fill="#e1ad52" fill-opacity=".5"/>'
            for f in geometry["active_sources"]["features"]
            if f["id"] == "land:" + default["source_h3"]
        )
        for collection, key in (("observers", "source_h3"), ("target_support", "target_h3")):
            for f in geometry[collection]["features"]:
                if (
                    f["properties"][key] == default[key]
                    and f["properties"]["source_type"] == "land"
                ):
                    x, y = project(f["geometry"]["coordinates"])
                    out.append(
                        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{3.5 if collection=="observers" else 0.6}" fill="{"#704219" if collection=="observers" else "#0e746b"}"/>'
                    )
        profile = data["profile"]
        observer = next(
            f for f in geometry["observers"]["features"] if f["id"] == profile["observer_id"]
        )
        endpoint = next(p for p in geometry["profiles"] if p["pair_id"] == default["id"])[
            "endpoint"
        ]
        a, b = project(observer["geometry"]["coordinates"]), project(endpoint)
        out.append(
            f'<path d="M{a[0]:.2f},{a[1]:.2f}L{b[0]:.2f},{b[1]:.2f}" stroke="#bd562d" stroke-width="3"/>'
        )
        for xy, t in ((a, "P"), (b, "Q")):
            out.append(f'<circle cx="{xy[0]:.2f}" cy="{xy[1]:.2f}" r="4" fill="#bd562d"/>')
            dx, dy = (-34, 40) if t == "P" else (40, 22)
            out.append(
                f'<path d="M{xy[0]:.2f},{xy[1]:.2f} l{dx},{dy-8}" stroke="#748b8e" stroke-width="1"/>'
            )
            out.append(label(xy[0] + dx, xy[1] + dy, t, "anchor", "middle"))
    for x, y, name in LABELS:
        px, py = project((x, y))
        if name == "San Juan Island":
            if regional:
                py += 34 if mobile else 40
                px -= 20 if mobile else 30
            elif mobile:
                py += 22
                px -= 12
        if 40 < px < width - 60 and 25 < py < map_height - 80:
            out.append(label(px, py, name, "place", "middle"))
    for cell, t, offset in ((default["source_h3"], "A", -15), (default["target_h3"], "B", 16)):
        out.append(
            f'<path d="{paths(cells[cell]["geometry"],project)}" fill="none" stroke="{"#965b27" if t=="A" else "#214858"}" stroke-width="3"/>'
        )
        px, py = project(center(cells[cell]["geometry"]))
        out.append(label(px + offset, py - 14, t, "anchor", "middle"))
    out.append("</g>")
    # Projected metric distance, equal scales on both axes. Context never expands candidates.
    km = 2 if not regional else 5
    out.extend(
        [
            f'<rect x="18" y="{map_height-93}" width="{km*1000*scale+40:.2f}" height="58" fill="white" fill-opacity=".9"/>',
            f'<path d="M30,{map_height-58} h{km*1000*scale:.2f}" stroke="#253f46" stroke-width="3"/>',
            label(30, map_height - 68, f"{km} km", "small"),
            label(width - 30, 32, "N ↑", "heading", "end"),
            "</g>",
            label(
                24,
                32,
                (
                    {
                        "One source area → one water target": "One pair · A → B",
                        "Combined support · distance included": "Combined · distance included",
                        "Strongest support from included land sources": "Strongest included land support",
                    }.get(title, title)
                    if mobile
                    else title
                ),
                "heading",
            ),
        ]
    )
    y = map_height + 32
    if weights is not None:
        bar_width = width - 48
        out.extend(
            f'<rect x="{24+i*bar_width/100:.2f}" y="{y}" width="{bar_width/100+0.1:.2f}" height="12" fill="{color(i/99)}"/>'
            for i in range(100)
        )
        out.extend(
            label(
                24 + v * bar_width,
                y + 35,
                f"{v:g}",
                "small",
                "middle" if v not in (0, 1) else "start" if v == 0 else "end",
            )
            for v in (0, 0.25, 0.5, 0.75, 1)
        )
        out.append(label(24, y + 64, "Fixed linear support scale · 0\u20131", "small"))
        for i, (fill, t) in enumerate(
            (
                ("#dce0df", "Computed zero"),
                ("url(#missing)", "Unavailable / partial"),
                ("none", "Outside candidate set"),
            )
        ):
            lx = 24 if mobile else 24 + i * 305
            ly = y + 94 + i * 26 if mobile else y + 100
            out.append(
                f'<rect x="{lx}" y="{ly-14}" width="16" height="16" fill="{fill}" stroke="#879a9b"/>'
            )
            out.append(label(lx + 25, ly, t, "small"))
    else:
        out.append(label(24, y, "A source area · B water target", "small"))
        out.append(label(24, y + 28, "Dots: recorded samples · orange: one P → Q path", "small"))
    return "".join(out) + "</svg>\n"


def profile_svg(profile, mobile=False):
    width, height = (480, 350) if mobile else (960, 350)
    x, y, w, h = 65, 65, width - 90, 190
    values = profile["samples"]
    distance = values[-1]["distance_m"]
    low = min(0, min(v["ground_m"] for v in values))
    high = max(max(v["canopy_surface_m"], v["ray_m"]) for v in values) * 1.12

    def point(v, k):
        return (x + v["distance_m"] / distance * w, y + h - (v[k] - low) / (high - low) * h)

    out = [
        svg_start("The same P → Q path", width, height),
        label(24, 32, "The same P → Q path", "heading"),
    ]
    for i in range(4):
        value = low + (high - low) * i / 3
        py = y + h - i * h / 3
        out.append(f'<path d="M{x},{py:.2f}h{w}" stroke="#e4eaea"/>')
        out.append(label(x - 10, py + 6, f"{value:.0f}", "small", "end"))
    for key, col in (("canopy_surface_m", "#95bfaa"), ("ground_m", "#aaa491")):
        pts = " ".join(f"{px:.2f},{py:.2f}" for px, py in (point(v, key) for v in values))
        out.append(f'<polygon points="{x},{y+h} {pts} {x+w},{y+h}" fill="{col}"/>')
    pts = " ".join(f"{px:.2f},{py:.2f}" for px, py in (point(v, "ray_m") for v in values))
    out.append(f'<polyline points="{pts}" fill="none" stroke="#bd562d" stroke-width="3"/>')
    out.append(label(x, y - 14, "Elevation (m)", "small"))
    for i in range(3 if mobile else 5):
        v = i / (2 if mobile else 4)
        out.append(label(x + v * w, y + h + 25, f"{v*distance/1000:.2f}", "small", "middle"))
    out.append(label(x + w / 2, y + h + 50, "Distance along path (km)", "small", "middle"))
    exaggeration = (h / (high - low)) / (w / distance)
    out.append(
        label(
            24,
            height - 16,
            f"Vertical exaggeration {exaggeration:.1f}× · curved ray",  # noqa: RUF001 -- multiplication symbol
            "small",
        )
    )
    return "".join(out) + "</svg>\n"


def contents(bundle):
    check_bundle(bundle)
    manifest = read(bundle / "manifest.json")
    pairs = load_pairs(bundle)
    default = next(p for p in pairs if p["id"] == manifest["defaults"]["pair_id"])
    geometry = read(bundle / "inputs/display-geometry.json")
    summary = target_summary(pairs)
    summary.update(
        {
            "generation_id": manifest["generation_id"],
            "scientific_bundle_id": manifest["bundle_id"],
            "pair_records_sha256": digest((bundle / "pairs.json").read_bytes()),
            "product_status": "example-derived summary; not a supported scientific product",
        }
    )
    family = [
        p for p in pairs if p["source_type"] == "land" and p["source_h3"] == default["source_h3"]
    ]
    cells = {f["id"]: f for f in geometry["cells"]["features"]}
    rings = [cells[p["target_h3"]]["geometry"]["coordinates"][0] for p in family] + [
        cells[default["source_h3"]]["geometry"]["coordinates"][0]
    ]
    coords = [p for ring in rings for p in ring]
    bounds = [
        min(p[0] for p in coords) - 3500,
        min(p[1] for p in coords) - 3500,
        max(p[0] for p in coords) + 3500,
        max(p[1] for p in coords) + 3500,
    ]
    # Candidate H3 cells extend beyond the prepared-raster footprint. Keep every
    # included cell corner visible; wider context never adds modeled records.
    all_corners = [
        point for feature in cells.values() for point in feature["geometry"]["coordinates"][0]
    ]
    regional_bounds = [
        min(p[0] for p in all_corners) - 1200,
        min(p[1] for p in all_corners) - 1200,
        max(p[0] for p in all_corners) + 1200,
        max(p[1] for p in all_corners) + 1200,
    ]
    data = {
        "geometry": geometry,
        "default": default,
        "local_bounds": bounds,
        "regional_bounds": regional_bounds,
        "target_ids": sorted({p["target_h3"] for p in pairs}),
        "profile": next(p for p in read(bundle / "profiles.json") if p["pair_id"] == default["id"]),
    }
    files = {"target-summary.json": encode(summary)}
    for mobile in (False, True):
        suffix = "-mobile" if mobile else ""
        files[f"pair-map{suffix}.svg"] = map_svg(
            data, "One source area → one water target", samples=True, mobile=mobile
        ).encode()
        files[f"profile{suffix}.svg"] = profile_svg(data["profile"], mobile).encode()
        for field, name, title in (
            ("line_of_sight_support", "ground", "Ground-only support"),
            ("physical_viewability", "canopy", "Ground + trees support"),
            (METRIC, "combined", "Combined support · distance included"),
        ):
            files[f"{name}{suffix}.svg"] = map_svg(
                data, title, {p["target_h3"]: p[field] for p in family}, mobile=mobile
            ).encode()
        weights = {
            r["target_h3"]: r["maximum_valid_combined_weight"]
            for r in summary["targets"]
            if r["candidate_count"]
        }
        files[f"target-summary{suffix}.svg"] = map_svg(
            data,
            "Strongest support from included land sources",
            weights,
            regional=True,
            mobile=mobile,
        ).encode()
    # Theme variants change presentation only; values and geometry are identical.
    palette = {
        "#ffffff": "#182b33",
        "#253f46": "#e4eeec",
        "#eaf2f3": "#213b46",
        "#f6f4eb": "#374b48",
        "#a5b1a9": "#728a80",
        "#53645b": "#c0cebc",
        "stroke:white": "stroke:#182b33",
        'fill="white"': 'fill="#182b33"',
        "#dce0df": "#73817e",
        "#e4eaea": "#3b5058",
        "#aaa491": "#8b8b7e",
        "#95bfaa": "#699981",
        "#bd562d": "#ffac7e",
    }
    for name, value in list(files.items()):
        if name.endswith(".svg"):
            dark = value.decode()
            for old, new in palette.items():
                dark = dark.replace(old, new)
            files[name.replace(".svg", "-dark.svg")] = dark.encode()
    identity = {
        "presentation_contract": "san_juan_walkthrough_v1",
        "scientific_bundle_id": manifest["bundle_id"],
        "generation_id": manifest["generation_id"],
        "pair_id": default["id"],
        "operator": summary["operator"],
        "source_role": "land",
        "local_extent_epsg32610": bounds,
        "regional_extent_epsg32610": regional_bounds,
        "color_scale": {"range": [0, 1], "transform": "linear"},
        "geographic_labels": {"reference": LABEL_SOURCE, "anchors_epsg32610": LABELS},
        "files": {name: digest(value) for name, value in files.items()},
    }
    identity["presentation_id"] = digest(encode(identity))
    files["manifest.json"] = encode(identity)
    return files


def check(bundle=BUNDLE, output=OUTPUT):
    expected = contents(bundle)
    if {p.name for p in output.iterdir() if p.is_file()} != set(expected):
        raise ValueError("Walkthrough presentation inventory mismatch")
    for name, value in expected.items():
        if (output / name).read_bytes() != value:
            raise ValueError(f"Walkthrough presentation differs from canonical records: {name}")
    return {
        "presentation_id": read(output / "manifest.json")["presentation_id"],
        "static_figure_variants": 24,
        "scientific_bundle_unchanged": True,
    }


def render(bundle=BUNDLE, output=OUTPUT):
    files = contents(bundle)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (output / name).write_bytes(value)
    return check(bundle, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(check(args.bundle, args.output) if args.check else render(args.bundle, args.output))
