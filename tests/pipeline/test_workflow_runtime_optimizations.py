"""Bounded fan-out, cache retention and unchanged-file validation contracts."""

import os
from pathlib import Path

import polars as pl
import pytest
import yaml

from tests.pipeline.test_cleanup_safety import _config, _write_valid_static_outputs
from viewshed_toolkit._internal.data.parquet import atomic_project_parquet
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths
from viewshed_toolkit.pipeline.finalize.final_artifacts import (
    cleanup_viewshed_dir_to_static_outputs,
    validate_static_artifact_metadata,
)


@pytest.mark.parametrize("rows", [0, 7])
def test_projection_fanout_visits_each_row_once_and_preserves_types(tmp_path, rows):
    source = tmp_path / "source.parquet"
    frame = pl.DataFrame(
        {"id": range(rows), "weight": [0.25] * rows, "state": [None] * rows},
        schema={"id": pl.Int64, "weight": pl.Float32, "state": pl.String},
    )
    frame.write_parquet(source)
    outputs = {tmp_path / "a.parquet": ["id", "weight"], tmp_path / "b.parquet": ["id", "state"]}
    seen = []
    assert (
        atomic_project_parquet(
            source, outputs, batch_size=2, batch_observer=lambda batch: seen.extend(batch["id"])
        )
        == rows
    )
    assert seen == list(range(rows))
    for path, columns in outputs.items():
        assert pl.read_parquet(path).equals(frame.select(columns))


def test_projection_failure_preserves_previous_outputs(tmp_path):
    source = tmp_path / "source.parquet"
    pl.DataFrame({"id": [1, 2, 3]}).write_parquet(source)
    outputs = {tmp_path / "a.parquet": ["id"], tmp_path / "b.parquet": ["id"]}
    for path in outputs:
        path.write_bytes(b"previous")

    def fail(_batch):
        raise RuntimeError("observer failed")

    with pytest.raises(RuntimeError, match="observer failed"):
        atomic_project_parquet(source, outputs, batch_size=1, batch_observer=fail)
    assert all(path.read_bytes() == b"previous" for path in outputs)
    assert not list(tmp_path.glob(".*.tmp"))


def test_projection_promotion_failure_rolls_back_entire_output_set(tmp_path, monkeypatch):
    source = tmp_path / "source.parquet"
    pl.DataFrame({"id": [1]}).write_parquet(source)
    first, second = tmp_path / "a.parquet", tmp_path / "b.parquet"
    first.write_bytes(b"old-a")
    second.write_bytes(b"old-b")
    original = Path.replace

    def fail_second(path, target):
        if path.suffix == ".tmp" and Path(target) == second:
            raise OSError("promotion failed")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError, match="promotion failed"):
        atomic_project_parquet(source, {first: ["id"], second: ["id"]})
    assert first.read_bytes() == b"old-a" and second.read_bytes() == b"old-b"


def test_projection_rejects_changed_input_before_promotion(tmp_path):
    source = tmp_path / "source.parquet"
    pl.DataFrame({"id": [1, 2]}).write_parquet(source)
    output = tmp_path / "output.parquet"
    output.write_bytes(b"previous")

    def change(_batch):
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1000))

    with pytest.raises(ValueError, match="Input changed"):
        atomic_project_parquet(source, {output: ["id"]}, batch_observer=change)
    assert output.read_bytes() == b"previous"


