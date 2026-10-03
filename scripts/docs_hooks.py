"""Read-only build hook inserts checked real bundle values into authored prose."""

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "docs/assets/examples/san-juan"


def on_pre_build(config):
    subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts/build_documentation_examples.py"),
            "--check",
            "--output",
            str(BUNDLE),
        ],
        check=True,
        capture_output=True,
    )


def on_page_markdown(markdown, page, config, files):
    def source_link(match):
        target = match.group(1)
        filename, _, anchor = target.partition("#")
        resolved = (ROOT / "docs" / Path(page.file.src_uri).parent / filename).resolve()
        if (
            resolved.is_relative_to(ROOT)
            and not resolved.is_relative_to(ROOT / "docs")
            and resolved.exists()
        ):
            kind = "tree" if resolved.is_dir() else "blob"
            path = quote(str(resolved.relative_to(ROOT)))
            return (
                f"](https://github.com/MarineCast/toolkit-viewshed/{kind}/main/{path}"
                + ("#" + anchor if anchor else "")
                + ")"
            )
        return match.group(0)

    markdown = re.sub(r"\]\((\.\./[^)]+)\)", source_link, markdown)
    if page.file.src_uri != "examples.md":
        return markdown
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    data = json.loads((BUNDLE / "pairs.json").read_text())
    pairs = {row[0]: dict(zip(data["columns"], row, strict=True)) for row in data["rows"]}
    default = pairs[manifest["defaults"]["pair_id"]]
    lessons = json.loads((BUNDLE / "lessons.json").read_text())
    coverage = json.loads((BUNDLE / "coverage.json").read_text())

    def fmt(value):
        if value is None:
            return "Unavailable"
        if value == 0:
            return "0 (modeled zero)"
        return f"{value:.3g}" if value < 0.001 else f"{value:.3f}"

    for field in (
        "line_of_sight_support",
        "physical_viewability",
        "distance_weighted_los_support",
        "vegetation_attenuation",
        "distance_adjusted_viewability",
    ):
        markdown = markdown.replace("{{" + field + "}}", fmt(default[field]))
    markdown = markdown.replace(
        "{{missing_canopy}}", f'{100*coverage["rasters"]["chm"]["missing_land_fraction"]:.2f}%'
    )
    markdown = markdown.replace("{{generation}}", manifest["generation_id"])
    markdown = markdown.replace("{{source}}", default["source_h3"]).replace(
        "{{target}}", default["target_h3"]
    )
    for lesson in lessons:
        ident = lesson["id"]
        markdown = markdown.replace(
            "{{figure_" + ident + "}}", (BUNDLE / f"previews/{ident}.svg").read_text()
        )
        rows = []
        for i, pair_id in enumerate(lesson["pair_ids"]):
            pair = pairs[pair_id]
            rows.append(
                f'<tr><th scope="row">Example {i+1} ({pair["source_type"]})</th><td>{pair["distance_km"]:.2f} km</td><td>{fmt(pair["line_of_sight_support"])}</td><td>{fmt(pair["physical_viewability"])}</td><td>{fmt(pair["distance_adjusted_viewability"])}</td></tr>'
            )
        table = (
            '<div class="example-table"><table><caption>Actual modeled values · fixed 0\u20131 support scale</caption><thead><tr><th>Pair</th><th>Distance</th><th>Ground LOS</th><th>Ground + trees LOS</th><th>Combined</th></tr></thead><tbody>'
            + "".join(rows)
            + "</tbody></table></div>"
        )
        markdown = markdown.replace("{{table_" + ident + "}}", table)
    if "{{" in markdown:
        raise ValueError("Unresolved instructional data placeholder")
    return markdown
