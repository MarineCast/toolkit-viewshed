"""Bounded regressions for component execution, reuse, telemetry, and map I/O."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import h3
import polars as pl
import pytest
import rasterio
from polars.testing import assert_frame_equal

from tests.pipeline.test_component_workflow import coastal_fixture
from viewshed_toolkit import load_app_config, run_component_stage, run_components
from viewshed_toolkit._internal.artifacts import checksums
from viewshed_toolkit.pipeline.api.registry import COMPONENT_TARGETS, component_plan
from viewshed_toolkit.pipeline.contracts import components as contracts


@pytest.mark.parametrize("target", ["dem", "chm", "static", "all"])
def test_water_dependency_plans_are_raster_free(target):
    plan = component_plan((COMPONENT_TARGETS[target],), source_type="water")
    assert not {"download-dem", "download-chm", "prepare-dem", "prepare-chm"}.intersection(plan)
    assert "build-source-target-lookup" in plan
    assert "build-dem-weights" in plan
    assert plan[-1] == COMPONENT_TARGETS[target]
    if target == "static":
        assert "export-maps" not in plan
    # An explicitly requested raster stage retains its explicit dependencies.
    assert "download-dem" in component_plan(("prepare-dem",), source_type="water")


def test_water_static_build_has_no_raster_or_map_execution(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.api import components
    from viewshed_toolkit.pipeline.visualization import component_maps

    config = coastal_fixture(tmp_path)
    (tmp_path / "dem.tif").unlink()
    (tmp_path / "chm.tif").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("water static build invoked raster acquisition, paired LOS, or maps")

    monkeypatch.setattr(components, "download_dataset", forbidden)
    monkeypatch.setattr(components, "prepare_dataset", forbidden)
    monkeypatch.setattr(components, "build_land_surface_components", forbidden)
    monkeypatch.setattr(component_maps, "export_component_maps", forbidden)
    outputs = run_components(config, target="static", source_type="water")
    assert outputs["finalize"].is_file()
    assert json.loads(outputs["validate"].read_text())["valid"]
    assert "export-maps" not in outputs
    assert not (tmp_path / "prepared").exists()


def test_component_input_cache_preserves_identity_and_output_validation(tmp_path, monkeypatch):
    app = load_app_config("configs/salish_sea.yaml")
    source = tmp_path / "source.bin"
    source.write_bytes(b"aaaa")
    checksums._unchanged_file_digest.cache_clear()
    first = contracts.provenance(app, "test", {"source": source})
    assert contracts.provenance(app, "test", {"source": source}) == first
    assert checksums._unchanged_file_digest.cache_info().hits == 1
    original = source.stat()
    source.write_bytes(b"bbbb")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert contracts.provenance(app, "test", {"source": source}) != first

    output = tmp_path / "output.bin"
    output.write_bytes(b"result")
    contracts.record_product(output, first)
    actual_checksum = contracts.checksum_path
    reads = []

    def track_output(path):
        reads.append(path)
        return actual_checksum(path)

    monkeypatch.setattr(contracts, "checksum_path", track_output)
    assert contracts.cache_matches(output, first)
    assert contracts.cache_matches(output, first)
    output.write_bytes(b"broken")
    assert not contracts.cache_matches(output, first)
    assert reads == [output, output, output]


def test_shapefile_cached_inputs_notice_companion_membership_changes(tmp_path):
    shape = tmp_path / "coast.shp"
    shape.write_bytes(b"shape")
    first = contracts.input_checksums({"land": shape}, reuse_unchanged=True)
    (tmp_path / "coast.dbf").write_bytes(b"attributes")
    assert contracts.input_checksums({"land": shape}, reuse_unchanged=True) != first


def test_cached_input_checksum_keeps_symlink_logical_name(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"data")
    alias = tmp_path / "alias.bin"
    alias.symlink_to(original)
    assert contracts.input_checksums({"source": alias}, reuse_unchanged=True) == {
        "source": checksums.checksum_path(alias)
    }


def test_disk_telemetry_is_not_sampled_at_rss_frequency(tmp_path, monkeypatch):
    from viewshed_toolkit._internal import performance

    enough_rss_samples = threading.Event()
    rss_samples = []
    disk_samples = []

    class Process:
        def children(self, **kwargs):
            return []

        def memory_info(self):
            rss_samples.append(1)
            if len(rss_samples) >= 5:
                enough_rss_samples.set()
            return SimpleNamespace(rss=1234)

    original_rglob = Path.rglob

    def count_disk(self, pattern):
        disk_samples.append(1)
        return original_rglob(self, pattern)

    monkeypatch.setattr(performance.psutil, "Process", Process)
    monkeypatch.setattr(Path, "rglob", count_disk)
    with performance.measure_stage(tmp_path, interval=0.001, disk_interval=60) as metrics:
        (tmp_path / "result").write_bytes(b"12345")
        assert enough_rss_samples.wait(timeout=2)
    assert len(disk_samples) == 2  # Initial and final, while RSS sampled repeatedly.
    assert metrics["peak_additional_work_disk_bytes"] == 5
    assert metrics["disk_sample_interval_seconds"] == 60
    assert metrics["peak_rss_bytes"] == 1234


def test_map_bundle_scans_once_and_reuses_target_geometry(tmp_path, monkeypatch):
    from viewshed_toolkit.pipeline.visualization import component_maps

    app = load_app_config(coastal_fixture(tmp_path))
    path = contracts.component_path(app, "static")
    path.parent.mkdir(parents=True)
    cells = sorted(h3.grid_disk(h3.latlng_to_cell(48.13, -122.765, 8), 1))[:4]
    pairs = pl.DataFrame(
        {
            "source_h3": [cells[0], cells[1], cells[0], cells[1]],
            "target_h3": [cells[2], cells[2], cells[3], cells[3]],
            "weight_terrain": [0.8, 0.0, 0.0, 0.0],
            "weight_vegetation": [0.5, 1.0, 1.0, 1.0],
            "weight_distance": [0.9, 0.6, 0.7, 0.8],
            "weight_static_viewability": [0.4, 0.0, 0.0, 0.0],
        }
    )
    pairs.write_parquet(path)
    scans, polygons, summaries = [], [], {}
    original_scan = pl.scan_parquet
    original_polygon = component_maps.cell_to_polygon
    original_render = component_maps._render_component_map

    def scan(*args, **kwargs):
        scans.append(args[0])
        return original_scan(*args, **kwargs)

    def polygon(cell):
        polygons.append(cell)
        return original_polygon(cell)

    def render(app, **kwargs):
        summaries[kwargs["factor"]] = kwargs["frame"]
        return original_render(app, **kwargs)

    monkeypatch.setattr(pl, "scan_parquet", scan)
    monkeypatch.setattr(component_maps, "cell_to_polygon", polygon)
    monkeypatch.setattr(component_maps, "_render_component_map", render)
    outputs = component_maps.export_component_maps(app, source_type="land")
    assert scans == [path]
    assert sorted(polygons) == cells[2:]
    for factor, column in {
        "terrain": "weight_terrain",
        "canopy": "weight_vegetation",
        "distance": "weight_distance",
        "static": "weight_static_viewability",
    }.items():
        values = pl.col(column)
        if factor == "canopy":
            values = values.filter(pl.col("weight_terrain") > 0)
        expected = (
            pairs.group_by("target_h3")
            .agg(
                values.mean().alias("mean_static_support"),
                pl.col("source_h3").n_unique().alias("source_cells"),
                (pl.col("weight_terrain") > 0).sum().alias("terrain_supported_sources"),
            )
            .sort("target_h3")
        )
        assert_frame_equal(summaries[factor], expected)
        assert b"component-legend" in outputs[factor].read_bytes()


def test_joint_land_build_matches_independent_stages_and_reuses_dem(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal")
    from viewshed_toolkit.pipeline.weights import components
    from viewshed_toolkit.pipeline.weights.terrain import runner

    paired_config = coastal_fixture(tmp_path / "paired")
    separate_config = coastal_fixture(tmp_path / "separate")
    counts = {"paired_runner": 0, "paired_preparations": 0, "separate_preparations": 0}
    mode = "paired"
    original_prepare = runner.prepare_batch_context
    original_pair = runner.run_paired_surface_source_cells

    def prepare(*args, **kwargs):
        counts[f"{mode}_preparations"] += 1
        return original_prepare(*args, **kwargs)

    def paired(*args, **kwargs):
        counts["paired_runner"] += 1
        return original_pair(*args, **kwargs)

    monkeypatch.setattr(runner, "prepare_batch_context", prepare)
    monkeypatch.setattr(runner, "run_paired_surface_source_cells", paired)
    outputs = run_components(paired_config, target="static", run_id="paired")
    mode = "separate"
    for stage in component_plan((COMPONENT_TARGETS["static"],)):
        run_component_stage(separate_config, stage)
    assert counts["paired_runner"] == 1
    assert counts["paired_preparations"] < counts["separate_preparations"]
    paired_app = load_app_config(paired_config)
    separate_app = load_app_config(separate_config)
    for product in ("dem", "chm", "static"):
        actual = pl.read_parquet(contracts.component_path(paired_app, product)).sort(
            contracts.PAIR_KEYS
        )
        expected = pl.read_parquet(contracts.component_path(separate_app, product)).sort(
            contracts.PAIR_KEYS
        )
        assert_frame_equal(actual, expected, check_exact=False, abs_tol=1e-7)
    assert outputs["finalize"].is_file()
    dem = contracts.component_path(paired_app, "dem")
    stamp = dem.stat().st_mtime_ns
    with rasterio.open(paired_app.paths.canopy_height_path, "r+") as dataset:
        values = dataset.read(1)
        values[values > 0] += 1
        dataset.write(values, 1)
    components.build_land_surface_components(paired_app)
    assert counts["paired_runner"] == 1  # Only CHM needed recomputation.
    assert dem.stat().st_mtime_ns == stamp
