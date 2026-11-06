"""Arithmetic visibility over an explicitly counted comparison population."""

from numbers import Integral


def comparison_mean(clear: int, blocked: int, unknown: int) -> dict:
    """Return complete/known-only means and bounds; empty populations stay null."""
    counts = (clear, blocked, unknown)
    if any(isinstance(v, bool) or not isinstance(v, Integral) or v < 0 for v in counts):
        raise ValueError("Comparison counts must be nonnegative integers")
    clear, blocked, unknown = map(int, counts)
    total = clear + blocked + unknown
    known = clear + blocked
    return {
        "total": total,
        "known": known,
        "lower": clear / total if total else None,
        "upper": (clear + unknown) / total if total else None,
        "complete": clear / total if total and not unknown else None,
        "known_only": clear / known if known else None,
    }
