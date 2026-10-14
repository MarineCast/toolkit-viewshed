"""Supervised preparation child; preserves partial tiles if a resource guard stops it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .transmission_tiles import run_tile_preparation

if __name__ == "__main__":
    result = run_tile_preparation(sys.argv[1], stage="prepare", resume=sys.argv[3] == "resume")
    Path(sys.argv[2]).write_text(json.dumps(result))
