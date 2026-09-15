from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import h3
import numpy as np
import polars as pl
import pytest
import yaml

from viewshed_toolkit import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    load_app_config,
    validate_distance_product,
)
from viewshed_toolkit._internal.artifacts import checksum_path
from viewshed_toolkit.pipeline.config import write_metadata_sidecar
from viewshed_toolkit.pipeline.config.distance import (
    load_distance_runtime,
    load_distance_weight_config,
)
from viewshed_toolkit.pipeline.contracts.artifacts import final_artifact_paths_from_raw
from viewshed_toolkit.pipeline.contracts.components import component_path, fingerprint
from viewshed_toolkit.pipeline.contracts.distance import (
    DISTANCE_PROFILE_SCHEMA,
    PAIR_DISTANCE_SCHEMA,
    distance_metadata_path,
)
from viewshed_toolkit.pipeline.prepare.area.config import (
    _lookup_config,
    _lookup_metadata_extra,
)
from viewshed_toolkit.pipeline.weights.distance.compute import distance_weight_values


def _cells() -> tuple[str, str, str]:
    source = h3.latlng_to_cell(48.13, -122.76, 7)
    neighbors = sorted(set(h3.grid_disk(source, 1)) - {source})
    return source, neighbors[0], neighbors[1]


def _rows() -> list[dict[str, object]]:
    source, first, second = _cells()
    return [
        {"source_h3": source, "target_h3": first, "distance_km": 0.0, "source_type": "land"},
        {
            "source_h3": source,
            "target_h3": second,
            "distance_km": 12.0,
            "source_type": "land",
        },
        {
            "source_h3": source,
            "target_h3": first,
            "distance_km": 4.0,
            "source_type": "water",
        },
    ]


