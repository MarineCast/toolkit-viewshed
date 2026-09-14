# Validation status

## Current source validation

The source-organization and shim-removal work passed:

- 202 tests with the ordinary `PYTHONPATH=src` layout;
- a full Pyflakes scan and Python byte-compilation check;
- Black checks for the changed Python files and `git diff --check`;
- wheel construction and archive inspection;
- package-root and canonical imports from the built wheel;
- packaged Salish Sea configuration loading from the built wheel; and
- `python -m viewshed_toolkit --help` from the built wheel.

The wheel inspection confirmed that the removed top-level pass-through modules and internal
`persistence.py` shim are not packaged.

These checks do not replace a full regional pipeline run. The notebooks were not executed, and
the terrain pipeline was not rebuilt because the required DEM, canopy, land, water, and downstream
OrcaCast inputs are not all present.

## Checked-in migration snapshot

[`viewshed_migration_validation.json`](viewshed_migration_validation.json) records an earlier
artifact-level inspection. It checks:

- land and water pair-key uniqueness and nullness;
- finite static weights bounded to `[0, 1]`;
- artifact checksum agreement with metadata;
- configuration and scientific-hash alignment;
- notebook JSON integrity and absence of embedded error outputs; and
- expected file size and SHA-256 from the portable artifact manifest.

The two primary kernels pass their recorded checks: 2,081,219 land pairs and 3,208,581 water
pairs. The report's overall `valid` value is nevertheless `false`: its portable-manifest section
records 52 missing map-support paths under `outputs/effort/viewshed`, and its map-output inventory
contains zero files. Copies under `data/outputs/effort/viewshed` do not satisfy those canonical
paths.

The script that produced this JSON report is not present in the current checkout, so the snapshot
was inspected but not regenerated. Restore or reimplement the validator before treating the report
as a repeatable current gate.
