from __future__ import annotations

from dataclasses import replace

import h3
import pandas as pd
import polars as pl
import pytest

from viewshed_toolkit.pipeline.api import pilot
from viewshed_toolkit.pipeline.config import load_app_config
from viewshed_toolkit.pipeline.weights.canopy_visibility import make_terrain_variant_app


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    base = load_app_config("configs/salish_sea.yaml")
    paths = replace(
        base.paths,
        **{
            name: tmp_path / name
            for name in (
                "output_dir",
                "final_output_dir",
                "map_dir",
                "final_visibility_path",
                "partitioned_visibility_dir",
                "manifest_path",
                "projected_dem_path",
            )
        },
    )
    base = replace(
        base,
        paths=paths,
        source_type="land",
        h3=replace(
            base.h3,
            source_resolution=7,
            output_resolution=7,
            source_sampling_mode="active_fraction",
            min_sample_points_per_source_cell=5,
            sample_points_per_source_cell=10,
        ),
        viewshed=replace(
            base.viewshed, max_distance_m=30000, aoi_margin_m=1000, dem_resolution_m=30
        ),
        batch=replace(base.batch, max_workers=1, raster_stack_mode="windowed"),
        run=replace(
            base.run,
            keep_batch_intermediates=True,
            combine_final_parquet=False,
            overwrite=False,
            write_maps=False,
            write_geojson=False,
            write_cumulative_rasters=False,
        ),
    )
    bare = make_terrain_variant_app(base, surface_model="bare_earth", terrain_dir=tmp_path / "bare")
    canopy = make_terrain_variant_app(base, surface_model="canopy", terrain_dir=tmp_path / "canopy")
    source = h3.latlng_to_cell(48.4, -123.3, 7)
    lookup = tmp_path / "lookup.parquet"
    pl.DataFrame(
        {"source_h3": [source], "target_h3": [source], "source_type": ["land"]}
    ).write_parquet(lookup)
    monkeypatch.setattr(pilot, "_area_lookup_path_for_app", lambda _: lookup)
    return bare, canopy, source, lookup


def test_pilot_retains_selected_land_pairs_and_rejects_duplicate_keys(prepared, tmp_path):
    bare, canopy, source, lookup = prepared
    assert pilot.validate_pilot_apps(bare, canopy, tmp_path, source) == 1
    original = pl.read_parquet(lookup)
    pl.concat([original, original]).write_parquet(lookup)
    with pytest.raises(ValueError, match="unique"):
        pilot.validate_pilot_apps(bare, canopy, tmp_path, source)


def test_pilot_enforces_selected_pair_ceiling(prepared, tmp_path):
    bare, canopy, source, lookup = prepared
    pl.DataFrame(
        {"source_h3": [source] * 6001, "target_h3": [str(i) for i in range(6001)]}
    ).write_parquet(lookup)
    with pytest.raises(ValueError, match="6000"):
        pilot.validate_pilot_apps(bare, canopy, tmp_path, source)


@pytest.mark.parametrize("setting", ["external_path", "prior_manifest", "promotion", "sampling"])
def test_pilot_rejects_unsafe_output_or_changed_policy(prepared, tmp_path, setting):
    bare, canopy, source, _ = prepared
    if setting == "external_path":
        bare = replace(bare, paths=replace(bare.paths, map_dir=tmp_path.parent / "external"))
    elif setting == "prior_manifest":
        bare.paths.manifest_path.parent.mkdir(parents=True)
        bare.paths.manifest_path.write_text("preserve")
    elif setting == "promotion":
        bare = replace(bare, run=replace(bare.run, combine_final_parquet=True))
    else:
        bare = replace(bare, h3=replace(bare.h3, min_sample_points_per_source_cell=1))
        canopy = replace(canopy, h3=bare.h3)
    with pytest.raises(ValueError):
        pilot.validate_pilot_apps(bare, canopy, tmp_path, source)


@pytest.mark.parametrize("status", ["ok", "failed", "skipped"])
def test_worker_requires_one_completed_source_on_both_surfaces(
    prepared, tmp_path, monkeypatch, status
):
    bare, canopy, source, _ = prepared
    monkeypatch.setattr(
        pilot, "load_app_config", lambda p, **_: bare if p.name == "bare" else canopy
    )
    seen = []

    def paired(*_args, **kwargs):
        seen.append(kwargs["selected_source_cells"])
        return pd.DataFrame({"source_h3_cell": [source], "status": ["ok"]}), pd.DataFrame(
            {"source_h3_cell": [source], "status": [status]}
        )

    monkeypatch.setattr(pilot, "run_paired_surface_source_cells", paired)
    if status == "ok":
        pilot._worker(["bare", "canopy", source, str(tmp_path), "-"])
        assert seen == [[source]]
    else:
        with pytest.raises(RuntimeError, match="completion failed"):
            pilot._worker(["bare", "canopy", source, str(tmp_path), "-"])