def _config_with_lookup(
    root: Path,
    rows: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(Path("configs/salish_sea.yaml").read_text(encoding="utf-8"))
    raw.pop("area", None)
    raw["region"].update(
        name="distance_product_fixture",
        bbox_wgs84={
            "min_lon": -123.0,
            "min_lat": 48.0,
            "max_lon": -122.5,
            "max_lat": 48.5,
        },
    )
    raw["h3"].update(source_resolution=7, target_resolution=7)
    raw["viewshed"].update(max_distance_m=30_000, crs_projected="EPSG:32610")
    raw["source_target_lookup"].update(
        h3_resolution=7,
        max_distance_km_land=30.0,
        max_distance_km_water=30.0,
    )
    raw["paths"] = {
        "land_polygon_path": str(root / "land.geojson"),
        "water_polygon_path": str(root / "water.parquet"),
        "regional_dem_path": str(root / "absent-dem.tif"),
        "canopy_height_path": str(root / "absent-chm.tif"),
        "raw_dem_dir": str(root / "absent-raw-dem"),
        "output_dir": str(root / "work"),
        "final_output_dir": str(root / "final"),
        "map_dir": str(root / "maps"),
        "land_h3_path": str(root / "land-cells.parquet"),
        "source_cells_path": str(root / "land-cells.parquet"),
        "projected_dem_path": str(root / "absent-projected-dem.tif"),
    }
    (root / "land.geojson").write_text("land-v1", encoding="utf-8")
    (root / "water.parquet").write_text("water-v1", encoding="utf-8")
    config_path = root / "distance.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    lookup_path = final_artifact_paths_from_raw(raw, root).source_target_lookup
    lookup_path.parent.mkdir(parents=True, exist_ok=True)
    lookup_rows = _rows() if rows is None else rows
    frame = pl.DataFrame(
        lookup_rows,
        schema={
            "source_h3": pl.String,
            "target_h3": pl.String,
            "distance_km": pl.Float32,
            "source_type": pl.String,
        },
    )
    frame.write_parquet(lookup_path)
    runtime = load_distance_runtime(config_path)
    weight_config = load_distance_weight_config(runtime.raw_config)
    lookup_config = _lookup_config(runtime.raw_config, runtime, weight_config)
    source_types = frame["source_type"].to_list()
    metadata = _lookup_metadata_extra(
        runtime,
        weight_config,
        lookup_config,
        n_pairs=frame.height,
        n_source_cells=frame["source_h3"].n_unique(),
        n_land_source_cells=(frame.filter(pl.col("source_type") == "land")["source_h3"].n_unique()),
        n_water_source_cells=(
            frame.filter(pl.col("source_type") == "water")["source_h3"].n_unique()
        ),
        n_target_cells=frame["target_h3"].n_unique(),
        grid_disk_k=1,
    )
    assert set(source_types).issubset({"land", "water"})
    write_metadata_sidecar(lookup_path, raw, metadata)
    return config_path, lookup_path


def test_pair_products_support_both_roles_and_safe_combination(tmp_path: Path) -> None:
    config, _ = _config_with_lookup(tmp_path)
    land_path = build_pair_distances(config, source_type="land")
    water_path = build_pair_distances(config, source_type="water")

    land = pl.read_parquet(land_path)
    water = pl.read_parquet(water_path)
    assert tuple(land.columns) == PAIR_DISTANCE_SCHEMA
    assert tuple(water.columns) == PAIR_DISTANCE_SCHEMA
    combined = pl.concat([land, water])
    assert not combined.select("source_type", "source_h3", "target_h3").is_duplicated().any()
    assert combined.select("source_h3", "target_h3").is_duplicated().any()
    assert validate_distance_product(land_path)["contract"]["source_type"] == "land"
    assert validate_distance_product(water_path)["contract"]["source_type"] == "water"


def test_multiple_profiles_reuse_one_pair_product_without_geometry(tmp_path: Path) -> None:
    config, lookup = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    pair_checksum = checksum_path(pair_path)
    pair_stamp = pair_path.stat().st_mtime_ns
    lookup.unlink()
    (tmp_path / "land.geojson").unlink()
    (tmp_path / "water.parquet").unlink()

    near = build_distance_profile(
        pair_path,
        DistanceProfile("near", selected_model="exponential", exponential_lambda_km=2.0),
    )
    broad = build_distance_profile(
        pair_path,
        DistanceProfile("broad", selected_model="exponential", exponential_lambda_km=20.0),
    )

    assert near != broad
    assert pair_path.stat().st_mtime_ns == pair_stamp
    assert checksum_path(pair_path) == pair_checksum
    near_frame = pl.read_parquet(near)
    broad_frame = pl.read_parquet(broad)
    assert near_frame.select(PAIR_DISTANCE_SCHEMA).equals(broad_frame.select(PAIR_DISTANCE_SCHEMA))
    assert not np.array_equal(
        near_frame["weight_distance"].to_numpy(), broad_frame["weight_distance"].to_numpy()
    )
    validate_distance_product(near)
    validate_distance_product(broad)


def test_profile_cutoff_preserves_pairs_and_enforces_available_coverage(tmp_path: Path) -> None:
    config, _ = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    profile_path = build_distance_profile(
        pair_path,
        DistanceProfile(
            "cutoff",
            selected_model="exponential",
            exponential_lambda_km=8.0,
            hard_cutoff_km=4.0,
        ),
    )
    pair_frame = pl.read_parquet(pair_path)
    weighted = pl.read_parquet(profile_path)
    assert weighted.height == pair_frame.height
    assert weighted.filter(pl.col("distance_km") > 4.0)["weight_distance"].eq(0.0).all()
    assert weighted.filter(pl.col("distance_km") == 0.0)["weight_distance"].eq(1.0).all()

    boundary_path = build_distance_profile(
        pair_path,
        DistanceProfile(
            "boundary",
            selected_model="exponential",
            exponential_lambda_km=8.0,
            hard_cutoff_km=12.0,
        ),
    )
    boundary = pl.read_parquet(boundary_path)
    assert boundary.filter(pl.col("distance_km") == 12.0)["weight_distance"].gt(0.0).all()
    with pytest.raises(ValueError, match="exceeds pair-product coverage"):
        build_distance_profile(
            pair_path,
            DistanceProfile("too-wide", selected_model="exponential", hard_cutoff_km=31.0),
        )


def test_equivalent_effective_profiles_share_scientific_identity(tmp_path: Path) -> None:
    config, _ = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    first = build_distance_profile(
        pair_path,
        DistanceProfile(
            "exponential",
            selected_model="exponential",
            exponential_lambda_km=8.0,
            logistic_d50_km=1.0,
        ),
    )
    stamp = first.stat().st_mtime_ns
    second = build_distance_profile(
        pair_path,
        DistanceProfile(
            "exponential",
            selected_model="exponential",
            exponential_lambda_km=8.0,
            logistic_d50_km=99.0,
        ),
    )
    assert first == second
    assert second.stat().st_mtime_ns == stamp
    record = validate_distance_product(second)
    assert record["contract"]["effective_profile"] == {
        "selected_model": "exponential",
        "parameters": {"lambda_km": 8.0},
        "distance_units": "km",
        "effective_cutoff_km": None,
        "cutoff_behavior": "preserve_pairs_and_assign_zero_beyond_cutoff",
    }


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (DistanceProfile("bad", selected_model="gaussian"), "selected_model"),
        (
            DistanceProfile("bad", selected_model="logistic", logistic_slope_km=0.0),
            "logistic_slope_km",
        ),
        (
            DistanceProfile(
                "bad",
                selected_model="piecewise",
                piecewise_full_weight_km=5.0,
                piecewise_zero_weight_km=4.0,
            ),
            "piecewise_zero_weight_km",
        ),
        (DistanceProfile("../escape"), "profile_id"),
    ],
)
def test_invalid_profiles_fail_clearly(
    tmp_path: Path, profile: DistanceProfile, message: str
) -> None:
    config, _ = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    with pytest.raises(ValueError, match=message):
        build_distance_profile(pair_path, profile)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows.__setitem__(1, {**rows[1], "distance_km": None}), "observed"),
        (lambda rows: rows.__setitem__(1, {**rows[1], "distance_km": -1.0}), "nonnegative"),
        (lambda rows: rows.__setitem__(1, {**rows[1], "distance_km": float("nan")}), "finite"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate"),
        (lambda rows: rows.__setitem__(0, {**rows[0], "source_h3": "invalid"}), "Invalid H3"),
        (
            lambda rows: rows.__setitem__(
                0,
                {**rows[0], "source_h3": h3.latlng_to_cell(48.13, -122.76, 6)},
            ),
            "resolution mismatch",
        ),
    ],
)
def test_pair_distance_integrity_failures(
    tmp_path: Path,
    mutate: Callable[[list[dict[str, object]]], None],
    message: str,
) -> None:
    rows = _rows()
    mutate(rows)
    config, _ = _config_with_lookup(tmp_path, rows)
    with pytest.raises(ValueError, match=message):
        build_pair_distances(config)


