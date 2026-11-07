"""Render Lime Kiln from a checked regional excerpt, without running or changing science."""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path

from build_documentation_examples import digest, encode, read
from documentation_geometry import projected_viewport
from render_documentation_examples import paths
from render_documentation_walkthrough import color, label, svg_start

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/examples/lime-kiln"
SOURCE = "8728d1061ffffff"
# Commons geotag for the lighthouse, transformed with PROJ to EPSG:32610.
PLACE = (488738.0458747733, 5373643.06484949)
PLACE_URL = "https://commons.wikimedia.org/wiki/File:Lime_Kiln_Light_-_US-WA.jpg"
METRICS = {
    "distance": ("distance_detection_weight", "Cell-center distance weight"),
    "terrain": ("distance_weighted_los_support", "Terrain weight · distance included"),
    "vegetation": ("vegetation_attenuation", "Vegetation · conditional factor"),
    "combined": ("distance_adjusted_viewability", "Combined physical support"),
}
PHOTO = "lime-kiln-dave-sizer.jpg"
PHOTO_SHA256 = "551f0620b4b592e48c92f7857c501f62dccb9c4c3d274163bf1636d332108bea"


def regional_evidence(output=None):
    output = OUTPUT if output is None else output
    evidence = read(output / "regional-source.json")
    if evidence["contract"] not in {"lime_kiln_regional_excerpt_v1", "regional_source_excerpt_v1"}:
        raise ValueError("Expected regional Lime Kiln evidence")
    settings = evidence["physical_assumptions"]
    curve = settings["distance_weight"]
    if (
        settings["dem_resolution_m"],
        settings["observer_canopy_clearance_radius_m"],
        curve["selected_model"],
        curve["logistic_d50_km"],
        curve["logistic_slope_km"],
        curve["hard_cutoff_km"],
        curve["normalize_at_zero"],
    ) != (30, 30, "logistic", 9, 2.7, 30, True):
        raise ValueError(
            "Regional settings changed: update the authored explanation before rendering"
        )
    for name, checksum in evidence["payload_sha256"].items():
        if digest((output / name).read_bytes()) != checksum:
            raise ValueError(f"Regional excerpt checksum mismatch: {name}")
    return evidence


def distance_weight(distance, curve):
    if distance > curve["hard_cutoff_km"]:
        return 0.0
    raw = 1 / (1 + math.exp((distance - curve["logistic_d50_km"]) / curve["logistic_slope_km"]))
    if curve["normalize_at_zero"]:
        raw *= 1 + math.exp(-curve["logistic_d50_km"] / curve["logistic_slope_km"])
    return min(1.0, max(0.0, raw))


def no_baseline(pair):
    # Derived display classification; retain the producer's original vegetation_state.
    return pair["distance_weighted_los_support"] == 0 and pair["vegetation_attenuation"] == 1


def records(output=None, source=SOURCE):
    output = OUTPUT if output is None else output
    evidence = regional_evidence(output)
    excerpt = read(output / "source-pairs.json")
    family = excerpt["rows"]
    if excerpt["generation_id"] != evidence["generation_id"]:
        raise ValueError("Regional generation mismatch")
    if len(family) != evidence["pair_count"] or len({p["target_h3"] for p in family}) != len(
        family
    ):
        raise ValueError("Missing or duplicate Lime Kiln source population")
    curve = evidence["physical_assumptions"]["distance_weight"]
    for p in family:
        if p["source_type"] != "land" or p["source_h3"] != source:
            raise ValueError("Unexpected source role or cell")
        for field, _ in METRICS.values():
            if p[field] is None or not math.isfinite(p[field]) or not 0 <= p[field] <= 1:
                raise ValueError("Invalid regional weight")
        if abs(p["distance_detection_weight"] - distance_weight(p["distance_km"], curve)) > 1e-6:
            raise ValueError("Distance curve mismatch")
        if (
            abs(
                p["distance_adjusted_viewability"]
                - p["distance_weighted_los_support"] * p["vegetation_attenuation"]
            )
            > 1e-7
        ):
            raise ValueError("Combined weight mismatch")
    return sorted(family, key=lambda p: p["target_h3"])


