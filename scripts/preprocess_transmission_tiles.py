"""Supported persistent tile workflow script; defaults to read-only planning."""

from __future__ import annotations

import sys

from viewshed_toolkit.pipeline.cli.main import main

if __name__ == "__main__":
    main(["transmission-tiles", *sys.argv[1:]])
