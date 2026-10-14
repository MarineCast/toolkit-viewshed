"""Score provisioned sources using a completed immutable prepared tile store."""

from __future__ import annotations

import sys

from viewshed_toolkit.pipeline.cli.main import main

if __name__ == "__main__":
    main(["transmission-tiles", "--stage", "score", *sys.argv[1:]])
