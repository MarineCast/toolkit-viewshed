from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl
from shapely.geometry import box

from viewshed_toolkit.pipeline.prepare.area import domains
from viewshed_toolkit.pipeline.prepare.area import lookup as area_lookup
from viewshed_toolkit.pipeline.prepare.area.config import (
    _configured_path_content_sha256,
)


def _centroids() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "h3": ["source", "edge", "far"],
            "lat": [0.0, 0.0, 0.0],
            "lon": [0.0, 0.02, 0.03],
        }
    )


def test_target_domain_is_buffered_beyond_source_bbox_in_projected_metres() -> None:
    runtime = SimpleNamespace(
        bbox_wgs84=(-125.8, 46.85, -121.6, 50.0),
        projected_crs="EPSG:32610",
        raw_config={
            "viewshed": {
                "max_distance_m": 30_000,
                "aoi_margin_m": 1_000,
            }
        },
    )

    target = domains.target_domain_polygon(runtime)
    source = box(*runtime.bbox_wgs84)

    assert target.contains(source)
    assert target.bounds[0] < source.bounds[0]
    assert target.bounds[1] < source.bounds[1]
    assert target.bounds[2] > source.bounds[2]
    assert target.bounds[3] > source.bounds[3]


def test_lookup_source_fingerprint_changes_with_file_content(tmp_path: Path) -> None:
    source = tmp_path / "water.parquet"
    source.write_bytes(b"first")
    runtime = SimpleNamespace(config_dir=tmp_path)

    first = _configured_path_content_sha256(runtime, source)
    source.write_bytes(b"second")
    second = _configured_path_content_sha256(runtime, source)

    assert first != second


def test_geometry_cutoff_retains_edge_pair_beyond_centroid_cutoff(monkeypatch) -> None:
    monkeypatch.setattr(
        area_lookup.domains,
        "h3_grid_disk",
        lambda _source, _k: {"source", "edge", "far"},
    )
    geometries = {
        "source": box(0.0, 0.0, 1_000.0, 1_000.0),
        "edge": box(2_000.0, 0.0, 3_000.0, 1_000.0),
        "far": box(3_000.0, 0.0, 4_000.0, 1_000.0),
    }

    result, _ = area_lookup._build_pair_chunk_polars(
        ["source"],
        target_cells={"edge", "far"},
        centroid_df=_centroids(),
        source_type="land",
        max_distance_km=1.5,
        grid_disk_k=3,
        allow_self_pairs=False,
        projected_cell_geometries=geometries,
    )

    assert result["target_h3"].to_list() == ["edge"]
    assert result.item(0, "distance_km") > 1.5


def test_active_role_self_pair_is_retained_when_generic_self_pairs_are_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        area_lookup.domains,
        "h3_grid_disk",
        lambda _source, _k: {"source"},
    )
    geometries = {"source": box(0.0, 0.0, 1_000.0, 1_000.0)}

    retained, _ = area_lookup._build_pair_chunk_polars(
        ["source"],
        target_cells={"source"},
        centroid_df=_centroids(),
        source_type="land",
        max_distance_km=1.5,
        grid_disk_k=0,
        allow_self_pairs=False,
        allow_active_role_self_pairs=True,
        projected_cell_geometries=geometries,
    )
    excluded, _ = area_lookup._build_pair_chunk_polars(
        ["source"],
        target_cells={"source"},
        centroid_df=_centroids(),
        source_type="land",
        max_distance_km=1.5,
        grid_disk_k=0,
        allow_self_pairs=False,
        allow_active_role_self_pairs=False,
        projected_cell_geometries=geometries,
    )

    assert retained.select("source_h3", "target_h3").rows() == [("source", "source")]
    assert excluded.is_empty()
