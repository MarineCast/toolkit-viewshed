"""Create a strict persistent-store request from an existing pinned regional recipe; no run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig
from viewshed_toolkit.pipeline.contracts.transmission import digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regional-config", type=Path, required=True)
    parser.add_argument("--store-dir", type=Path, required=True)
    parser.add_argument("--score-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    region = json.loads(args.regional_config.read_text())

    def asset(name: str) -> dict[str, str]:
        path = (args.regional_config.resolve().parent / region[name]).resolve()
        expected = region.get("pins", {}).get(str(path))
        sha = digest(path)
        if expected is not None and expected != sha:
            raise ValueError("Existing recipe input pin changed")
        return dict(path=str(path), sha256=sha)

    payload = dict(
        schema_version=1,
        method=region["method"],
        store_dir=str(args.store_dir.resolve()),
        score_dir=str(args.score_dir.resolve()),
        provisioned_metadata=asset("provisioned_metadata"),
        source_inventory=asset("source_inventory"),
        reporting_water=asset("reporting_water_wkb"),
        global_transform=region["global_transform"],
        global_shape=region["global_shape"],
        source_policy=region["source_policy"],
        max_distance_m=region["max_distance_m"],
        source_samples=region["source_samples"],
        target_stride=region["target_stride"],
        census_sources=region["census_sources"],
        census_threshold=region["census_threshold"],
        maximum_sampling_error=region["maximum_sampling_error"],
        p95_sampling_error=region["p95_sampling_error"],
        distance_weight=region["distance_weight"],
    )
    validated = TransmissionTileConfig.model_validate_json(json.dumps(payload))
    with args.output.open("x") as handle:
        handle.write(json.dumps(validated.runtime(), indent=2))
    print("Request saved; no preprocessing or weights were executed.")


if __name__ == "__main__":
    main()
