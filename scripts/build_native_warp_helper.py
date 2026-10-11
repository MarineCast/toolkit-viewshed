"""Explicit local build of the optional metadata-only GDAL chunk planner."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path


def build(source: Path, output: Path, gdal_config: Path) -> dict[str, str]:
    if output.exists():
        raise FileExistsError(f"Preserve the existing helper and select a new path: {output}")
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        raise RuntimeError("A C++17 compiler and matching GDAL development headers are required")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    flags = []
    for option in ("--cflags", "--libs"):
        flags += shlex.split(subprocess.check_output([str(gdal_config), option], text=True))
    library_dirs = [flag[2:] for flag in flags if flag.startswith("-L")]
    for directory in library_dirs:
        flags += ["-Xlinker", "-rpath", "-Xlinker", directory]
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f'-DVIEWSHED_PLANNER_SOURCE_SHA256="{source_sha}"',
            str(source),
            *flags,
            "-o",
            str(output),
        ],
        check=True,
    )
    info = json.loads(subprocess.check_output([str(output.resolve()), "--info"], text=True))
    if info["source_code_sha256"] != source_sha:
        raise ValueError("Compiled helper source identity mismatch")
    info["binary_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    return info


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gdal-config", required=True, type=Path)
    args = parser.parse_args()
    source = (
        Path(__file__).resolve().parents[1] / "src/viewshed_toolkit/resources/native_warp_plan.cpp"
    )
    print(json.dumps(build(source, args.output, args.gdal_config), indent=2))
