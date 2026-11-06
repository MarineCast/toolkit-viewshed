"""Shared, offline presentation for three regional land-source walkthroughs."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from build_documentation_examples import digest, encode, read
from render_lime_kiln import (
    METRICS,
    map_figure,
    no_baseline,
    page_values,
    records,
    regional_evidence,
    shoreline_figure,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/assets/examples"
TEMPLATES = ROOT / "scripts/templates"
DARK = {
    "#ffffff": "#182b33",
    "#253f46": "#e4eeec",
    "#eaf2f3": "#213b46",
    "#f6f4eb": "#374b48",
    "#53645b": "#c0cebc",
    "stroke:white": "stroke:#182b33",
    'fill="white"': 'fill="#182b33"',
    "#bd562d": "#ffac7e",
    "#ead9b3": "#71664d",
    "#965b27": "#dda16c",
    "#704219": "#e5ba7d",
}


def cases():
    return read(ASSETS / "places.json")


def tabs(selected):
    entries = [(key, c["tab"], c["page"]) for key, c in cases().items()]
    entries.append(("region", "Putting it all together", "examples-region.md"))
    return (
        '<nav class="example-tabs" aria-label="Visual walkthrough locations" markdown="1">\n\n'
        + "\n\n".join(
            f"[{title}]({page}){{ .example-tab"
            + (' aria-current="page"' if key == selected else "")
            + " }"
            for key, title, page in entries
        )
        + "\n\n</nav>"
    )


def picture(key, name, alt, width=960, height=755, mobile=True):
    result = []
    for theme, suffix in [("light", ""), ("dark", "-dark")]:
        base = f"../assets/examples/{key}/{name}"
        source = (
            f'<source media="(max-width:600px)" srcset="{base}-mobile{suffix}.svg" width="480" height="{560 if name=="study" else 635}">'
            if mobile
            else ""
        )
        result.append(
            f'<picture class="figure-{theme}">{source}<img src="{base}{suffix}.svg" width="{width}" height="{height}" alt="{html.escape(alt)}" loading="lazy"></picture>'
        )
    return "".join(result)


def photo(key, index, caption="", cls=""):
    p = cases()[key]["photos"][index]
    credit = f'<a href="{p["source"]}">{html.escape(p["author"])}</a>, {p["date"]}, <a href="{p["license"]}">{p["license_label"]}</a>. {p["changes"]}.'
    return f'<figure class="{cls}"><a href="{p["source"]}"><img src="../assets/examples/{key}/{p["file"]}" width="{p["width"]}" height="{p["height"]}" alt="{html.escape(p["alt"])}" loading="lazy"></a><figcaption>{caption} Photograph: {credit}</figcaption></figure>'


def source_values(key):
    if key == "lime-kiln":
        return page_values()
    c = cases()[key]
    output = ASSETS / key
    rows = records(output, c["source"])
    evidence = regional_evidence(output)
    field = (
        "distance_adjusted_viewability"
        if any(r["distance_adjusted_viewability"] > 0 for r in rows)
        else "distance_weighted_los_support"
    )
    chosen = sorted(
        (r for r in rows if r["target_h3"] != c["source"]),
        key=lambda r: (-r[field], r["target_h3"]),
    )[0]
    values = {
        "lime_source": c["source"],
        "lime_count": str(len(rows)),
        "lime_positive": str(sum(r["distance_adjusted_viewability"] > 0 for r in rows)),
        "lime_neutral": str(sum(no_baseline(r) for r in rows)),
        "lime_target": chosen["target_h3"],
        "lime_km": f'{chosen["distance_km"]:.2f}',
        "lime_generation": evidence["generation_id"],
        "lime_observers": str(evidence["observer_count"]),
    }
    table = "".join(
        f'<tr><th scope="row">{html.escape(label)}</th><td>{chosen[field]:.6f}</td></tr>'
        for field, label in METRICS.values()
    )
    values["lime_result"] = (
        f'<div class="example-table"><table><caption>One recorded pair · land source {c["source"]} → {chosen["target_h3"]}</caption><thead><tr><th>Component</th><th>Recorded value</th></tr></thead><tbody>{table}</tbody></table></div>'
    )
    for name in ["study", *METRICS]:
        values["lime_" + name] = picture(
            key,
            name,
            f'{c["name"]}: all {len(rows)} recorded water targets, {name}',
            height=680 if name == "study" else 755,
        )
    return values


def shoreline(key):
    if key == "lime-kiln":
        return (TEMPLATES / "lime-kiln-shoreline.html").read_text()
    c = cases()[key]
    locator = picture(
        key, "shoreline", f'{c["name"]} source cell and retained observer samples', 480, 550, False
    )
    return f"""<section class="chapter" id="shoreline" markdown="1">

