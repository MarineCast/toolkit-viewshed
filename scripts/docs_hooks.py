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
    if page.file.src_uri != "examples.md":
        return markdown
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    data = json.loads((BUNDLE / "pairs.json").read_text())
    pairs = {row[0]: dict(zip(data["columns"], row, strict=True)) for row in data["rows"]}
    default = pairs[manifest["defaults"]["pair_id"]]
    lessons = json.loads((BUNDLE / "lessons.json").read_text())
    coverage = json.loads((BUNDLE / "coverage.json").read_text())
    grid = json.loads((BUNDLE / "inputs/display-grid.json").read_text())
    profiles = json.loads((BUNDLE / "profiles.json").read_text())
    assumptions = manifest["assumptions"]
    years = {record["source_year"] for record in manifest["source_vintages"]}
    parameters = {
        "analysis_resolution": manifest["analysis_resolution_m"],
        "dem_native": manifest["native_resolution_m"]["dem"],
        "canopy_native": manifest["native_resolution_m"]["canopy"],
        "canopy_year": (
            next(iter(years)) if len(years) == 1 and None not in years else "unknown or mixed"
        ),
        "display_resolution": grid["display_resolution_m"],
        "profile_step": profiles[0]["display_step_m"],
        "sample_min": assumptions["h3"]["min_sample_points_per_source_cell"],
        "sample_max": assumptions["h3"]["sample_points_per_source_cell"],
        "clearance": assumptions["viewshed"]["observer_canopy_clearance_radius_m"],
        "midpoint": assumptions["distance_weight"]["logistic_d50_km"],
        "slope": assumptions["distance_weight"]["logistic_slope_km"],
        "cutoff": assumptions["distance_weight"]["hard_cutoff_km"],
        "analysis_crs": manifest["crs"],
    }
    curve = manifest["curve_contract"]
    cfg = curve["settings"]
    model = cfg["selected_model"]
    if model == "logistic":
        rule = f"logistic rule has a {cfg['logistic_d50_km']} km midpoint and {cfg['logistic_slope_km']} km slope scale"
    elif model == "exponential":
        rule = f"exponential rule has a {cfg['exponential_lambda_km']} km decay scale"
    else:
        near = (
            cfg["piecewise_near_km"]
            if cfg["piecewise_near_km"] is not None
            else cfg["piecewise_full_weight_km"]
        )
        far = (
            cfg["piecewise_far_km"]
            if cfg["piecewise_far_km"] is not None
            else cfg["piecewise_zero_weight_km"]
        )
        rule = f"piecewise rule retains full support through {near} km and reaches zero at {far} km"
    parameters["distance_rule"] = (
        f"The configured {rule}, {'with' if cfg['normalize_at_zero'] else 'without'} normalization at zero, and a {curve['extent_km']} km cutoff."
    )
    for key, value in parameters.items():
        markdown = markdown.replace("{{" + key + "}}", str(value))

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
    bare, canopy = default["line_of_sight_support"], default["physical_viewability"]
    effect = (
        "removes most of that support"
        if bare > 0.02 and canopy / bare <= 0.2
        else (
            "changes little of that support"
            if bare > 0.02 and canopy / bare >= 0.98
            else "changes the modeled support"
        )
    )
    markdown = markdown.replace(
        "{{canopy_reading}}",
        f"Ground alone leaves modeled viewing support toward B. Including the mapped tree heights {effect}: unweighted support falls from {fmt(bare)} to {fmt(canopy)}. This describes modeled geometry, not the chance of seeing an animal.",
    )
    near, far = [
        pairs[key]
        for key in next(lesson for lesson in lessons if lesson["id"] == "distance")["pair_ids"]
    ]
    markdown = markdown.replace(
        "{{distance_reading}}",
        f"{'From the same observer area' if near['source_h3'] == far['source_h3'] else 'Changing observer area'}, nearer water is {near['distance_km']:.2f} km away with a diagnostic of {fmt(near['distance_detection_weight'])}; farther water is {far['distance_km']:.2f} km away with {fmt(far['distance_detection_weight'])}.",
    )
    markdown = markdown.replace(
        "{{distance_population}}",
        (
            "These two cases share one observer area"
            if near["source_h3"] == far["source_h3"]
            else "The observer area changes between these two cases"
        ),
    )
    terrain = next(lesson for lesson in lessons if lesson["id"] == "terrain")
    open_pair, blocked = [pairs[key] for key in terrain["pair_ids"]]
    markdown = markdown.replace(
        "{{terrain_reading}}",
        f"Open ground has unweighted ground support {fmt(open_pair['line_of_sight_support'])}; Ground-blocked example has {fmt(blocked['line_of_sight_support'])}.",
    )
    for lesson in lessons:
        ident = lesson["id"]
        markdown = markdown.replace(
            "{{figure_" + ident + "}}", (BUNDLE / f"previews/{ident}.svg").read_text()
        )
        rows = []
        distance_only = ident == "distance"
        for case in lesson["cases"]:
            pair = pairs[case["pair_id"]]
            label = html.escape(case["title"] + " (" + pair["source_type"] + ")")
            if distance_only:
                values = f'<td>{pair["distance_km"]:.2f} km</td><td>{fmt(pair["distance_detection_weight"])}</td>'
            else:
                values = "".join(
                    f"<td>{fmt(pair[field])}</td>"
                    for field in (
                        "line_of_sight_support",
                        "physical_viewability",
                        "distance_adjusted_viewability",
                    )
                )
            rows.append(f'<tr><th scope="row">{label}</th>{values}</tr>')
        headers = (
            ["Case", "Centroid distance", "Distance diagnostic"]
            if distance_only
            else ["Case", "Ground LOS", "Ground + trees LOS", "Combined"]
        )
        caption = (
            "Curated distance comparison · same observer area"
            if distance_only and lesson["shared_source"]
            else (
                "Worked example A → B and named comparisons · fixed 0\u20131 support scale"
                if lesson["worked_example"] == "A → B"
                else "Curated ground comparison · fixed 0\u20131 support scale"
            )
        )
        table = (
            '<div class="example-table" role="region" aria-label="Scrollable actual results"><table><caption>'
            + caption
            + "</caption><thead><tr>"
            + "".join('<th scope="col">' + title + "</th>" for title in headers)
            + "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
        markdown = markdown.replace("{{table_" + ident + "}}", table)
    if "{{" in markdown:
        raise ValueError("Unresolved instructional data placeholder")
    return markdown
