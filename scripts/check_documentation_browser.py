"""Check and capture the static walkthrough and homepage; never executes a model."""

from __future__ import annotations

import argparse
import functools
import json
import threading
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from render_documentation_walkthrough import check

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        prefix = "/toolkit-viewshed"
        if path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        return super().translate_path(path)

    def log_message(self, format, *args):
        pass


def run(site: Path, output: Path, engines: list[str]) -> dict:
    presentation = check()
    output.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(QuietHandler, directory=str(site.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results = []
    try:
        with sync_playwright() as playwright:
            for engine in engines:
                browser = getattr(playwright, engine).launch()
                errors, failed_assets, blocked = [], [], []
                initial_payload_bytes = None
                context = browser.new_context(reduced_motion="reduce")

                def route(request, _request):
                    if request.request.url.startswith(origin + "/"):
                        request.continue_()
                    else:
                        blocked.append(request.request.url)
                        request.abort()

                context.route("**/*", route)
                page = context.new_page()
                page.on(
                    "pageerror",
                    lambda error: errors.append(
                        {"message": error.message, "stack": error.stack, "url": page.url}
                    ),
                )
                page.on(
                    "response",
                    lambda response: (
                        failed_assets.append(response.url) if response.status >= 400 else None
                    ),
                )
                for width, height, device in ((1440, 1000, "desktop"), (390, 844, "mobile")):
                    page.set_viewport_size({"width": width, "height": height})
                    for theme in ("default", "slate"):
                        mode = "light" if theme == "default" else "dark"
                        for path, name in (("/", "home"), ("/examples/", "examples")):
                            page.goto(origin + path)
                            page.evaluate(
                                "scheme => document.body.setAttribute('data-md-color-scheme', scheme)",
                                theme,
                            )
                            page.evaluate(
                                "async ()=>{for(const img of document.images){img.loading='eager';} await Promise.all([...document.images].map(img=>img.decode().catch(()=>{})));}"
                            )
                            if name == "examples" and device == "desktop" and mode == "light":
                                initial_payload_bytes = page.evaluate(
                                    "performance.getEntriesByType('resource').reduce((sum, item)=>sum+item.decodedBodySize, 0) + document.documentElement.outerHTML.length"
                                )
                                assert initial_payload_bytes <= 5 * 1024**2, initial_payload_bytes
                            assert page.evaluate(
                                "document.documentElement.scrollWidth<=window.innerWidth"
                            ), (device, mode, name)
                            if name == "home":
                                assert page.get_by_role(
                                    "link", name="Read the visual walkthrough", exact=True
                                ).is_visible()
                                assert page.locator(".viewshed-hero").evaluate(
                                    "img=>img.complete && img.naturalWidth>0"
                                )
                            else:
                                assert page.locator(".chapter").count() == 3
                                assert page.locator(".chapter figure").count() == 6
                                assert (
                                    page.locator(
                                        ".walkthrough select, .walkthrough button, #viewshed-explorer"
                                    ).count()
                                    == 0
                                )
                                assert page.locator(".chapter details").count() == 0
                                assert page.locator(".chapter table").count() == 2
                                visible = page.locator(f".figure-{mode} img")
                                assert visible.count() == 6
                                assert visible.evaluate_all(
                                    "imgs=>imgs.every(img=>img.complete && img.naturalWidth>0 && img.getBoundingClientRect().width>250)"
                                )
                                for i in range(6):
                                    figure = page.locator(".chapter figure").nth(i)
                                    figure.scroll_into_view_if_needed()
                                    figure.screenshot(
                                        style=".md-header,.md-tabs{visibility:hidden!important}",
                                        path=str(
                                            output / f"{engine}-{device}-{mode}-figure-{i+1}.png"
                                        ),
                                    )
                                for chapter in ("one-pair", "one-source", "target-summary"):
                                    page.locator("#" + chapter).screenshot(
                                        path=str(
                                            output / f"{engine}-{device}-{mode}-{chapter}.png"
                                        ),
                                        style=".md-header,.md-tabs{visibility:hidden!important}",
                                    )
                            page.screenshot(
                                path=str(output / f"{engine}-{device}-{mode}-{name}.png"),
                                full_page=True,
                            )
                page.goto(origin + "/examples/")
                page.get_by_title("Switch to dark mode", exact=True).click()
                assert page.locator("body").get_attribute("data-md-color-scheme") == "slate"
                page.locator(".figure-dark").first.wait_for(state="visible")
                page.set_viewport_size({"width": 390, "height": 844})
                page.evaluate("document.documentElement.style.zoom='200%'")
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                page.screenshot(path=str(output / f"{engine}-mobile-200-percent.png"))
                page.evaluate("document.documentElement.style.zoom='100%'")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.get_by_role("link", name="Home", exact=True).last.click()
                page.wait_for_url(origin + "/")
                page.get_by_role("heading", name="Viewshed Toolkit", exact=True).wait_for()
                page.wait_for_load_state("networkidle")
                page.go_back()
                page.wait_for_url(lambda url: url.split("#")[0] == origin + "/examples/")
                page.locator(".chapter").first.wait_for()
                page.wait_for_load_state("networkidle")
                assert page.locator(".chapter").count() == 3
                for path in ("/assets/san-juan-demo.html", "/san-juan-demo/"):
                    page.goto(origin + "/toolkit-viewshed" + path)
                    if path.endswith("html"):
                        page.wait_for_url(origin + "/toolkit-viewshed/examples/")
                    else:
                        page.get_by_role("link", name="guided Examples page", exact=True).click()
                        page.wait_for_url(origin + "/toolkit-viewshed/examples/")
                    assert page.locator(".chapter figure").count() == 6
                for width, height, device in ((1440, 1000, "desktop"), (390, 844, "mobile")):
                    fallback = browser.new_context(
                        java_script_enabled=False, viewport={"width": width, "height": height}
                    )
                    fallback.route("**/*", route)
                    static = fallback.new_page()
                    static.goto(origin + "/examples/")
                    for figure in static.locator(".chapter figure").all():
                        figure.scroll_into_view_if_needed()
                    assert static.locator(".chapter").count() == 3
                    assert static.locator(".figure-light img").evaluate_all(
                        "imgs=>imgs.every(img=>img.complete && img.naturalWidth>0)"
                    )
                    assert static.locator(".chapter table").count() == 2
                    assert "maximum" in static.locator("#target-summary").inner_text()
                    assert static.locator(".chapter details").count() == 0
                    assert static.evaluate(
                        "document.documentElement.scrollWidth<=window.innerWidth"
                    )
                    static.screenshot(
                        path=str(output / f"{engine}-{device}-javascript-disabled.png"),
                        full_page=True,
                    )
                    fallback.close()
                assert not errors, errors
                assert not failed_assets, failed_assets
                context.close()
                browser.close()
                results.append(
                    {
                        "engine": engine,
                        "status": "passed",
                        "checks": [
                            "homepage and all three chapters",
                            "six finished figures and visible comparisons",
                            "fixed pair and contributor results",
                            "desktop/mobile light/dark",
                            "loaded local assets",
                            "theme toggle",
                            "200 percent CSS zoom reflow",
                            "JavaScript-disabled desktop/mobile",
                            "compatibility URLs and base path",
                            "instant navigation back",
                            "no required controls",
                            "third-party requests blocked",
                        ],
                        "page_errors": errors,
                        "failed_assets": failed_assets,
                        "blocked_third_party_requests": len(blocked),
                        "initial_payload_bytes": initial_payload_bytes,
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    receipt = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "presentation": presentation,
        "results": results,
        "evidence": "Automated browser assertions and screenshots. Visual inspection is recorded separately; not a novice usability study.",
    }
    (output / "browser-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=ROOT / "site")
    parser.add_argument("--output", type=Path, default=ROOT / "work/browser-qa")
    parser.add_argument("--engines", nargs="+", default=["chromium", "webkit"])
    args = parser.parse_args()
    print(json.dumps(run(args.site, args.output, args.engines)))