## 6. From the map to the shoreline

<p class="chapter-question">What does this overhead grid feel like at eye level?</p>

{photo(key,0,'<strong>The wider setting.</strong> These photographs describe the place; their exact camera coordinates are not published.', 'shore-panorama')}

<div class="shore-detail" markdown="1">
<div class="shore-location" markdown="1">

### One cell, different viewing spots

<figure class="shore-locator">{locator}<figcaption>Orange outline: the selected R7 cell. Beige: retained active land. Brown dots: modeled observers. The diamond is the cell center, not a camera pin. This is not a trail map.</figcaption></figure>

{c['shore_reading']}

[See the combined map again](#combined){{ .md-button }}

</div>
{photo(key,1,'<strong>At eye level.</strong> Wider place context; not a documented model observer position.', 'shore-photo')}
</div>

<p class="takeaway"><strong>Use the photographs to read the landscape.</strong> They illustrate foreground obstructions and open directions. They do not establish the numerical weights, current conditions or the probability of spotting an animal. Dates differ from the mixed canopy inputs.</p>

[Photo sources and location notes](assets/examples/{key}/manifest.json) record the credits. These images have no camera pins because exact camera coordinates are unverified.

</section>"""


def markdown(key):
    c = cases()[key]
    values = source_values(key)
    values.update(
        {
            "example_tabs": tabs(key),
            "place_key": key,
            "place_title": c["title"],
            "place_lead": c["lead"],
            "place_text": c["text"],
            "place_intro": c["intro"],
            "hero_photo": c.get("hero_html") or photo(key, 0, "", "place-photo"),
            "center_note": c["center_note"],
            "selection_note": c["selection_note"],
            "site_result_note": c["result_note"],
            "shoreline_content": shoreline(key),
        }
    )
    result = (TEMPLATES / "source-walkthrough.md").read_text()
    for _ in range(3):
        for name, value in values.items():
            result = result.replace("{{" + name + "}}", str(value))
    if "{{" in result:
        raise ValueError("Unresolved shared walkthrough placeholder")
    return result


def outputs(key):
    c = cases()[key]
    output = ASSETS / key
    g = read(output / "geometry.json")
    rows = records(output, c["source"])
    files = {}
    for name in ["study", *METRICS, "shoreline"]:
        for mobile in ([False] if name == "shoreline" else [False, True]):
            suffix = "-mobile" if mobile else ""
            value = (
                shoreline_figure(
                    geometry=g,
                    source=c["source"],
                    place=c["place"],
                    place_name="Cell center",
                    use_photo=False,
                )
                if name == "shoreline"
                else map_figure(
                    rows,
                    name,
                    mobile,
                    geometry=g,
                    source=c["source"],
                    place=c["place"],
                    place_name=c["name"],
                    labels=[],
                )
            )
            value = value.replace(
                "Published camera geotag beside retained source-area geometry; photographic context, not model validation.",
                "Retained source-area geometry and modeled observers; no photo camera is located here.",
            )
            files[f"{name}{suffix}.svg"] = value.encode()
            for before, after in DARK.items():
                value = value.replace(before, after)
            files[f"{name}{suffix}-dark.svg"] = value.encode()
    receipt = {
        "presentation_contract": "regional_source_walkthrough_v1",
        "source_h3": c["source"],
        "source_type": "land",
        "generation_id": regional_evidence(output)["generation_id"],
        "regional_source_sha256": digest((output / "regional-source.json").read_bytes()),
        "pair_count": len(rows),
        "photos": c["photos"],
        "files": {name: digest(value) for name, value in files.items()},
    }
    files["manifest.json"] = encode(receipt)
    return files


def check():
    for key, c in cases().items():
        markdown(key)
        if key == "lime-kiln":
            continue
        for name, value in outputs(key).items():
            if (ASSETS / key / name).read_bytes() != value:
                raise ValueError(f"Stale source presentation: {key}/{name}")
        for p in c["photos"]:
            if digest((ASSETS / key / p["file"]).read_bytes()) != p["sha256"]:
                raise ValueError("Photo checksum mismatch")
    return {"source_walkthroughs": 3, "additional_map_variants": 44}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        for key in cases():
            if key != "lime-kiln":
                for name, value in outputs(key).items():
                    (ASSETS / key / name).write_bytes(value)
    print(json.dumps(check()))
