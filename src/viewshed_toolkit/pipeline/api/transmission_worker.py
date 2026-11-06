"""Private installed-package subprocess entry point for one source-worker shard."""

import json
import sys
from pathlib import Path

from ..config.transmission import load_transmission_config
from ..contracts.transmission import implementation_identity, runtime_identity
from ..weights.transmission_worker import worker

if __name__ == "__main__":
    config = load_transmission_config(sys.argv[1])
    receipt = json.loads((Path(config.output_dir) / "preparation-manifest.json").read_text())
    if (
        receipt["code_hashes"] != implementation_identity()
        or receipt["runtime"] != runtime_identity()
    ):
        raise ValueError("Worker implementation/runtime differs from preparation")
    index = int(sys.argv[2])
    if not 0 <= index < config.workers:
        raise ValueError("Worker index outside configured shard count")
    worker(config.runtime(), index)