def map_figure(
    family,
    name,
    mobile=False,
    *,
    geometry=None,
    source=SOURCE,
    place=PLACE,
    place_name="Lime Kiln",
    labels=None,
):
    g = geometry if geometry is not None else read(OUTPUT / "geometry.json")
    cells = {f["id"]: f for f in g["cells"]["features"]}
    target_ids = {p["target_h3"] for p in family}
    points = [
        xy for key in target_ids | {source} for xy in cells[key]["geometry"]["coordinates"][0]
    ]
    bounds = [
        min(p[0] for p in points) - 1800,
        min(p[1] for p in points) - 1800,
        max(p[0] for p in points) + 1800,
        max(p[1] for p in points) + 1800,
    ]
    width, map_h = (480, 460) if mobile else (960, 580)
    height = map_h + (100 if name == "study" else 175)
    project, scale = projected_viewport(bounds, width, map_h - 60, padding=24)
    title = f"{place_name} · study area" if name == "study" else METRICS[name][1]
    out = [
        svg_start(title, width, height),
        '<defs><pattern id="neutral" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#eee7d7"/><path d="M0 8L8 0" stroke="#a29982"/></pattern></defs>',
        label(24, 34, title, "heading"),
        '<g transform="translate(0 60)">',
        f'<rect width="{width}" height="{map_h-60}" fill="#eaf2f3"/>',
        f'<clipPath id="clip"><rect width="{width}" height="{map_h-60}"/></clipPath><g clip-path="url(#clip)">',
    ]
    for f in g["coast"]["features"]:
        out.append(
            f'<path d="{paths(f["geometry"], project)}" fill="#f6f4eb" stroke="#a5b1a9" stroke-width="1" fill-rule="evenodd"/>'
        )
    for p in family:
        value = None if name == "study" else p[METRICS[name][0]]
        neutral = name == "vegetation" and no_baseline(p)
        fill = "url(#neutral)" if neutral else "#d3e5e5" if name == "study" else color(value)
        out.append(
            f'<path data-target="{p["target_h3"]}" data-value="{value}" data-state="{p["vegetation_state"] if name == "vegetation" else "candidate"}" data-display-state="{"no_baseline_support" if neutral else "value"}" d="{paths(cells[p["target_h3"]]["geometry"], project)}" fill="{fill}" stroke="#6a8d86" stroke-width=".5"/>'
        )
    if name == "distance":
        cx, cy = project(g["source_centroid"])
        for km in (10, 20, 30):
            out.append(
                f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{km*1000*scale:.2f}" fill="none" stroke="#567279" stroke-width="1" stroke-dasharray="4 5"/>'
            )
            out.append(
                label(
                    cx - km * 1000 * scale * 0.28,
                    cy + km * 1000 * scale * 0.96,
                    f"{km} km",
                    "place",
                    "middle",
                )
            )
        out.append(
            f'<path d="M{cx-5:.2f},{cy:.2f}h10 M{cx:.2f},{cy-5:.2f}v10" stroke="#253f46" stroke-width="2"/>'
        )
        out.append(f'<path d="M{cx+5:.2f},{cy+3:.2f}l48,-5" stroke="#253f46"/>')
        out.append(label(cx + 60, cy + 2, "Cell center", "place"))
    for f in g["active_sources"]["features"]:
        if f["id"] == "land:" + source and name == "study":
            out.append(
                f'<path d="{paths(f["geometry"], project)}" fill="#e1ad52" fill-opacity=".6"/>'
            )
    out.append(
        f'<path d="{paths(cells[source]["geometry"], project)}" fill="none" stroke="#965b27" stroke-width="3"/>'
    )
    for f in g["observers"]["features"]:
        if f["properties"]["source_h3"] == source and f["properties"]["source_type"] == "land":
            x, y = project(f["geometry"]["coordinates"])
            out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#704219" stroke="white"/>')
    x, y = project(place)
    out.append(
        f'<path d="M{x:.2f},{y-8:.2f} l7,8 -7,8 -7,-8 Z" fill="#bd562d" stroke="white" stroke-width="1.5"/>'
    )
    out.append(f'<path d="M{x-8:.2f},{y:.2f} h-25" stroke="#bd562d"/>')
    out.append(label(x - 38, y + 5, place_name, "anchor", "end"))
    for px, py, name_text in (
        labels
        if labels is not None
        else (
            (497000, 5383000, "San Juan Island"),
            (470000, 5365000, "Haro Strait"),
            (505900, 5391887, "Orcas Island"),
            (509000, 5364500, "Lopez Island"),
            (463000, 5384000, "Vancouver Island"),
        )
    ):
        x, y = project((px, py))
        out.append(label(x, y, name_text, "place", "middle"))
    out.extend(
        [
            "</g>",
            label(width - 25, 30, "N ↑", "heading", "end"),
            f'<rect x="18" y="{map_h-120}" width="{10000*scale+30:.2f}" height="50" fill="white" fill-opacity=".9"/>',
            f'<path d="M30,{map_h-80} h{10000*scale:.2f}" stroke="#253f46" stroke-width="3"/>',
            label(30, map_h - 93, "10 km", "small"),
            "</g>",
        ]
    )
    y = map_h + 28
    if name != "study":
        for i in range(100):
            out.append(
                f'<rect x="{24+i*(width-48)/100:.2f}" y="{y}" width="{(width-48)/100+.2:.2f}" height="13" fill="{color((i+.5)/100)}"/>'
            )
        out.extend(
            [
                label(24, y + 36, "0", "small"),
                label(width / 2, y + 36, "0.5", "small", "middle"),
                label(width - 24, y + 36, "1", "small", "end"),
            ]
        )
        text = (
            "Hatched: neutral 1; no baseline support"
            if name == "vegetation"
            else "Gray: computed zero · pink: unavailable"
        )
        out.append(label(24, y + 66, text, "small"))
        y += 76
    out.append(label(24, y + 20, "Orange outline: source · dots: modeled observers", "small"))
    out.append(label(24, y + 45, "Diamond: lighthouse · unfilled: outside candidates", "small"))
    return "".join(out) + "</svg>\n"


