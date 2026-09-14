from __future__ import annotations

from pathlib import Path

import h3
import numpy as np
import polars as pl
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import xy
from shapely.geometry import Polygon, mapping
from shapely.ops import transform as shapely_transform

from viewshed_toolkit.pipeline.contracts.artifacts import FINAL_SCHEMAS
from viewshed_toolkit.pipeline.weights.terrain import pixel_index, summarize


def _write_reference_raster(path: Path, shape: tuple[int, int]) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=shape[0],
        width=shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:32610",
        transform=Affine.translation(480_000.0, 5_371_000.0) * Affine.scale(10.0, -10.0),
    ) as dst:
        dst.write(np.zeros(shape, dtype="float32"), 1)


def test_full_aggregation_retains_narrow_visibility_missed_by_stride_sampling(
    tmp_path: Path,
) -> None:
    """A one-pixel feature between stride lattice rows must not disappear."""

    shape = (20, 20)
    reference_path = tmp_path / "reference.tif"
    _write_reference_raster(reference_path, shape)

    pixel_index = summarize.WaterPixelH3Index(
        code_grid=np.zeros(shape, dtype="int32"),
        h3_cells=("target",),
    )

    visible_count = np.zeros(shape, dtype="uint16")
    visible_count[1, :] = 1  # row 1 is never selected by a stride-10 lattice
    distance_weight_sum = visible_count.astype("float32")
    min_distance = np.where(visible_count, 1_000.0, np.inf).astype("float32")
    mean_distance = np.where(visible_count, 1_000.0, 0.0).astype("float32")
    max_distance = mean_distance.copy()
    observer_mask = visible_count.astype("uint64")
    target_area = pl.DataFrame(
        {
            "target_h3_cell": ["target"],
            "target_water_pixel_count": [400],
            "target_water_area_m2": [40_000.0],
            "target_water_area_km2": [0.04],
        }
    )
    common = dict(
        visible_count=visible_count,
        distance_weight_sum=distance_weight_sum,
        min_distance=min_distance,
        mean_distance=mean_distance,
        max_distance=max_distance,
        observer_mask=observer_mask,
        reference_raster_path=reference_path,
        h3_resolution=8,
        target_water_area=target_area,
        include_geometry=False,
        n_observer_points=1,
        pixel_h3_index=pixel_index,
    )

    full = summarize.cumulative_visible_arrays_to_h3(
        **common,
        aggregation_mode="full",
        pixel_stride=1,
    )
    sampled = summarize.cumulative_visible_arrays_to_h3(
        **common,
        aggregation_mode="sampled",
        pixel_stride=10,
    )

    assert len(full) == 1
    assert full.iloc[0]["union_visible_target_fraction"] == pytest.approx(0.05)
    assert full.iloc[0]["distance_weighted_los_fraction"] == pytest.approx(0.05)
    assert sampled.empty

    rows, cols = np.where(visible_count > 0)
    sparse = summarize.cumulative_visible_arrays_to_h3(
        visible_count=visible_count[rows, cols],
        distance_weight_sum=distance_weight_sum[rows, cols],
        min_distance=min_distance[rows, cols],
        mean_distance=mean_distance[rows, cols],
        max_distance=max_distance[rows, cols],
        observer_mask=observer_mask[rows, cols],
        reference_raster_path=reference_path,
        h3_resolution=8,
        target_water_area=target_area,
        include_geometry=False,
        n_observer_points=1,
        pixel_h3_index=pixel_index,
        aggregation_mode="full",
        pixel_stride=1,
        sparse_global_rows=rows.astype("int32"),
        sparse_global_cols=cols.astype("int32"),
    )
    pd_columns = [
        "target_h3_cell",
        "visible_sampled_pixel_count",
        "visible_observer_pixel_count_sum",
        "union_visible_target_fraction",
        "distance_weighted_los_fraction",
    ]
    assert sparse[pd_columns].to_dict("records") == full[pd_columns].to_dict("records")


def test_persisted_target_water_denominator_names_equivalent_pixel_count() -> None:
    assert FINAL_SCHEMAS["target_water_area"] == (
        "target_h3",
        "total_water_area_m2",
        "equivalent_water_pixel_count",
    )

    normalized = summarize._normalized_domain_target_water_area(
        pl.DataFrame(
            {
                "target_h3": ["target"],
                "total_water_area_m2": [900.0],
                "equivalent_water_pixel_count": [1],
            }
        )
    )

    assert normalized["target_water_pixel_count"].to_list() == [1]
    assert normalized["target_water_area_km2"].to_list() == [0.0009]


def test_canonical_pixel_h3_index_loads_an_aligned_batch_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transform = Affine.translation(480_000.0, 5_371_000.0) * Affine.scale(30.0, -30.0)
    canonical_path = tmp_path / "canonical_codes.tif"
    full_codes = np.arange(12 * 14, dtype="int32").reshape((12, 14))
    with rasterio.open(
        canonical_path,
        "w",
        driver="GTiff",
        height=12,
        width=14,
        count=1,
        dtype="int32",
        nodata=-1,
        crs="EPSG:32610",
        transform=transform,
    ) as destination:
        destination.write(full_codes, 1)
    artifact = pixel_index.CanonicalPixelH3Artifact(
        code_grid_path=canonical_path,
        lookup_path=tmp_path / "lookup.parquet",
        h3_cells=tuple(f"cell-{index}" for index in range(full_codes.size)),
        fingerprint="test",
    )
    monkeypatch.setattr(
        pixel_index,
        "ensure_canonical_pixel_h3_artifact",
        lambda *_args, **_kwargs: artifact,
    )
    batch_mask = np.ones((4, 5), dtype=bool)
    batch_mask[1, 2] = False
    batch_transform = transform * Affine.translation(3, 6)

    index = pixel_index.canonical_pixel_h3_window(
        object(),
        tmp_path / "water.tif",
        batch_transform=batch_transform,
        batch_crs="EPSG:32610",
        batch_water_mask=batch_mask,
    )

    expected = full_codes[6:10, 3:8].copy()
    expected[~batch_mask] = -1
    np.testing.assert_array_equal(index.code_grid, expected)
    assert index.code_grid_path == canonical_path


