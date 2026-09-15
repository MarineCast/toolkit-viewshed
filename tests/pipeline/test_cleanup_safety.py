from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml

from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit.pipeline.config import (
    load_yaml,
    stable_config_hash,
    write_metadata_sidecar,
)
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
from viewshed_toolkit.pipeline.contracts.cleanup import cleanup_data_contract
from viewshed_toolkit.pipeline.finalize import cleanup as finalize_cleanup
from viewshed_toolkit.pipeline.finalize.final_artifacts import (
    _effective_physical_assumptions,
    _static_input_paths,
    cleanup_viewshed_dir_to_static_outputs,
    static_scientific_config_hash,
    validate_static_artifact_metadata,
)


def _config(tmp_path: Path) -> Path:
    config_path = tmp_path / "viewshed.yaml"
    output_dir = tmp_path / "processed" / "domain" / "human" / "viewshed"
    config_path.write_text(
        yaml.safe_dump(
            {
                "h3": {"source_resolution": 7, "target_resolution": 7},
                "paths": {"output_dir": str(output_dir)},
            }
        )
    )
    return config_path


def _split_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "viewshed-split.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "h3": {"source_resolution": 7, "target_resolution": 7},
                "paths": {
                    "output_dir": str(tmp_path / "tmp" / "viewshed" / "h3r7"),
                    "final_output_dir": str(tmp_path / "processed" / "viewshed" / "RES7"),
                },
            }
        )
    )
    return config_path


def _write_valid_static_outputs(config_path: Path) -> None:
    raw, _config_dir = load_yaml(config_path)
    paths = final_artifact_paths(config_path)
    frame = pl.DataFrame(
        {
            "source_h3": ["s"],
            "target_h3": ["t"],
            "weight_terrain": [0.0],
            "weight_distance": [1.0],
            "weight_vegetation": [1.0],
            "weight_static_viewability": [0.0],
        }
    )
    for source_type, output in (
        ("land", paths.land_static_weights),
        ("water", paths.water_static_weights),
    ):
        inputs = _static_input_paths(paths, source_type)
        for name, input_path in inputs.items():
            input_path.parent.mkdir(parents=True, exist_ok=True)
            if not input_path.exists():
                input_path.write_text(f"{source_type}:{name}")
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(output)
        assumptions = _effective_physical_assumptions(raw, source_type=source_type)
        write_metadata_sidecar(
            output,
            raw,
            {
                "artifact_kind": "human.viewshed.static_pair_kernel",
                "schema_version": "viewshed_static_pair_v2",
                "status": "complete",
                "source_type": source_type,
                "config_hash": stable_config_hash(raw),
                "scientific_config_hash": static_scientific_config_hash(raw),
                "dem": assumptions,
                "h3": assumptions["h3"],
                "physical_assumptions": assumptions,
                "input_paths": {name: str(path) for name, path in inputs.items()},
                "input_checksums": {name: checksum_path(path) for name, path in inputs.items()},
                "input_retention_policy": ("ephemeral_inputs_may_be_pruned_after_final_validation"),
                "artifact_checksum": checksum_path(output),
            },
        )


def test_cleanup_data_contract_never_removes_sibling_domain_data(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    paths = final_artifact_paths(config_path)
    sibling = paths.output_dir.parent / "population" / "population.parquet"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("must survive")

    paths.land_static_weights.parent.mkdir(parents=True)
    paths.land_static_weights.write_text("final")
    scratch = paths.output_dir / "_tmp" / "terrain" / "scratch.tif"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("scratch")
    preserved_dir = paths.output_dir / "retained_notes"
    preserved_child = preserved_dir / "notes.txt"
    preserved_dir.mkdir()
    preserved_child.write_text("notes")

    removed = cleanup_data_contract(
        config_path,
        remove_stage_scratch=True,
        preserve_paths=[preserved_dir],
    )

    assert sibling.read_text() == "must survive"
    assert paths.land_static_weights.exists()
    assert preserved_child.exists()
    assert not scratch.exists()
    assert scratch in removed


def test_static_cleanup_preserves_outputs_and_sidecars_only_inside_root(
    tmp_path: Path,
) -> None:
    config_path = _config(tmp_path)
    paths = final_artifact_paths(config_path)
    sibling = paths.output_dir.parent / "daylight" / "daily.parquet"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("must survive")

    _write_valid_static_outputs(config_path)
    scratch = paths.output_dir / "lookup" / "lookup.parquet"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("scratch")

    cleanup_viewshed_dir_to_static_outputs(config_path)

    assert sibling.read_text() == "must survive"
    assert not scratch.exists()
    for output in (paths.land_static_weights, paths.water_static_weights):
        assert output.exists()
        assert output.with_name(f"{output.stem}_metadata.json").exists()
    raw, _ = load_yaml(config_path)
    validate_static_artifact_metadata(paths.land_static_weights, raw=raw, source_type="land")
    validate_static_artifact_metadata(paths.water_static_weights, raw=raw, source_type="water")


def test_static_cleanup_removes_separate_working_directory(tmp_path: Path) -> None:
    config_path = _split_config(tmp_path)
    paths = final_artifact_paths(config_path)
    _write_valid_static_outputs(config_path)
    scratch = paths.output_dir / "lookup" / "lookup.parquet"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("scratch")

    cleanup_viewshed_dir_to_static_outputs(config_path)

    assert not paths.output_dir.exists()
    assert sorted(path.name for path in paths.final_output_dir.iterdir()) == [
        "LAND_STATIC_WEIGHTS_R7.parquet",
        "LAND_STATIC_WEIGHTS_R7_metadata.json",
        "WATER_STATIC_WEIGHTS_R7.parquet",
        "WATER_STATIC_WEIGHTS_R7_metadata.json",
    ]


def test_finalize_cleanup_is_opt_in(tmp_path: Path, monkeypatch) -> None:
    config_path = _config(tmp_path)
    paths = final_artifact_paths(config_path)
    calls: list[Path] = []

    monkeypatch.setattr(
        finalize_cleanup,
        "materialize_static_viewability_outputs",
        lambda *_args, **_kwargs: {
            "land_static_weights": paths.land_static_weights,
            "water_static_weights": paths.water_static_weights,
        },
    )
    monkeypatch.setattr(
        finalize_cleanup,
        "cleanup_viewshed_dir_to_static_outputs",
        lambda path: calls.append(Path(path)) or [],
    )

    finalize_cleanup.main(["--config", str(config_path)])
    assert calls == []

    finalize_cleanup.main(["--config", str(config_path), "--clean-intermediates"])
    assert calls == [config_path.resolve()]


def test_intermediate_cleanup_preserves_only_requested_final_products(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.finalize import cleanup

    final = tmp_path / "final.parquet"
    final.write_text("final")
    captured = {}

    def capture(config_path, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cleanup, "cleanup_data_contract", capture)
    assert cleanup.clean_intermediates(tmp_path / "config.yaml", {"land": final}) == []
    assert captured["preserve_paths"] == [final]
    assert captured["remove_stage_scratch"] is True
