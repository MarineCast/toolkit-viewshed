"""Read-only build hook inserts checked real bundle values into authored prose."""

import html
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "docs/assets/examples/san-juan"


def on_pre_build(config):
    config.extra["source_revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
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

    subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts/render_documentation_walkthrough.py"),
            "--check",
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
            revision = config.extra["source_revision"]
            return (
                f"](https://github.com/MarineCast/toolkit-viewshed/{kind}/{revision}/{path}"
                + ("#" + anchor if anchor else "")
                + ")"
            )
        return match.group(0)

    markdown = re.sub(r"\]\((\.\./[^)]+)\)", source_link, markdown)
    if page.file.src_uri not in {"examples.md", "products/index.md"}:
        return markdown
    # Standard-library presentation renderer is shared by the build and offline checks.
    sys.path.insert(0, str(ROOT / "scripts"))
    from render_documentation_examples import number
    from render_documentation_walkthrough import METRIC, target_summary

    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    raw = json.loads((BUNDLE / "pairs.json").read_text())
    pairs = [dict(zip(raw["columns"], row, strict=True)) for row in raw["rows"]]
    default = next(p for p in pairs if p["id"] == manifest["defaults"]["pair_id"])
    summary = target_summary(pairs)
    family = [
        p for p in pairs if p["source_type"] == "land" and p["source_h3"] == default["source_h3"]
    ]
    target = next(t for t in summary["targets"] if t["target_h3"] == default["target_h3"])
    candidates = sorted(
        (p for p in pairs if p["source_type"] == "land" and p["target_h3"] == default["target_h3"]),
        key=lambda p: (-p[METRIC], p["source_h3"]),
    )
    coverage = json.loads((BUNDLE / "coverage.json").read_text())
    profiles = json.loads((BUNDLE / "profiles.json").read_text())
    years = {record["source_year"] for record in manifest["source_vintages"]}
    year = next(iter(years)) if len(years) == 1 and None not in years else "unknown or mixed"
    assumptions = manifest["assumptions"]
    h3 = assumptions["h3"]["target_resolution"]
    resolution = manifest["analysis_resolution_m"]
    cutoff = assumptions["distance_weight"]["hard_cutoff_km"]
    values = {
        "metadata": f"Recorded real-data results · H3 R{h3} · {resolution} m analysis · {year} canopy · {cutoff} km modeled distance limit",
        "analysis_resolution": resolution,
        "h3_resolution": h3,
        "canopy_year": year,
        "cutoff": cutoff,
        "clearance": assumptions["viewshed"]["observer_canopy_clearance_radius_m"],
        "profile_step": profiles[0]["display_step_m"],
        "generation": manifest["generation_id"],
        "missing_canopy": f'{100*coverage["rasters"]["chm"]["missing_land_fraction"]:.2f}%',
        "land_source_count": len(summary["included_sources"]),
    }

    def table(headers, rows, caption):
        return (
            '<div class="example-table" role="region" aria-label="Recorded results"><table><caption>'
            + caption
            + "</caption><thead><tr>"
            + "".join('<th scope="col">' + v + "</th>" for v in headers)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>"
                + "".join(
                    ('<th scope="row">' if i == 0 else "<td>")
                    + html.escape(str(v))
                    + ("</th>" if i == 0 else "</td>")
                    for i, v in enumerate(row)
                )
                + "</tr>"
                for row in rows
            )
            + "</tbody></table></div>"
        )

    values["pair_comparison"] = table(
        ["Same pair A → B", "Recorded value"],
        [
            ["Ground-only, unweighted", number(default["line_of_sight_support"])],
            ["Ground + trees, unweighted", number(default["physical_viewability"])],
            ["Combined, distance included", number(default[METRIC])],
        ],
        "One pair · matched population · different quantities",
    )
    bare, canopy = default["line_of_sight_support"], default["physical_viewability"]
    change = "lower" if canopy < bare else "equal" if canopy == bare else "higher"
    values["pair_reading"] = (
        f"For A → B, unweighted ground support is {number(bare)} and ground-plus-trees support is {number(canopy)} ({change}). The recorded combined score is {number(default[METRIC])}. These are model support indices, not percentages of visible water or detection probabilities."
    )
    counts = [
        sum(p[k] > 0 for p in family)
        for k in ("line_of_sight_support", "physical_viewability", METRIC)
    ]
    values["source_reading"] = (
        f"Among A's {len(family)} candidate target cells, {counts[0]} have positive ground-only support, {counts[1]} have positive ground-plus-trees support, and {counts[2]} have positive combined support. The other {len(family)-counts[2]} combined scores are computed zeros. The largest combined score is {number(max(p[METRIC] for p in family))}."
    )
    selected = candidates[:3]
    if default not in selected:
        selected = [*selected, default]
    rows = [
        [
            "A" if p["source_h3"] == default["source_h3"] else p["source_h3"],
            number(p[METRIC]),
            "Strongest" if p[METRIC] == target["maximum_valid_combined_weight"] else "Valid",
        ]
        for p in selected
    ]
    others = [p for p in candidates if p not in selected]
    if others:
        rows.append(
            [
                f"{len(others)} other included sources",
                number(max(p[METRIC] for p in others)),
                f"Maximum among these; {sum(p[METRIC]==0 for p in others)} zeros",
            ]
        )
    values["contributors"] = table(
        ["Included land source", "Combined", "Contribution"],
        rows,
        f"Target B · {target['valid_count']} valid / {target['candidate_count']} candidates · strongest three and A",
    )
    values["target_reading"] = (
        f"B's strongest included land-source score is {number(target['maximum_valid_combined_weight'])}; A contributes {number(default[METRIC])}. The remaining {target['outside_candidate_count']} of the {len(summary['included_sources'])} included land sources are outside B's candidate set. All contributor rows above map to role/source/target identities in the downloadable summary and canonical records."
    )
    active = [t for t in summary["targets"] if t["candidate_count"]]
    values["coverage_reading"] = (
        f"This summary covers {len(active)} target cells with land-source candidates. It has {sum(t['valid_count'] for t in active):,} valid candidate records and {sum(t['missing_count'] for t in active):,} missing candidate records. Coverage is complete within those recorded candidate sets, not across every possible source area."
    )
    values["read_one_result"] = (
        f"The committed land pair `{default['source_h3']} → {default['target_h3']}` (A → B) has unweighted ground support **{number(bare)}**, directly calculated unweighted canopy support **{number(canopy)}**, and combined weight **{number(default[METRIC])}**. Distance attenuation is already included in the combined value. It is a modeled support score, not the probability of seeing an animal."
    )
    alts = {
        "pair-map": "Source A, target B and recorded P to Q samples within San Juan geography",
        "profile": "Recorded ground, ground plus canopy and viewing ray along the P to Q path",
        "ground": "Every target candidate for A, colored by unweighted ground support",
        "canopy": "Same candidates, colored by directly calculated unweighted canopy support",
        "combined": "Same candidates, colored by combined support including distance",
        "target-summary": "Maximum valid combined weight per target across included land sources",
    }

    def dimensions(name):
        svg = (ROOT / "docs/assets/examples/walkthrough" / name).read_text()
        match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        return f'width="{match[1]}" height="{match[2]}"'

    for name, alt in alts.items():
        pictures = []
        for theme, suffix in (("light", ""), ("dark", "-dark")):
            desktop = name + suffix + ".svg"
            mobile = name + "-mobile" + suffix + ".svg"
            base = "../assets/examples/walkthrough/"
            pictures.append(
                f'<picture class="figure-{theme}"><source media="(max-width: 600px)" srcset="{base}{mobile}" {dimensions(mobile)}><img src="{base}{desktop}" {dimensions(desktop)} alt="{html.escape(alt)}" loading="lazy"></picture>'
            )
        values["figure_" + name] = "".join(pictures)
    for key, value in values.items():
        markdown = markdown.replace("{{" + key + "}}", str(value))
    if "{{" in markdown:
        raise ValueError("Unresolved instructional data placeholder")
    return markdown