def test_rasterized_water_pixel_h3_index_matches_center_membership_on_boundaries(
    tmp_path: Path,
) -> None:
    """Rasterized H3 codes must match direct H3 lookup at every pixel center."""

    resolution = 8
    center_cell = h3.latlng_to_cell(48.1181, -123.4307, resolution)
    allowed_targets = tuple(sorted(h3.grid_disk(center_cell, 3)))
    to_projected = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)
    projected_polygons = {
        cell: shapely_transform(
            to_projected.transform,
            Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]),
        )
        for cell in allowed_targets
    }
    min_x = min(geometry.bounds[0] for geometry in projected_polygons.values())
    min_y = min(geometry.bounds[1] for geometry in projected_polygons.values())
    max_x = max(geometry.bounds[2] for geometry in projected_polygons.values())
    max_y = max(geometry.bounds[3] for geometry in projected_polygons.values())
    pixel_size = 30.0
    padding = 2.0 * pixel_size
    width = int(np.ceil((max_x - min_x + 2.0 * padding) / pixel_size))
    height = int(np.ceil((max_y - min_y + 2.0 * padding) / pixel_size))
    transform = Affine.translation(min_x - padding, max_y + padding) * Affine.scale(
        pixel_size, -pixel_size
    )
    water_mask = np.ones((height, width), dtype="uint8")
    water_mask[5:12, 7:15] = 0
    reference_path = tmp_path / "h3_boundary_water_mask.tif"
    with rasterio.open(
        reference_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:32610",
        transform=transform,
    ) as dst:
        dst.write(water_mask, 1)

    output_path = tmp_path / "canonical_codes.tif"
    pixel_index._build_code_grid(
        water_mask_path=reference_path,
        output_path=output_path,
        target_cells=allowed_targets,
        geometry_by_cell=projected_polygons,
        block_size=32,
    )
    with rasterio.open(output_path) as source:
        code_grid = source.read(1)
    index = pixel_index.WaterPixelH3Index(
        code_grid=code_grid,
        h3_cells=allowed_targets,
        code_grid_path=output_path,
    )
    rows, cols = np.indices((height, width))
    xs, ys = xy(transform, rows.ravel(), cols.ravel(), offset="center")
    to_wgs84 = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)
    lons, lats = to_wgs84.transform(xs, ys)
    code_by_cell = {cell: code for code, cell in enumerate(index.h3_cells)}
    expected = np.asarray(
        [
            code_by_cell.get(
                h3.latlng_to_cell(float(lat), float(lon), resolution),
                -1,
            )
            for lon, lat in zip(lons, lats, strict=True)
        ],
        dtype="int32",
    ).reshape((height, width))
    expected[~water_mask.astype(bool)] = -1

    boundary_mask = rasterize(
        [(mapping(geometry.boundary), 1) for geometry in projected_polygons.values()],
        out_shape=(height, width),
        transform=transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)
    boundary_water = boundary_mask & water_mask.astype(bool)
    assert np.count_nonzero(boundary_water) > 0
    np.testing.assert_array_equal(
        index.code_grid[boundary_water],
        expected[boundary_water],
    )
    np.testing.assert_array_equal(index.code_grid, expected)


def test_compact_h3_aggregation_filters_with_integer_code_mask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = (2, 4)
    reference_path = tmp_path / "reference.tif"
    _write_reference_raster(reference_path, shape)
    visible_count = np.ones(shape, dtype="uint16")
    observer_mask = np.ones(shape, dtype="uint64")
    compact_index = summarize.WaterPixelH3Index(
        code_grid=np.asarray(
            [[0, 0, 1, 1], [0, -1, 1, -1]],
            dtype="int32",
        ),
        h3_cells=("target-a", "target-b"),
    )
    target_area = pl.DataFrame(
        {
            "target_h3_cell": ["target-a", "target-b"],
            "target_water_pixel_count": [3, 3],
            "target_water_area_m2": [300.0, 300.0],
            "target_water_area_km2": [0.0003, 0.0003],
        }
    )
    monkeypatch.setattr(
        summarize.rasterio,
        "open",
        lambda *_args, **_kwargs: pytest.fail("batch-owned grid metadata should avoid reopen"),
    )

    result = summarize.cumulative_visible_arrays_to_h3(
        visible_count=visible_count,
        distance_weight_sum=visible_count.astype("float32"),
        min_distance=np.full(shape, 1_000.0, dtype="float32"),
        mean_distance=np.full(shape, 1_000.0, dtype="float32"),
        max_distance=np.full(shape, 1_000.0, dtype="float32"),
        observer_mask=observer_mask,
        reference_raster_path=reference_path,
        h3_resolution=8,
        target_water_area=target_area,
        aggregation_mode="full",
        include_geometry=False,
        n_observer_points=1,
        allowed_target_h3={"target-b"},
        pixel_h3_index=compact_index,
        reference_shape=shape,
        reference_crs="EPSG:32610",
        pixel_area_m2=100.0,
    )

    assert result["target_h3_cell"].tolist() == ["target-b"]
    assert result.iloc[0]["visible_sampled_pixel_count"] == 3
    assert result.iloc[0]["distance_weighted_los_fraction"] == pytest.approx(1.0)
