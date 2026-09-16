---
name: scientific-change
description: Preserve viewshed numerical and scientific contracts when changing LOS, terrain, canopy, distance, composition, sampling, or missingness semantics.
---

# scientific-change

All paths and commands below are relative to the toolkit-viewshed checkout root.
Source/tests, explicit contracts and architecture remain authoritative.

**Before changing numerical behavior, read `docs/scientific-methodology.md`.** Distance attenuation
is already integrated into `weight_terrain`; do not multiply the H3-centroid distance diagnostic
into the static result. Preserve the distinction between physical viewability and observer
activity, access, detection, presence, or ecology. Missing, unavailable, not-applicable, and
observed zero are distinct states.

Distance products are independently executable. Changing a standalone distance profile must not
trigger terrain or canopy computation or alter their scientific configuration. Independently
computed H3-level averages are not generally interchangeable with joint observer/target-sample
averages. The current viewshed kernel already integrates distance attenuation; never multiply a
standalone centroid-distance weight into it again.

Read `docs/methodology.md` when broader methodological context is needed.
Run invariant and edge-case tests through the validation skill for numerical changes.
