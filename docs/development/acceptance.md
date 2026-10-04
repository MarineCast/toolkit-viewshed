# Scientific and release acceptance

A flagship repository should let a reader distinguish **what passed**, **what was measured**, and
**what is still unknown**. Link evidence for the exact code and data revision being released.
Historical reports are not certification of a later checkout.

## Evidence ladder

| Evidence | Current scope | Requirement for a new release |
| --- | --- | --- |
| Unit and synthetic integration | Native kernel, lineage, rollback, API, CLI and schema regression tests | Pass the suite and targeted regressions on the release commit; record skips and environment |
| Wheel and first result | Installed package/resources/CLI and offline synthetic distance example | Record wheel identity, native versions and first-result receipt |
| Documentation | Strict build, checked instructional bundle, Chromium/WebKit automation | Publish the same site artifact that passed browser QA |
| Real bounded rebuild | Historical San Juan evidence linked below | Rerun only when required by changed science/inputs; preserve producer and generation receipts |
| Regional coverage | Separate Salish Sea evidence and source limitations | Verify required coverage and inputs for the actual region; do not extrapolate from the demo |
| Sensitivity | Sampling/resolution/clearance robustness remains unperformed | Agree on questions, settings and acceptance criteria before running |
| Human reader study | Not performed by browser automation | Ask an independent reader to complete the lesson and explain value/state/provenance correctly |
| Field reference comparison | Not established | Use an independent measured reference protocol before making empirical accuracy claims |

The [reliability improvement report](../reports/reliability-improvements.md) records this pass.
The [evidence index](evidence.md) links dated reports. Software version, schema version, source
vintage and generation identity answer different questions; retain all four.

## Bounded sensitivity protocol

Use a new configuration and dedicated input/output directory for each scenario, retaining the
baseline and immutable receipts. Keep this outside routine CI and do not acquire large regional
data as part of a documentation build.

Before execution, record the domain and candidate pair set, input bytes, baseline configuration,
parameter values to vary, numerical comparison tolerance and the decision each comparison informs.
Vary one assumption at a time first: analysis resolution, observer sampling, canopy clearance,
missing-canopy treatment and observer/target heights. Record any changed pair population separately
from changes on the common evaluated pairs.

Report absolute/relative value changes, changed support states, positive/zero transitions, source
or target rank changes, source coverage, native versions, runtime and memory. Preserve raw outcomes,
including failures. A numerical regression tolerance is not a universal scientific acceptance
threshold; justify application-specific thresholds before inspecting results.

Known-geometry fixtures should have independently derivable visibility, not merely match a previous
implementation. Existing algebraic/kernel fixtures remain useful regression evidence. A new field
study needs measured positions, heights, terrain/canopy reference and a documented uncertainty model.
Neither synthetic geometry nor real raster inputs by themselves establish field accuracy.

## Release checklist

1. Resolve correctness findings and document supported/legacy interfaces and compatibility changes.
2. Run applicable CI and save evidence linked to the exact commit; update `CHANGELOG.md`.
3. Record the environment (`python -m pip freeze`, `conda list --explicit` when applicable), input
   identity and native versions. Do not commit credentials or machine-private paths.
4. Inspect source rights, units, vertical reference, missingness and regional coverage. Publish only
   permitted derivatives. See the [San Juan dataset card](../dataset-card.md).
5. Reconcile documentation examples, public signatures and artifact schemas. Review fallback,
   keyboard, mobile and dark presentation; record human review separately.
6. Assign a release version/tag deliberately, update citation metadata when publishing, and publish
   only the validated artifacts. No package publication or repository merge is implied by this list.
