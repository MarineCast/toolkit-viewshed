"""Combined current-store and scoring-output byte reservations; no cleanup ownership."""

from pathlib import Path
from typing import Any


def reserve_tile_workflow_bytes(config: Any, reserve: int) -> int:
    if type(reserve) is not int or reserve < 0:
        raise ValueError("Nonnegative storage reservation required")
    total = 0
    for root in [Path(config.store_dir), Path(config.score_dir)]:
        if root.is_symlink():
            raise ValueError("Unsafe workflow output root")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Unsafe workflow artifact")
            if path.is_file():
                total += path.stat().st_size
    if total + reserve > config.max_store_bytes:
        raise ValueError(
            "Combined prepared store and score byte budget would be exceeded; stop and reestimate"
        )
    return total
