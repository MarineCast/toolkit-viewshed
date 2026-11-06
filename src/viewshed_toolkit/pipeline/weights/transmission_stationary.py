"""One-pass qualification of compact canonical obstruction rasters."""

import math
import time
from typing import Any

import numpy as np

from .transmission_fast_kernels import _native
from .vegetation.classified import PreparedClassifiedCanopy, _compact_class_tables


@_native
def _qualify(
    building: Any,
    building_unknown: Any,
    present: Any,
    water: Any,
    canopy: Any,
    canopy_unknown: Any,
    classes: Any,
    coefficients: Any,
    missing_classes: Any,
    zero_water: Any,
    outputs: Any,
) -> None:
    h, w = canopy.shape
    b, bu, ch, unk, coef, mc, mh = outputs
    for r in range(h):
        for c in range(w):
            height = building[r, c]
            if not math.isfinite(height) or height < 0:
                raise ValueError("Finite nonnegative component lower heights required")
            conflict = water[r, c] and present[r, c]
            clear = water[r, c] and not conflict
            b[r, c] = 0.0 if water[r, c] else height
            bu[r, c] = True if conflict else False if clear else building_unknown[r, c]
            ch[r, c] = 0 if clear else canopy[r, c]
            unk[r, c] = canopy_unknown[r, c] and not clear
            code = classes[r, c]
            coef[r, c] = coefficients[ch[r, c], code]
            mc[r, c] = missing_classes[ch[r, c], code]
            mh[r, c] = unk[r, c] and not (zero_water and code == 80 and ch[r, c] == 0)


def stationary_compact(data: Any, classes: Any, policy: Any) -> Any:
    """Return None outside the exact canonical dtype envelope."""
    shape = data["ground"].shape
    names = ("building_unknown", "building_present", "qualified_water", "canopy_unknown")
    if (
        data["ground"].dtype != np.float32
        or data["building"].dtype != np.float32
        or data["canopy"].dtype != np.uint8
        or classes.dtype != np.uint8
        or len(shape) != 2
        or not all(shape)
        or any(data[k].dtype != np.bool_ for k in names)
        or any(data[k].shape != shape for k in (*names, "building", "canopy"))
        or classes.shape != shape
        or policy not in ("zero_if_missing", "retain_unknown")
    ):
        return None
    # Preserve NumPy's platform-specific signed-zero maximum behavior via the
    # generic implementation. Negative heights are validated there as usual.
    if np.signbit(data["building"]).any():
        return None
    started = time.perf_counter()
    outputs = tuple(
        np.empty(shape, dtype=dtype)
        for dtype in (np.float32, bool, np.uint8, bool, np.float64, bool, bool)
    )
    tables = _compact_class_tables()
    _qualify(
        data["building"],
        data["building_unknown"],
        data["building_present"],
        data["qualified_water"],
        data["canopy"],
        data["canopy_unknown"],
        classes,
        tables[0],
        tables[1],
        policy == "zero_if_missing",
        outputs,
    )
    b, bu, ch, unk, coef, mc, mh = outputs
    lc = np.array(classes, copy=True)
    zero = np.zeros(shape, bool)
    for a in (b, bu, ch, unk, coef, mc, mh, lc, zero):
        a.flags.writeable = False
    return (
        b,
        bu,
        PreparedClassifiedCanopy(
            ch, lc, unk, coef, mc, mh, policy, zero, time.perf_counter() - started
        ),
    )