def distance_figure(curve):
    points = " ".join(
        f"{55+d/35*390:.2f},{340-distance_weight(d,curve)*145:.2f}"
        for d in [i / 10 for i in range(301)]
    )
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 435" role="img" aria-labelledby="title desc"><title id="title">Regional distance attenuation over unobstructed water</title><desc id="desc">Schematic observer above a plotted normalized logistic curve: midpoint parameter 9 km, slope 2.7 km, hard cutoff beyond 30 km. This assumption is separate from the geometric horizon.</desc><rect width="480" height="435" rx="12" fill="#f3f6f1"/><style>text{font:18px system-ui,sans-serif;fill:#253f46}.small{font-size:16px}.title{font-size:22px;font-weight:650}</style><text x="24" y="34" class="title">03 / A clear view can still be distant</text><path d="M25 129H110V155H25Z" fill="#c5c0ad"/><path d="M110 129H455V155H110Z" fill="#b4d9df"/><g transform="translate(61 129)" fill="#253f46" stroke="#253f46" stroke-width="4" stroke-linecap="round"><circle cy="-33" r="6"/><path d="M0-26v15m0-12 11-7m-11 19-7 11m7-11 7 11" fill="none"/></g>'
    ]
    for x, opacity in ((170, 1), (300, 0.55), (430, 0.2)):
        out.append(
            f'<path d="M66 96L{x} 124" stroke="#087c6c" stroke-width="2" opacity="{opacity}"/><circle cx="{x}" cy="124" r="5" fill="#087c6c" opacity="{opacity}"/>'
        )
    out.append(
        '<text x="24" y="181" class="small">Assumed distance weight · regional settings</text>'
    )
    for y, t in ((195, "1"), (267.5, "0.5"), (340, "0")):
        out.append(
            f'<path d="M55 {y}H445" stroke="#d4ddd6"/><text x="43" y="{y+5}" text-anchor="end" class="small">{t}</text>'
        )
    cutoff_x = 55 + 30 / 35 * 390
    out.append(
        f'<polyline points="{points}" fill="none" stroke="#087c6c" stroke-width="3"/><path d="M{cutoff_x:.2f} 195V340" stroke="#bd562d" stroke-dasharray="4 4"/><path d="M{cutoff_x:.2f} 340H445" stroke="#087c6c" stroke-width="3" stroke-dasharray="4 4"/><text x="{cutoff_x-10:.2f}" y="212" class="small" text-anchor="end">30 km cutoff</text>'
    )
    for d in range(0, 36, 5):
        out.append(
            f'<text x="{55+d/35*390:.2f}" y="364" text-anchor="middle" class="small">{d}</text>'
        )
    out.append(
        '<text x="250" y="389" text-anchor="middle" class="small">Observer-to-water distance (km)</text><text x="24" y="421" class="small">Configured decay ≠ geometric horizon</text></svg>\n'
    )
    return "".join(out)


