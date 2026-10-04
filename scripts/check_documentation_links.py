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
        root / "docs/assets/san-juan-demo.html",
    ]:
        source = path.read_text()
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
