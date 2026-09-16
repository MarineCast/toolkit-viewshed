---
name: pipeline-safety
description: Preserve viewshed workflow safety when changing orchestration, stage dependencies, cleanup, overwrite, promotion, or rollback.
---

# pipeline-safety

All paths and commands below are relative to the toolkit-viewshed checkout root.
Source/tests, explicit contracts and architecture remain authoritative.

**Before changing orchestration, promotion, overwrite, or cleanup, identify the affected workflow.**
The established paired workflow (`run_viewshed` / `process`) and explicit component workflow
(`run_components` / `run_component_stage`) have different output, promotion, and cleanup
contracts. Read `docs/api.md` and `docs/pipelines.md` before changing either.

## Cleanup and overwrite safety

Cleanup and overwrite behavior is part of the public data contract.

Do not transfer cleanup or promotion behavior between workflows. Successful complete execution in
the established paired workflow may clean intermediates and uses rollback-safe paired land/water
promotion. The explicit component workflow writes its separate component namespace, performs no
implicit cleanup, and uses per-role manifests rather than a combined atomic generation pointer.
For water builds, read the dependency caveat beside the examples in `docs/pipelines.md`; an
individual opaque-land stage can be raster-free even when a broader dependency-resolved build is
not.

- Never broaden a cleanup root or bypass containment checks.
- Never clean the repository root, current working directory, or a path outside the configured
  viewshed output directory.
- Require durable outputs and their schema/metadata validation before deleting intermediates.
- Preserve final artifacts and all supported metadata sidecar names.
- Keep paired land/water promotion rollback-safe: a failure must not leave one new artifact paired
  with one stale artifact.
- Use a new run ID or explicit force/resume behavior rather than overwriting a manifest for a
  different logical run.
- Add or update cleanup and rollback tests whenever deletion, promotion, manifest, or overwrite
  behavior changes.