def shoreline_figure(
    *, geometry=None, source=SOURCE, place=PLACE, place_name="Lighthouse", use_photo=True
):
    """Locate the published photo geotag without inventing a panorama position."""
    g = geometry if geometry is not None else read(OUTPUT / "geometry.json")
    source_feature = next(f for f in g["cells"]["features"] if f["id"] == source)
    ring = source_feature["geometry"]["coordinates"][0]
    bounds = [
        min(p[0] for p in ring),
        min(p[1] for p in ring),
        max(p[0] for p in ring),
        max(p[1] for p in ring),
    ]
    project, scale = projected_viewport(bounds, 480, 410, padding=30)
    camera = read(OUTPUT / "shoreline-sources.json")["photos"][1]["camera"] if use_photo else None
    out = [
        svg_start(
            (
                "Photo A within the Lime Kiln source cell"
                if use_photo
                else "Selected source cell and modeled observers"
            ),
            480,
            550,
        ).replace(
            "Precomputed San Juan Islands model evidence. Physical viewing support, not probability.",
            "Published camera geotag beside retained source-area geometry; photographic context, not model validation.",
        ),
        label(24, 35, "One cell, different views", "heading"),
        '<g transform="translate(0 60)">',
        f'<path d="{paths(source_feature["geometry"], project)}" fill="#eaf2f3"/>',
    ]
    for f in g["active_sources"]["features"]:
        if f["id"] == "land:" + source:
            out.append(
                f'<path d="{paths(f["geometry"], project)}" fill="#ead9b3" fill-rule="evenodd"/>'
            )
    out.append(
        f'<path d="{paths(source_feature["geometry"], project)}" fill="none" stroke="#965b27" stroke-width="3"/>'
    )
    for f in g["observers"]["features"]:
        if f["properties"]["source_h3"] == source:
            x, y = project(f["geometry"]["coordinates"])
            out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#704219" stroke="white"/>')
    x, y = project(place)
    out.extend(
        [
            f'<path d="M{x:.2f},{y-6:.2f}l6,6 -6,6 -6,-6 Z" fill="#bd562d" stroke="white"/>',
            f'<path d="M{x:.2f},{y:.2f}l-30,-35" stroke="#bd562d"/>',
            label(x - 32, y - 42, place_name, "small", "middle"),
        ]
    )
    if use_photo:
        x, y = project(camera["epsg32610"])
        out.extend(
            [
                f'<rect data-photo="A" x="{x-7:.2f}" y="{y-7:.2f}" width="14" height="14" rx="3" fill="#167c88" stroke="white" stroke-width="2"/>',
                f'<path d="M{x+7:.2f},{y:.2f}l35,30" stroke="#167c88"/>',
                label(x + 48, y + 40, "A", "heading"),
                label(445, 15, "N ↑", "small", "end"),
                f'<path d="M30,385h{500*scale:.2f}" stroke="#253f46" stroke-width="3"/>',
                label(30, 372, "500 m", "small"),
                "</g>",
                label(24, 495, "A  Published camera location", "small"),
                label(24, 523, "Brown dots: five modeled observers", "small"),
                "</svg>\n",
            ]
        )
    else:
        out.extend(
            [
                label(445, 15, "N ↑", "small", "end"),
                f'<path d="M30,385h{500*scale:.2f}" stroke="#253f46" stroke-width="3"/>',
                label(30, 372, "500 m", "small"),
                "</g>",
                label(24, 495, "Orange: source cell · beige: active land", "small"),
                label(24, 523, "Brown dots: modeled observers", "small"),
                "</svg>\n",
            ]
        )
    return "".join(out)


