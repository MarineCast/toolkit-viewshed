"""Automated local browser QA over precomputed real data; never runs a model."""

from __future__ import annotations

import argparse
import functools
import json
import threading
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from build_documentation_examples import seal_manifest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def run(site: Path, output: Path, engines: list[str]) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(QuietHandler, directory=str(site.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    bundle = ROOT / "docs/assets/examples/san-juan"
    raw = json.loads((bundle / "pairs.json").read_text())
    pairs = {row[0]: dict(zip(raw["columns"], row, strict=True)) for row in raw["rows"]}
    results = []
    try:
        with sync_playwright() as playwright:
            for engine in engines:
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
                )
                blocked = []

                def route(request, _request, blocked=blocked):
                    if request.request.url.startswith(origin + "/"):
                        request.continue_()
                    else:
                        blocked.append(request.request.url)
                        request.abort()

                context.route("**/*", route)
                page = context.new_page()
                errors = []
                page.on(
                    "pageerror",
                    lambda error, errors=errors: errors.append(f"{error.message}\n{error.stack}"),
                )
                response = page.goto(origin + "/examples/")
                root = page.locator('#viewshed-explorer[data-ready="true"]')
                root.wait_for(timeout=20000)
                initial_bytes = len(response.body()) + page.evaluate(
                    "performance.getEntriesByType('resource').reduce((sum,item)=>sum+item.decodedBodySize,0)"
                )
                assert initial_bytes <= 5 * 1024**2, initial_bytes

                def state(root=root):
                    return json.loads(root.get_attribute("data-state"))

                def control(name, value, page=page):
                    field = page.locator(f'[data-control="{name}"]')
                    if not field.is_visible():
                        page.locator(".vs-advanced > summary").click()
                    field.select_option(value)

                def assert_pair(page=page, state=state):
                    current = state()
                    identity = (
                        f'{current["source_type"]}:{current["source_h3"]}:{current["target_h3"]}'
                    )
                    record = pairs.get(identity)
                    actual = page.locator(".vs-status").get_attribute("data-pair-value")
                    assert (
                        (float(actual) == record["distance_adjusted_viewability"])
                        if record
                        else actual == "unavailable"
                    ), (identity, actual)
                    return record

                initial = state()
                original = assert_pair()
                for lesson in [
                    "inputs",
                    "samples",
                    "distance",
                    "terrain",
                    "canopy",
                    "combined",
                    "inverse",
                ]:
                    page.locator(f'[data-lesson="{lesson}"]').click()
                    assert state()["lesson"] == lesson
                    assert_pair()
                page.locator('[data-action="reset"]').click()
                assert state() == initial
                control("direction", "inverse")
                assert (
                    assert_pair()["distance_adjusted_viewability"]
                    == original["distance_adjusted_viewability"]
                )
                control("direction", "forward")
                assert (
                    assert_pair()["distance_adjusted_viewability"]
                    == original["distance_adjusted_viewability"]
                )
                control("source_type", "water")
                assert (
                    state()["source_h3"] == initial["source_h3"]
                )  # actual mixed cell, distinct role
                assert_pair()
                control("factor", "retention")
                assert "Not applicable" in page.locator(".vs-status").inner_text()
                control("source_type", "land")
                for factor in ["bare", "canopy", "distance", "integrated", "retention", "combined"]:
                    control("factor", factor)
                    record = assert_pair()
                    field = {
                        "bare": "line_of_sight_support",
                        "canopy": "physical_viewability",
                        "distance": "distance_detection_weight",
                        "integrated": "distance_weighted_los_support",
                        "retention": "vegetation_attenuation",
                        "combined": "distance_adjusted_viewability",
                    }[factor]
                    display = page.locator(".vs-status").inner_text().rsplit(": ", 1)[1]
                    value = float(display.split()[0])
                    assert abs(value - record[field]) <= max(0.000500001, record[field] * 0.005)
                neutral = next(
                    p
                    for p in pairs.values()
                    if p["source_type"] == "land"
                    and p["vegetation_state"] == "no_baseline_support_neutral"
                )
                control("source_h3", neutral["source_h3"])
                control("target_h3", neutral["target_h3"])
                control("factor", "retention")
                assert "no baseline support" in page.locator(".vs-status").inner_text()
                tiny = next(
                    p for p in pairs.values() if 0 < p["distance_adjusted_viewability"] < 0.001
                )
                control("source_type", tiny["source_type"])
                control("source_h3", tiny["source_h3"])
                control("target_h3", tiny["target_h3"])
                control("factor", "combined")
                assert "modeled zero" not in page.locator(".vs-status").inner_text()
                assert_pair()
                # Deliberately query an absent candidate instead of manufacturing a zero.
                targets = [
                    option.get_attribute("value")
                    for option in page.locator('[data-control="target_h3"] option').all()
                ]
                absent = next(
                    target
                    for target in targets
                    if f'{state()["source_type"]}:{state()["source_h3"]}:{target}' not in pairs
                )
                control("target_h3", absent)
                assert "No candidate result" in page.locator(".vs-status").inner_text()
                assert_pair()
                page.locator('[data-action="reset"]').click()
                selected = page.locator('.vs-map [tabindex="0"]').first
                selected.focus()
                page.keyboard.press("ArrowRight")
                assert state()["target_h3"] != initial["target_h3"]
                assert page.locator('.vs-map [tabindex="0"]').evaluate(
                    "e=>e===document.activeElement"
                )
                page.locator('[data-action="reset"]').click()
                page.locator('[data-action="next"]').click()
                assert state()["lesson"] == "samples"
                page.locator('[data-action="previous"]').click()
                assert state()["lesson"] == "inputs"
                page.locator('[data-action="ground-layer"]').click()
                assert state()["layer"] == "ground"
                page.locator('[data-action="canopy-layer"]').click()
                assert state()["layer"] == "canopy_height"
                page.locator('[data-action="reset"]').click()
                page.screenshot(path=str(output / f"{engine}-desktop-light-viewport.png"))
                page.screenshot(path=str(output / f"{engine}-desktop-light.png"), full_page=True)
                page.get_by_title("Switch to dark mode", exact=True).click()
                assert page.locator("body").get_attribute("data-md-color-scheme") == "slate"
                page.screenshot(path=str(output / f"{engine}-desktop-dark.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator(".vs-explorer-card").scroll_into_view_if_needed()
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                page.screenshot(path=str(output / f"{engine}-mobile-dark.png"))
                page.get_by_title("Switch to light mode", exact=True).click()
                page.screenshot(path=str(output / f"{engine}-mobile-light.png"))
                page.get_by_title("Switch to dark mode", exact=True).click()
                page.locator('[data-action="next"]').click()
                assert state()["lesson"] == "samples"
                page.evaluate("document.documentElement.style.zoom='200%'")
                assert page.locator('[data-action="next"]').is_visible()
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                page.screenshot(path=str(output / f"{engine}-mobile-200-percent.png"))
                page.evaluate("document.documentElement.style.zoom='100%'")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.get_by_role("link", name="Overview", exact=True).first.click()
                page.wait_for_url(origin + "/")
                page.get_by_role("heading", name="Documentation", exact=True).wait_for()
                page.go_back()
                page.wait_for_url(lambda url: url.split("#")[0] == origin + "/examples/")
                root.wait_for(timeout=20000)
                page.locator('[data-action="next"]').click()
                assert state()["lesson"] == "samples"
                assert not errors, errors
                fallback = browser.new_context(
                    java_script_enabled=False, viewport={"width": 390, "height": 844}
                )
                fallback.route("**/*", route)
                static = fallback.new_page()
                static.goto(origin + "/examples/")
                assert static.locator(".lesson").count() == 7
                assert static.locator(".lesson > figure svg").count() == 7
                coverage = json.loads((bundle / "coverage.json").read_text())
                missing = coverage["rasters"]["chm"]["missing_land_fraction"]
                assert f"{100 * missing:.2f}%" in static.locator(".viewshed-examples").inner_text()
                static.locator("#lesson-canopy details summary").click()
                assert static.locator("#lesson-canopy table").is_visible()
                static.screenshot(path=str(output / f"{engine}-javascript-disabled.png"))
                fallback.close()
                context.close()
                semantic_failures = []
                for field in (
                    "analysis_resolution_m",
                    "crs",
                    "assumptions",
                    "source_vintages",
                    "curve_contract",
                ):
                    invalid = json.loads((bundle / "manifest.json").read_text())
                    if field == "analysis_resolution_m":
                        invalid[field] += 1
                    elif field == "crs":
                        invalid[field] = "EPSG:4326"
                    elif field == "assumptions":
                        invalid[field]["viewshed"]["observer_canopy_clearance_radius_m"] += 1
                    elif field == "source_vintages":
                        invalid[field][0]["source_year"] = 1999
                    else:
                        invalid[field]["extent_km"] -= 1
                    seal_manifest(invalid)
                    bad_context = browser.new_context()
                    bad_context.route("**/*", route)
                    bad_context.route(
                        "**/san-juan/manifest.json",
                        lambda request, _request, invalid=invalid: request.fulfill(json=invalid),
                    )
                    bad_page = bad_context.new_page()
                    bad_page.goto(origin + "/examples/")
                    bad_page.get_by_role("status").filter(
                        has_text="interactive bundle could not be loaded"
                    ).wait_for()
                    assert bad_page.locator(".lesson > figure svg").count() == 7
                    assert bad_page.locator('#viewshed-explorer[data-ready="true"]').count() == 0
                    semantic_failures.append(field)
                    bad_context.close()
                browser.close()
                results.append(
                    {
                        "engine": engine,
                        "status": "passed",
                        "checks": [
                            "seven lessons",
                            "forward/inverse parity",
                            "mixed land/water role",
                            "six factors",
                            "neutral and not-applicable states",
                            "tiny positive",
                            "noncandidate",
                            "keyboard map",
                            "reset/back/next",
                            "real input layers",
                            "dark theme",
                            "mobile",
                            "200 percent zoom",
                            "instant navigation back",
                            "third-party blocked",
                            "JavaScript disabled",
                        ],
                        "initial_payload_bytes": initial_bytes,
                        "blocked_third_party_requests": len(blocked),
                        "page_errors": errors,
                        "rehashed_semantic_mutations_rejected": semantic_failures,
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    receipt = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "results": results,
        "evidence": "automated browser checks and screenshots; not a human usability study",
    }
    (output / "browser-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=ROOT / "site")
    parser.add_argument("--output", type=Path, default=ROOT / "work/browser-qa")
    parser.add_argument("--engines", nargs="+", default=["chromium", "webkit"])
    args = parser.parse_args()
    print(json.dumps(run(args.site, args.output, args.engines)))


if __name__ == "__main__":
    main()
