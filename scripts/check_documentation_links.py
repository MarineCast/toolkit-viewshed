"""Check repository-local Markdown/HTML references, including GitHub's README."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    failures = []
    references = 0
    for path in [
        root / "README.md",
        root / "CONTRIBUTING.md",
        root / "CHANGELOG.md",
        *sorted((root / "docs").rglob("*.md")),
        *sorted((root / "docs/assets").rglob("*.html")),
    ]:
        source = path.read_text()
        if "{{source_walkthrough}}" in source:
            from render_source_examples import cases, markdown

            key = next(key for key, case in cases().items() if case["page"] == path.name)
            source = source.replace("{{source_walkthrough}}", markdown(key))
            source = source.replace('src="../assets/', 'src="assets/')
        elif path.name == "examples-region.md":
            from render_source_examples import tabs

            source = source.replace("{{example_tabs}}", tabs("region"))
            source = source.replace('src="../assets/', 'src="assets/')
        source = re.sub(r"```.*?```", "", source, flags=re.S)
        targets = re.findall(r"!?\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", source)
        targets += re.findall(r'(?:src|href)="([^"]+)"', source)
        for target in targets:
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path or "{{" in parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            # HTML compatibility URL points into the built site, not source Markdown.
            if path.suffix == ".html" and resolved == root / "docs/examples":
                resolved = root / "docs/examples.md"
            if not resolved.exists():
                failures.append(f"{path.relative_to(root)}: {target}")
            references += 1
    if failures:
        raise ValueError("Unresolved local references:\n" + "\n".join(failures))
    return {"local_references_checked": references}


if __name__ == "__main__":
    print(check())