def contents():
    family = records()
    files = {}
    for name in ("study", *METRICS):
        for mobile in (False, True):
            suffix = "-mobile" if mobile else ""
            value = map_figure(family, name, mobile)
            files[f"{name}{suffix}.svg"] = value.encode()
            dark = value
            for a, b in {
                "#ffffff": "#182b33",
                "#253f46": "#e4eeec",
                "#eaf2f3": "#213b46",
                "#f6f4eb": "#374b48",
                "#53645b": "#c0cebc",
                "stroke:white": "stroke:#182b33",
                'fill="white"': 'fill="#182b33"',
                "#bd562d": "#ffac7e",
            }.items():
                dark = dark.replace(a, b)
            files[f"{name}{suffix}-dark.svg"] = dark.encode()
    evidence = regional_evidence()
    shoreline = shoreline_figure()
    files["shoreline.svg"] = shoreline.encode()
    for before, after in {
        "#ffffff": "#182b33",
        "#253f46": "#e4eeec",
        "#eaf2f3": "#213b46",
        "#ead9b3": "#71664d",
        "#bd562d": "#ffac7e",
        "#965b27": "#dda16c",
        "#704219": "#e5ba7d",
        "#167c88": "#59c4ca",
    }.items():
        shoreline = shoreline.replace(before, after)
    files["shoreline-dark.svg"] = shoreline.encode()
    files["distance-decay.svg"] = distance_figure(
        evidence["physical_assumptions"]["distance_weight"]
    ).encode()
    receipt = {
        "presentation_contract": "lime_kiln_regional_v2",
        "generation_id": evidence["generation_id"],
        "regional_source_sha256": digest((OUTPUT / "regional-source.json").read_bytes()),
        "source_h3": SOURCE,
        "source_type": "land",
        "pair_count": len(family),
        "metric_fields": METRICS,
        "color_scale": "linear 0–1; neutral vegetation separately hatched",
        "location": {
            "lat": 48.5158,
            "lon": -123.1525,
            "epsg32610": PLACE,
            "source": PLACE_URL,
            "purpose": "lighthouse locator, not a modeled observer",
        },
        "source_pairs_sha256": digest((OUTPUT / "source-pairs.json").read_bytes()),
        "photo": {
            "file": PHOTO,
            "sha256": PHOTO_SHA256,
            "author": "Dave Sizer",
            "date": "2010-09-14",
            "source": PLACE_URL,
            "license": "https://creativecommons.org/licenses/by/2.0/",
            "changes": "none; displayed scaled",
            "checked": "2026-11-06",
        },
        "files": {k: digest(v) for k, v in files.items()},
        "shoreline_sources_sha256": digest((OUTPUT / "shoreline-sources.json").read_bytes()),
        "shoreline_photos": read(OUTPUT / "shoreline-sources.json")["photos"],
    }
    files["manifest.json"] = encode(receipt)
    return files