@pytest.mark.parametrize("policy", [None, "retain_reusable", "compact"])
def test_cleanup_preserves_configured_inputs_and_obeys_cache_policy(tmp_path, policy):
    config = _config(tmp_path)
    raw = yaml.safe_load(config.read_text())
    paths = final_artifact_paths(config)
    source = paths.output_dir / "inputs/terrain.tif"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unique raw source")
    raw["paths"]["regional_dem_path"] = str(source)
    raw["run"] = {} if policy is None else {"cleanup_policy": policy}
    config.write_text(yaml.safe_dump(raw))
    _write_valid_static_outputs(config)
    generation_receipt = paths.land_static_weights.parent / "viewshed-generation.json"
    generation_receipt.write_text("generation receipt")
    factor_receipt = paths.distance_weights.with_suffix(".parquet.producer.json")
    factor_receipt.write_text("factor receipt")
    checkpoint = (
        paths.output_dir / "land/terrain_surfaces/bare_earth/partitions/source=land/cell.parquet"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    scratch = paths.output_dir / "_tmp/unrelated/scratch.bin"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(b"scratch")
    cleanup_viewshed_dir_to_static_outputs(config)
    assert source.read_bytes() == b"unique raw source"
    assert checkpoint.exists() == (policy != "compact")
    assert not scratch.exists()
    assert paths.source_target_lookup.exists() == (policy != "compact")
    assert generation_receipt.read_text() == "generation receipt"
    assert factor_receipt.exists() == (policy != "compact")
    assert paths.land_static_weights.exists() and paths.water_static_weights.exists()


def test_final_validation_detects_same_size_input_rewrite_with_restored_mtime(tmp_path):
    config = _config(tmp_path)
    _write_valid_static_outputs(config)
    raw = yaml.safe_load(config.read_text())
    paths = final_artifact_paths(config)
    validate_static_artifact_metadata(paths.land_static_weights, raw=raw, source_type="land")
    path = paths.distance_weights
    before = path.stat()
    values = path.read_bytes()
    path.write_bytes(bytes([values[0] ^ 1]) + values[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="inputs changed"):
        validate_static_artifact_metadata(paths.land_static_weights, raw=raw, source_type="land")


def test_repeated_final_validation_reads_unchanged_inputs_once(tmp_path, monkeypatch):
    from collections import Counter
    from viewshed_toolkit._internal.artifacts import checksums

    config = _config(tmp_path)
    _write_valid_static_outputs(config)
    paths = final_artifact_paths(config)
    raw = yaml.safe_load(config.read_text())
    checksums._unchanged_file_digest.cache_clear()
    original = checksums.checksum_path
    reads = Counter()

    def counted(path, **kwargs):
        reads[Path(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(checksums, "checksum_path", counted)
    for _ in range(2):
        validate_static_artifact_metadata(paths.land_static_weights, raw=raw, source_type="land")
    assert reads and all(count == 1 for count in reads.values())


def test_domain_cache_reuses_exact_wkb_and_rebuilds_changed_sources(tmp_path, monkeypatch):
    from tests.pipeline.test_component_workflow import coastal_fixture
    from viewshed_toolkit.pipeline.config.distance import load_distance_runtime
    from viewshed_toolkit.pipeline.prepare.area import domains

    runtime = load_distance_runtime(coastal_fixture(tmp_path))
    first = domains.load_land_water_domains(runtime, extent="target")
    original = domains._build_land_water_domains
    domains._DOMAIN_CACHE.clear()
    monkeypatch.setattr(
        domains,
        "_build_land_water_domains",
        lambda *_a, **_k: pytest.fail("repeated domain preparation"),
    )
    reused = domains.load_land_water_domains(runtime, extent="target")
    assert first.land_domain.wkb == reused.land_domain.wkb
    assert first.water_domain.wkb == reused.water_domain.wkb
    monkeypatch.setattr(domains, "_build_land_water_domains", original)
    cache = next((Path(runtime.output_dir) / "geometry_cache/land_water_domains").glob("*.parquet"))
    cache.write_bytes(b"corrupt cache")
    repaired = domains.load_land_water_domains(runtime, extent="target")
    assert repaired.water_domain.wkb == first.water_domain.wkb
    import geopandas as gpd

    water = Path(runtime.raw_config["paths"]["water_polygon_path"])
    frame = gpd.read_parquet(water)
    from shapely import affinity

    frame.geometry = frame.geometry.map(lambda g: affinity.translate(g, xoff=0.0001))
    frame.to_parquet(water)
    changed = domains.load_land_water_domains(runtime, extent="target")
    assert first.water_domain.wkb != changed.water_domain.wkb


def test_domain_algorithm_change_invalidates_completed_workflow_identity(monkeypatch):
    from viewshed_toolkit.pipeline.api.service import _stage_signature
    from viewshed_toolkit.pipeline.prepare.area import domains

    before = _stage_signature("same-config", ("terrain-weight",))
    monkeypatch.setattr(domains, "DOMAIN_GEOMETRY_ALGORITHM_VERSION", "new-domain-algorithm")
    assert _stage_signature("same-config", ("terrain-weight",)) != before


def test_cleanup_policy_rejects_unknown_value(tmp_path):
    from viewshed_toolkit.pipeline.config.loader import RunConfig

    with pytest.raises(ValueError, match="cleanup_policy"):
        RunConfig(name="n", version="v", cleanup_policy="silently_discard")


def test_metadata_replacement_preserves_linked_input(tmp_path):
    from viewshed_toolkit.pipeline.config.paths import write_metadata_sidecar

    source = tmp_path / "original_metadata.json"
    source.write_text('{"retained": true}\n')
    output = tmp_path / "cache.parquet"
    metadata = tmp_path / "cache_metadata.json"
    os.link(source, metadata)
    write_metadata_sidecar(output, {"run": {"version": "new"}})
    assert source.read_text() == '{"retained": true}\n'
    assert metadata.stat().st_ino != source.stat().st_ino


@pytest.mark.parametrize("extra", [False, True])
def test_shoreline_fill_difference_matches_previous_union_with_holes(extra):
    import geopandas as gpd
    from shapely.geometry import box
    from viewshed_toolkit._internal.geo.geometry import safe_polygonal_union
    from viewshed_toolkit.pipeline.prepare.area.domains import _land_domain_geometry

    bbox = box(0, 0, 10, 10)
    land = box(0, 0, 5, 10)
    water = box(5.2, 1, 9, 9).difference(box(7, 3, 8, 4))
    if extra:
        water = water.union(box(1, 2, 2, 3))
    offshore = bbox.difference(land).difference(water)
    boundary = bbox.boundary.buffer(1e-9)
    parts = [water] + [p for p in getattr(offshore, "geoms", [offshore]) if p.intersects(boundary)]
    expected = bbox.difference(
        safe_polygonal_union(gpd.GeoSeries(parts, crs=4326)).intersection(bbox)
    )
    actual = _land_domain_geometry(bbox, land, water)
    assert actual.equals(expected)


def test_projection_failed_restore_retains_recovery_backup(tmp_path, monkeypatch):
    source = tmp_path / "source.parquet"
    pl.DataFrame({"id": [1]}).write_parquet(source)
    first, second = tmp_path / "a.parquet", tmp_path / "b.parquet"
    first.write_bytes(b"old-a")
    second.write_bytes(b"old-b")
    original = Path.replace

    def fail(path, target):
        if path.suffix == ".tmp" and Path(target) == second:
            raise OSError("promotion failed")
        if path.suffix == ".backup":
            raise OSError("restore failed")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="restore failed"):
        atomic_project_parquet(source, {first: ["id"], second: ["id"]})
    assert {p.read_bytes() for p in tmp_path.glob(".*.backup")} == {b"old-a", b"old-b"}
