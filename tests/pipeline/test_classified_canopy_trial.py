import importlib.util
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import rasterio
from affine import Affine
from shapely.geometry import box, mapping


def module(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "classified_trial", scripts / "compare_classified_canopy.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_weighted_mean_keeps_unknown_and_range_in_full_denominator(monkeypatch):
    m = module(monkeypatch)
    result = m.aggregate_support(
        np.array([[0.5, -1, -2, 0], [1, 0, -2, 0]]),
        np.array([0, 0, 1, 1]),
        np.array([1, 3, 1, 1]),
        2,
    )
    assert result["score"] == [None, 0]
    np.testing.assert_allclose(result["lower"], [0.1875, 0])
    np.testing.assert_allclose(result["upper"], [0.5625, 0])
    np.testing.assert_allclose(result["unknown"], [0.375, 0])
    np.testing.assert_allclose(result["out_of_range"], [0, 0.5])


@pytest.mark.parametrize(
    "values,weights,count",
    [
        (np.array([[1.1]]), [1], 1),
        (np.array([[np.nan]]), [1], 1),
        (np.array([[-3]]), [1], 1),
        (np.array([[1]]), [0], 1),
        (np.array([[1]]), [1], 2),
        (np.empty((0, 1)), [1], 1),
    ],
)
def test_invalid_or_empty_support_is_rejected(monkeypatch, values, weights, count):
    with pytest.raises(ValueError):
        module(monkeypatch).aggregate_support(values, np.array([0]), np.array(weights), count)


def test_configuration_rejects_typos_before_writing(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="Unknown"):
        module(monkeypatch).run(dict(output_dir=str(tmp_path / "unused"), typo=True))
    assert not (tmp_path / "unused").exists()


def trial_inputs(m, tmp_path):
    transform = Affine(10, 0, 500000, 0, -10, 5400000)
    prep = tmp_path / "prepared"
    prep.mkdir()
    shape = (7, 7)
    rows, cols = np.array([3, 4]), np.array([5, 5])
    targets = np.array([transform * (c + 0.5, r + 0.5) for r, c in zip(rows, cols, strict=True)])
    grid = np.zeros(shape)
    mask = np.zeros(shape, bool)
    np.savez(
        prep / "Test.npz",
        ground=grid,
        canopy=grid,
        canopy_unknown=mask,
        building=grid,
        building_unknown=mask,
        building_present=mask,
        qualified_water=mask,
        transform=np.array(transform)[:6],
        targets=targets,
        target_h3=np.array(["water_a", "water_b"]),
        target_rows=rows,
        target_cols=cols,
    )
    geometry = mapping(box(500000, 5399930, 500070, 5400000))
    metadata = dict(
        cache_contract="bounded_cell_surface_cache_v1",
        cache_npz_sha256=m.digest(prep / "Test.npz"),
        building_inventory_policy="assume_complete",
        source_land=geometry,
        land=geometry,
    )
    (prep / "Test.json").write_text(json.dumps(metadata))
    observers = [[*transform * (c + 0.5, r + 0.5), 0] for r in range(3) for c in range(5)]
    site = dict(
        name="Test",
        source_h3="8728d180cffffff",
        target_cells=["water_a", "water_b"],
        observers=observers,
        exact_point=observers[0],
        target_polygons={
            k: [[500050, 5399950], [500060, 5399950], [500060, 5399940], [500050, 5399940]]
            for k in ("water_a", "water_b")
        },
    )
    pairs = [
        dict(
            site="Test",
            target_h3=cell,
            profile=profile,
            component=component,
            score=1,
            lower=1,
            upper=1,
            unknown_fraction=0,
        )
        for cell in site["target_cells"]
        for profile in ("reference", "direct_15_10m_stride2", "exact_point")
        for component in ("building", "joint")
    ]
    report = dict(
        contract="bounded_cell_score_benchmark_v1",
        config=dict(input_dir=str(prep)),
        sites=[site],
        pairs=pairs,
        input_checksums={"Test": {ext: m.digest(prep / f"Test.{ext}") for ext in ("json", "npz")}},
    )
    (tmp_path / "benchmark.json").write_text(json.dumps(report))
    wc = tmp_path / "classes.tif"
    with rasterio.open(
        wc,
        "w",
        driver="GTiff",
        width=7,
        height=7,
        count=1,
        dtype="uint8",
        crs="EPSG:32610",
        transform=transform,
        nodata=0,
    ) as ds:
        ds.write(np.full(shape, 30, dtype="uint8"), 1)
    asset = dict(path=str(wc), sha256=m.digest(wc))
    (tmp_path / "worldcover.json").write_text(
        json.dumps(dict(status="completed_diagnostic_only", assets=[asset]))
    )
    return dict(
        benchmark_report=str(tmp_path / "benchmark.json"),
        worldcover_manifest=str(tmp_path / "worldcover.json"),
        output_dir=str(tmp_path / "result"),
        repetitions=1,
    )


def test_end_to_end_trial_exports_matching_unique_tables_and_embedded_report(monkeypatch, tmp_path):
    m = module(monkeypatch)
    config = trial_inputs(m, tmp_path)
    report = m.run(config)
    out = Path(config["output_dir"])
    table = pl.read_parquet(out / "worldcover_canopy_scores.parquet")
    lookup = pl.read_parquet(out / "worldcover_view_scores.parquet")
    assert table.height == 24 and lookup.height == 2
    assert table["modeled_score"].to_list() == [1] * 24
    assert report["selected_profile"] == "fast"
    assert table.to_dicts() == sorted(
        report["pairs"],
        key=lambda r: (
            r["source_type"],
            r["source_h3"],
            r["target_h3"],
            r["profile"],
            r["beta_per_m"],
        ),
    )
    import re

    payload = re.search(
        r'<script id="payload" type="application/json">(.*?)</script>',
        (out / "report.html").read_text(),
        re.S,
    ).group(1)
    assert json.loads(payload) == json.loads((out / "report.json").read_text())
    assert "__PAYLOAD__" not in (out / "report.html").read_text()


def test_changed_worldcover_is_rejected_before_output_creation(monkeypatch, tmp_path):
    m = module(monkeypatch)
    config = trial_inputs(m, tmp_path)
    (tmp_path / "classes.tif").write_bytes(b"stale input")
    with pytest.raises(ValueError, match="WorldCover source checksum"):
        m.run(config)
    assert not Path(config["output_dir"]).exists()


def test_conflicting_worldcover_tiles_cannot_silently_overwrite(monkeypatch, tmp_path):
    m = module(monkeypatch)
    transform = Affine(10, 0, 500000, 0, -10, 5400000)
    paths = []
    for code in (10, 30):
        path = tmp_path / f"{code}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=1,
            dtype="uint8",
            crs="EPSG:32610",
            transform=transform,
            nodata=0,
        ) as ds:
            ds.write(np.full((2, 2), code, dtype="uint8"), 1)
        paths.append(dict(path=str(path)))
    with pytest.raises(ValueError, match="Conflicting"):
        m.load_worldcover(paths, (2, 2), transform)
