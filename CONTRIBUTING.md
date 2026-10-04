# Contributing

Start with the [development guide](docs/development/index.md). Install the
[geospatial environment](docs/getting-started/installation.md), run the
[offline first result](docs/getting-started/quick-start.md), and use a separate branch/worktree.

For a fix, provide a small synthetic regression through the public API/CLI boundary. Run focused
pytest tests, then the full suite for code changes; run Ruff, changed-file Black, compileall,
and `scripts/check_components.py` for its covered components. Documentation changes require the
bundle check, local links and strict MkDocs; interactive changes also require browser checks.
The development guide links the exact commands and scientific safety rules.

Describe the trigger, resulting behavior, compatibility impact, tests actually run and remaining
limitations in the pull request. Keep formulas and missingness semantics explicit. Do not check
in generated model products or change scientific receipts to manufacture evidence. Small private
helper cleanup is welcome; public/advanced API removal needs a documented compatibility decision.

Report bugs with commit, command, redacted configuration, `doctor` output and minimal inputs.
For scientific concerns, distinguish a reproducible software defect, an assumption to test, and
an empirical validation request. Propose new data/features only with clear ownership and provenance.

Releases require the [acceptance checklist](docs/development/acceptance.md), change notes and
matching code/docs evidence. This repository has no automatic package release process configured
by these instructions. Agent-specific guidance remains in [AGENTS.md](AGENTS.md).
