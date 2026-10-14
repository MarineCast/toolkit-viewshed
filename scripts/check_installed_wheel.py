"""Build a wheel and verify its installation outside the source checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="viewshed-wheel-") as directory:
        scratch = Path(directory)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "VIEWSHED_TOOLKIT_ROOT"}
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(scratch),
                str(ROOT),
            ],
            check=True,
            env=environment,
        )
        wheel = next(scratch.glob("*.whl"))
        venv = scratch / "installed"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
            env=environment,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
            check=True,
            cwd=scratch,
            env=environment,
        )
        probe = """
import json, sys
from importlib import resources as package_resources
from pathlib import Path
import viewshed_toolkit
from viewshed_toolkit.resources import common_areas_path, default_config_path
package = Path(viewshed_toolkit.__file__).resolve()
assert package.is_relative_to(Path(sys.prefix).resolve()), package
resources = [common_areas_path(), default_config_path()]
assert all(p.is_file() and p.resolve().is_relative_to(Path(sys.prefix).resolve()) for p in resources), resources
bundled = package_resources.files('viewshed_toolkit.resources')
for name in ['study.schema.json', 'registry-artifact-interface.v1.json', 'native_warp_plan.cpp', 'comparison_map.html', 'raster_overlay.js']:
    assert bundled.joinpath(name).is_file(), name
assert callable(viewshed_toolkit.run_transmission)
from affine import Affine
import numpy as np
from viewshed_toolkit.pipeline.weights.transmission import prepare_calculation, calculate_prepared
from viewshed_toolkit.pipeline.config.transmission_tiles import TransmissionTileConfig
assert TransmissionTileConfig.model_fields['calculation_mode'].default == 'exact_fast'
shape = (8, 16)
zero = np.zeros(shape, np.float32)
mask = np.zeros(shape, bool)
data = dict(ground=zero,canopy=np.zeros(shape,np.uint8),canopy_unknown=mask,
            building=zero,building_unknown=mask,building_present=mask,
            qualified_water=mask,transform=Affine(10,0,0,0,-10,80))
prepared = prepare_calculation(data,np.full(shape,30,np.uint8))
args = (prepared,np.array([[15.,65.,0.]]),np.array([[145.,15.]]),
        np.array([0]),np.array([1]),['target'],5000.)
reference,_ = calculate_prepared(*args,calculation_mode='reference')
fast,_ = calculate_prepared(*args,calculation_mode='exact_fast',observer_workers=8,target_chunks=4)
for key in ['lower','upper','unknown','out_of_range']:
    assert reference[key].tobytes() == fast[key].tobytes()
assert reference['score'] == fast['score']

assert callable(viewshed_toolkit.run_transmission_tiles)
assert callable(viewshed_toolkit.load_transmission_tile_config)
assert callable(viewshed_toolkit.validate_transmission_product)
assert bundled.joinpath("transmission.schema.json").is_file()
from viewshed_toolkit.pipeline.weights.transmission import calculate
from viewshed_toolkit.pipeline.config.distance import DistanceWeightConfig
import numpy as np
from affine import Affine
shape = (3, 12)
data = {key: np.zeros(shape, dtype=bool if key in {"qualified_water", "building_present"} or "unknown" in key else "float32") for key in ["ground", "canopy", "canopy_unknown", "building", "building_unknown", "building_present", "qualified_water"]}
data["transform"] = Affine(10,0,0,0,-10,30)
result, _ = calculate(data,np.full(shape,30,dtype="uint8"),np.array([[15.,15.,2.]]),np.array([[95.,15.]]),np.array([0]),np.ones(1),np.array(["fixture"]),120.,distance_config=DistanceWeightConfig())
assert result["score"] == [1.]
assert result["diagnostics"][0]["weight_above_ground"] == 1.
from tempfile import TemporaryDirectory
from viewshed_toolkit.pipeline.contracts.transmission import DTYPES
from viewshed_toolkit.pipeline.prepare.elevation.transmission_store import PreparedTileWriter, PreparedTileStore
with TemporaryDirectory() as folder:
    root=Path(folder)/"tiles"
    fields={key:np.zeros((384,384),dtype=dtype) for key,dtype in DTYPES.items()}
    with PreparedTileWriter(root,{"installed_fixture":True},Affine(10,0,0,0,-10,3840),(384,384),[(0,0)],max_bytes=10*1024**2) as writer:
        writer.write_tile(0,0,fields)
        writer.complete()
    tile=PreparedTileStore(root).tile(0,0)
    assert tile.ground.dims==("y","x")
    assert tile.ground.dtype==np.float32 and tile.canopy_unknown.dtype==np.bool_
    assert not tile.ground.data.flags.writeable

assert callable(viewshed_toolkit.export_comparison_maps)
assert callable(viewshed_toolkit.export_pilot_map)
assert bundled.joinpath("pilot_map.js").is_file()
assert bundled.joinpath("pilot_map.html").is_file()
assert callable(viewshed_toolkit.export_raster_overlay_asset)
assert "rasterPixel" in bundled.joinpath("raster_overlay.js").read_text()
from viewshed_toolkit.pipeline.prepare.elevation.warp_planner import planner_source_sha256
assert len(planner_source_sha256()) == 64
print(json.dumps({'package': str(package), 'resources': [p.name for p in resources]}))
"""
        result = subprocess.run(
            [str(python), "-c", probe],
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            print(result.stdout, result.stderr, file=sys.stderr)
        result.check_returncode()
        command = venv / (
            "Scripts/viewshed-toolkit.exe" if os.name == "nt" else "bin/viewshed-toolkit"
        )
        help_result = subprocess.run(
            [str(command), "--help"],
            check=True,
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert "usage:" in help_result.stdout.lower()
        transmission_help = subprocess.run(
            [str(command), "transmission", "--help"],
            check=True,
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert "--stage" in transmission_help.stdout and "--config" in transmission_help.stdout
        tile_help = subprocess.run(
            [str(command), "transmission-tiles", "--help"],
            check=True,
            cwd=scratch,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert "recover-store" in tile_help.stdout and "--resume" in tile_help.stdout
        print(
            json.dumps(
                {
                    "status": "passed",
                    "python": sys.version.split()[0],
                    "wheel": wheel.name,
                    "installed": json.loads(result.stdout),
                    "cli_help": "passed",
                    "dependencies": "reused system site packages; toolkit itself installed from the wheel",
                }
            )
        )


if __name__ == "__main__":
    main()
