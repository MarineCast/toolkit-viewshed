# Contributing and validation

Start with [CONTRIBUTING](../../CONTRIBUTING.md) for the human contribution workflow,
[change history](../../CHANGELOG.md), and [acceptance checklist](acceptance.md).
Work from the repository root and read [AGENTS.md](../../AGENTS.md) for the local scientific,
artifact and validation rules. Keep generated source data and model products out of routine
documentation changes.

## Build the documentation

Install the documentation dependencies separately from the native model environment:

```bash
python -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r requirements-docs.txt
python -S scripts/build_documentation_examples.py --check
python scripts/check_documentation_links.py
python -m mkdocs build --strict
git diff --check
```

The site build checks the committed bundle and inserts its verified values into the guided
example. It does not download inputs or recalculate LOS. For browser checks and bundle export,
see [reproducing the instructional bundle](../documentation-examples.md).

The GitHub documentation jobs install Chromium and WebKit with their system dependencies.
On runners that use `/etc/apt/apt-mirrors.txt`, they select the official Ubuntu archive
over HTTPS before installation to avoid the slow Azure mirror downloads observed on
November 6, 2026. Package signature verification and all browser checks remain enabled.

## Design and organization

The reading route is Overview → Lime Kiln walkthrough → first runnable result → Outputs.
How it works introduces sightlines, canopy, distance and practical uses. Exact methods, APIs
and development guides remain under Reference. Published compatibility URLs are retained.

Use the shared teal Material theme, full-width banner, concise entry points, clear units and explicit
interpretation boundaries. The banner is decorative artwork. Preserve its full aspect ratio;
maps and figures need captions that distinguish illustration from modeled evidence.

Retain existing published page URLs when reorganizing navigation. In particular, `examples.md`
owns `/examples/` and `api.md` owns `/api/`; do not add competing `index.md` files at those paths.
Check light/dark presentation, mobile reflow, keyboard access and the static visual walkthrough after a
navigation or styling change.

## Code and scientific changes

The [architecture guide](../architecture.md) describes implementation ownership. Follow the
repository's matching local skill before changing science, data contracts or pipeline lifecycle.
Choose focused tests appropriate to the changed behavior. A documentation-only change does not
require a regional run or the package suite.

[Validation evidence](evidence.md) records historical checks and their limitations. A dated
report is evidence of its stated run, not certification of the current checkout or a new release.


Use `python -m mkdocs serve` for local development. The [Lime Kiln revision checks](lime-kiln-validation.md)
record the November 6 validation scope.