def check():
    for photo in read(OUTPUT / "shoreline-sources.json")["photos"]:
        if digest((OUTPUT / photo["file"]).read_bytes()) != photo["sha256"]:
            raise ValueError(f"Shoreline photo checksum mismatch: {photo['file']}")
    for name, value in contents().items():
        if (OUTPUT / name).read_bytes() != value:
            raise ValueError(f"Stale Lime Kiln presentation: {name}")
    if digest((OUTPUT / PHOTO).read_bytes()) != PHOTO_SHA256:
        raise ValueError("Photo checksum mismatch")
    return {
        "source": SOURCE,
        "pairs": len(records()),
        "figures": 20,
        "regional_delivery_unchanged": True,
    }


def pair_weights(pair):
    return {f"example_{name}": f"{pair[field]:.6f}" for name, (field, _) in METRICS.items()}


def page_values():
    family = records()
    chosen = max(
        (p for p in family if p["target_h3"] != SOURCE),
        key=lambda p: p["distance_adjusted_viewability"],
    )
    values = {
        **pair_weights(chosen),
        "lime_source": SOURCE,
        "lime_panorama_src": "../assets/examples/lime-kiln/haro-panorama-chris-light.jpg",
        "lime_shoreline": "".join(
            f'<picture class="figure-{theme}"><img src="../assets/examples/lime-kiln/shoreline{suffix}.svg" width="480" height="550" loading="lazy" alt="Close-up of the Lime Kiln H3 source cell, with published photo location A near the lighthouse and the five modeled observer samples"></picture>'
            for theme, suffix in (("light", ""), ("dark", "-dark"))
        ),
        "lime_shore_photo_src": "../assets/examples/lime-kiln/shore-chiwauk.jpg",
        "lime_photo_src": "../assets/examples/lime-kiln/" + PHOTO,
        "lime_count": str(len(family)),
        "lime_positive": str(sum(p["distance_adjusted_viewability"] > 0 for p in family)),
        "lime_neutral": str(sum(no_baseline(p) for p in family)),
        "lime_target": chosen["target_h3"],
        "lime_km": f"{chosen['distance_km']:.2f}",
        "lime_generation": regional_evidence()["generation_id"],
        "lime_observers": str(regional_evidence()["observer_count"]),
    }
    rows = "".join(
        f'<tr><th scope="row">{html.escape(title)}</th><td>{chosen[field]:.6f}</td></tr>'
        for field, title in METRICS.values()
    )
    values["lime_result"] = (
        '<div class="example-table"><table><caption>One recorded pair · land source '
        + SOURCE
        + " → "
        + chosen["target_h3"]
        + "</caption><thead><tr><th>Component</th><th>Recorded value</th></tr></thead><tbody>"
        + rows
        + "</tbody></table></div>"
    )
    for name in ("study", *METRICS):
        pictures = []
        desktop_height = 680 if name == "study" else 755
        mobile_height = 560 if name == "study" else 635
        for theme, suffix in (("light", ""), ("dark", "-dark")):
            base = "../assets/examples/lime-kiln/"
            pictures.append(
                f'<picture class="figure-{theme}"><source media="(max-width:600px)" srcset="{base}{name}-mobile{suffix}.svg" width="480" height="{mobile_height}"><img src="{base}{name}{suffix}.svg" width="960" height="{desktop_height}" alt="Lime Kiln source area and all {len(family)} recorded candidate water targets: {name}" loading="lazy"></picture>'
            )
        values["lime_" + name] = "".join(pictures)
    return values


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name, value in contents().items():
            (OUTPUT / name).write_bytes(value)
    print(json.dumps(check()))