def test_empty_role_and_zero_distance_are_valid(tmp_path: Path) -> None:
    rows = [row for row in _rows() if row["source_type"] == "water"]
    config, _ = _config_with_lookup(tmp_path, rows)
    empty = build_pair_distances(config, source_type="land")
    assert pl.read_parquet(empty).is_empty()
    assert tuple(pl.read_parquet(empty).columns) == PAIR_DISTANCE_SCHEMA

    water = build_pair_distances(config, source_type="water")
    profile = build_distance_profile(
        water,
        DistanceProfile("zero", selected_model="exponential", exponential_lambda_km=8.0),
    )
    assert tuple(pl.read_parquet(profile).columns) == DISTANCE_PROFILE_SCHEMA


def test_cache_isolated_from_rasters_but_tracks_lookup_inputs(tmp_path: Path) -> None:
    config, lookup = _config_with_lookup(tmp_path)
    first = build_pair_distances(config)
    stamp = first.stat().st_mtime_ns
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["viewshed"]["dem_nodata_policy"] = "error"
    raw["paths"]["regional_dem_path"] = str(tmp_path / "another-absent-dem.tif")
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert build_pair_distances(config) == first
    assert first.stat().st_mtime_ns == stamp

    (tmp_path / "land.geojson").write_text("land-v2", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata does not match current config"):
        build_pair_distances(config)

    # A newly validated lookup with changed content invalidates the fixed raw-product path.
    _, target, _ = _cells()
    _config_with_lookup(
        tmp_path,
        [
            {
                "source_h3": target,
                "target_h3": target,
                "distance_km": 0.0,
                "source_type": "land",
            }
        ],
    )
    assert lookup.exists()
    with pytest.raises(FileExistsError, match="different or invalid contract"):
        build_pair_distances(config)
    assert build_pair_distances(config, overwrite=True) == first


def test_reordered_lookup_has_equivalent_pair_scientific_identity(tmp_path: Path) -> None:
    rows = _rows()
    config, _ = _config_with_lookup(tmp_path, rows)
    pair_path = build_pair_distances(config)
    first = validate_distance_product(pair_path)
    first_frame = pl.read_parquet(pair_path)

    _config_with_lookup(tmp_path, list(reversed(rows)))
    build_pair_distances(config, overwrite=True)
    second = validate_distance_product(pair_path)

    assert pl.read_parquet(pair_path).equals(first_frame)
    assert (
        second["contract"]["pair_scientific_identity"]
        == first["contract"]["pair_scientific_identity"]
    )


def test_corrupt_metadata_checksum_and_containment_are_rejected(tmp_path: Path) -> None:
    config, _ = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    metadata_path = distance_metadata_path(pair_path)
    original_record = json.loads(metadata_path.read_text(encoding="utf-8"))

    metadata_path.unlink()
    with pytest.raises(ValueError, match="metadata is missing"):
        validate_distance_product(pair_path)
    metadata_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no contract"):
        validate_distance_product(pair_path)
    metadata_path.write_text(json.dumps(original_record), encoding="utf-8")
    pair_path.write_bytes(pair_path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_distance_product(pair_path)

    config, _ = _config_with_lookup(tmp_path / "contained")
    pair_path = build_pair_distances(config)
    metadata_path = distance_metadata_path(pair_path)
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    record["contract"]["output_root"] = str(tmp_path / "elsewhere")
    record["fingerprint"] = fingerprint(record["contract"])
    metadata_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes configured output root"):
        validate_distance_product(pair_path)


def test_pair_product_rejects_legacy_cleanup_overlap(tmp_path: Path) -> None:
    config, _ = _config_with_lookup(tmp_path)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["paths"]["final_output_dir"] = raw["paths"]["output_dir"]
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=r"final_output_dir outside paths\.output_dir"):
        build_pair_distances(config)


def test_default_component_remains_numerically_compatible(tmp_path: Path) -> None:
    from viewshed_toolkit import run_component_stage

    config, _ = _config_with_lookup(tmp_path)
    app = load_app_config(config)
    output = run_component_stage(app, "build-distance-weights", source_type="land")
    frame = pl.read_parquet(output)
    cfg = load_distance_weight_config(app.raw_config)
    expected = distance_weight_values(
        frame["distance_km"].to_numpy(),
        cfg,
        max_distance_km=cfg.hard_cutoff_km or app.viewshed.max_distance_m / 1000.0,
    )
    np.testing.assert_array_equal(frame["weight_distance"].to_numpy(), expected)
    assert frame.columns == [
        "source_h3",
        "target_h3",
        "distance_km",
        "distance_m",
        "weight_distance",
        "distance_model",
    ]
    assert component_path(app, "distance") == output


def test_cli_profile_and_validation_match_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from viewshed_toolkit.pipeline.cli.main import main

    config, _ = _config_with_lookup(tmp_path)
    pair_path = build_pair_distances(config)
    expected = build_distance_profile(
        pair_path,
        DistanceProfile(
            "cli",
            selected_model="exponential",
            exponential_lambda_km=6.0,
            hard_cutoff_km=20.0,
        ),
    )
    main(
        [
            "build-distance-profile",
            "--pair-distances",
            str(pair_path),
            "--profile-id",
            "cli",
            "--model",
            "exponential",
            "--exponential-lambda-km",
            "6",
            "--hard-cutoff-km",
            "20",
        ]
    )
    assert str(expected) in capsys.readouterr().out
    main(["validate-distance-product", str(expected)])
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["product_type"] == "distance_profile"
