"""User-facing first-result and read-only diagnostics checks."""

import importlib.util
from pathlib import Path

import pytest

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit.pipeline.api.preflight import inspect_environment


def test_offline_example_uses_validated_products_and_preserves_existing_directory(tmp_path):
    spec = importlib.util.spec_from_file_location("offline_example", "examples/offline_distance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "first-result"
    result = module.run(output)
    assert result["valid"] and result["rows"] > 0
    assert Path(result["pairs"]).is_relative_to(output)
    assert not list(output.rglob("*.tif"))
    before = (output / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        module.run(output)
    assert (output / "result.json").read_bytes() == before


def test_doctor_is_read_only_and_distinguishes_workflow_inputs(tmp_path):
    config = coastal_fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    land = inspect_environment(config)
    assert not land["valid"]
    assert set(land["missing_inputs"]) == {"DEM", "CHM"}
    assert inspect_environment(config, workflow="distance")["valid"]
    assert inspect_environment(config, workflow="water")["valid"]
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
