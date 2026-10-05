# Contributing and validation

Start with [CONTRIBUTING](../../CONTRIBUTING.md) for the human contribution workflow,
[change history](../../CHANGELOG.md), and [acceptance checklist](acceptance.md).
Work from the repository root and read [AGENTS.md](../../AGENTS.md) for the local scientific,
artifact and validation rules. Keep generated source data and model products out of routine
documentation changes.

## Documentation development

Install the documentation dependencies separately from the native model environment:

```bash
python -m pip install -r requirements-docs.txt
python -S scripts/build_documentation_examples.py --check
python scripts/check_documentation_links.py
python -m mkdocs build --strict
git diff --check
```

The site build checks the committed bundle and inserts its verified values into the guided
example. It does not download inputs or recalculate LOS. For browser checks and bundle export,
see [reproducing the instructional bundle](../documentation-examples.md).

## Design and organization

The documentation follows the reader journeys used by
[Seascape Toolkit](https://marinecast.github.io/toolkit-seascape/): Home, Get started, Understand,
Products, Examples, API, Reference and Development. Short entry guides link to the existing
canonical pages; scientific formulas and lifecycle contracts remain in their owners.

Use the shared teal Material theme, full-width banner, entry cards, clear units and explicit
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
