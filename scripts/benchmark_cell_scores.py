"""Offline cell-score speed/accuracy experiment; no production promotion.

Use caches written by compare_obstruction_points.py's benchmark_cache_dir option.
Usage: PYTHONPATH=src python scripts/benchmark_cell_scores.py CONFIG.json
"""

import hashlib
import html
import json
import platform
import sys
import time
from pathlib import Path

import h3
import numpy as np
import polars as pl
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import shape
from shapely.ops import unary_union

from viewshed_toolkit import direct_ray_visibility
from viewshed_toolkit.pipeline.prepare.area.sampling import sample_points_in_source_geometry
from viewshed_toolkit.pipeline.prepare.area.transmission import target_design
from viewshed_toolkit.pipeline.prepare.elevation.obstruction import compose_above_ground_surface
from viewshed_toolkit.pipeline.prepare.elevation.terrain import sample_observer_ground_elevation

CONTRACT = "bounded_cell_score_benchmark_v1"
UNKNOWN_BARRIER_M = 100000.0
GDAL_DIAGNOSTICS = {"monotonic_conflicts": 0}
PROFILES = [
    ("direct_5_10m_stride2", "direct", 5, 10, 2),
    ("direct_10_10m_stride2", "direct", 10, 10, 2),
    ("direct_15_10m_stride2", "direct", 15, 10, 2),
    ("gdal_5_10m_full", "gdal", 5, 10, 1),
    ("gdal_10_10m_full", "gdal", 10, 10, 1),
    ("gdal_15_10m_full", "gdal", 15, 10, 1),
    ("gdal_5_30m_full", "gdal", 5, 30, 1),
    ("gdal_10_30m_full", "gdal", 10, 30, 1),
]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def aggregate(states, codes, weights, cell_count):
    """Average across all observer/target pairs, preserving unknown and range zeros."""
    if states.ndim != 2 or states.shape[1] != len(codes) or len(weights) != len(codes):
        raise ValueError("Aligned observer-by-target states and target weights required")
    if not np.isin(states, [-2, -1, 0, 1]).all() or (weights <= 0).any():
        raise ValueError("Valid visibility states and positive weights required")
    denominator = np.bincount(codes, weights=weights, minlength=cell_count) * len(states)
    if (denominator <= 0).any():
        raise ValueError("Every cell requires positive observer and target support")
    clear = np.bincount(codes, weights=(states == 1).sum(0) * weights, minlength=cell_count)
    unknown = np.bincount(codes, weights=(states == -1).sum(0) * weights, minlength=cell_count)
    excluded = np.bincount(codes, weights=(states == -2).sum(0) * weights, minlength=cell_count)
    lower, upper = clear / denominator, (clear + unknown) / denominator
    return dict(
        lower=lower,
        upper=upper,
        unknown=unknown / denominator,
        out_of_range=excluded / denominator,
        score=[
            None if missing else float(value) for value, missing in zip(lower, unknown, strict=True)
        ],
    )


def coarsen(data):
    """Conservative max height/any unknown; original 30m terrain support unchanged."""
    result = {}
    for key, a in data.items():
        if key == "transform":
            result[key] = a * Affine.scale(3)
            continue
        blocks = a.reshape(a.shape[0] // 3, 3, a.shape[1] // 3, 3)
        if key == "ground":
            result[key] = blocks[:, 1, :, 1].copy()
        elif key == "qualified_water":
            result[key] = blocks.all(axis=(1, 3))
        else:
            result[key] = blocks.max(axis=(1, 3))
    return result


def gdal_visibility(
    ground,
    lower,
    unknown,
    *,
    transform,
    observer_xy,
    targets_xy,
    observer_ground_elevation_m,
    max_distance_m=5000,
):
    """GDAL Edge floor/unknown-ceiling evaluation on caller-owned MEM rasters."""
    from osgeo import gdal

    eye = observer_ground_elevation_m + 1.7
    if eye > 10000 or np.max(ground + lower) > 10000:
        raise ValueError("Bounded unknown barrier only qualified below 10000m")
    source_r, source_c = rasterio.transform.rowcol(transform, *observer_xy)
    target_r, target_c = rasterio.transform.rowcol(transform, targets_xy[:, 0], targets_xy[:, 1])
    target_r, target_c = np.asarray(target_r), np.asarray(target_c)

    def visible(upper):
        surface = ground + np.where(unknown, UNKNOWN_BARRIER_M, lower) if upper else ground + lower
        surface = surface.astype("float64", copy=True)
        surface[source_r, source_c] = ground[source_r, source_c]
        ds = gdal.GetDriverByName("MEM").Create(
            "", surface.shape[1], surface.shape[0], 1, gdal.GDT_Float64
        )
        ds.SetGeoTransform(transform.to_gdal())
        ds.SetProjection(rasterio.crs.CRS.from_epsg(32610).to_wkt())
        ds.GetRasterBand(1).WriteArray(surface)
        output = gdal.ViewshedGenerate(
            ds.GetRasterBand(1),
            "MEM",
            "",
            [],
            *map(float, observer_xy),
            eye - float(surface[source_r, source_c]),
            1.0,
            1.0,
            0.0,
            0.0,
            255.0,
            0.85714,
            gdal.GVM_Edge,
            max_distance_m,
        )
        if output is None:
            raise RuntimeError(gdal.GetLastErrorMsg())
        a = output.ReadAsArray()
        child = Affine.from_gdal(*output.GetGeoTransform())
        rr, cc = rasterio.transform.rowcol(child, targets_xy[:, 0], targets_xy[:, 1])
        rr, cc = np.asarray(rr), np.asarray(cc)
        valid = (rr >= 0) & (cc >= 0) & (rr < a.shape[0]) & (cc < a.shape[1])
        result = np.zeros(len(rr), bool)
        result[valid] = a[rr[valid], cc[valid]] == 1
        return result

    floor = visible(False)
    ceiling = visible(True) if unknown.any() else floor
    conflicts = ceiling & ~floor
    GDAL_DIAGNOSTICS["monotonic_conflicts"] += int(conflicts.sum())
    result = np.where(~floor, 0, np.where(ceiling, 1, -1)).astype("int8")
    result[conflicts] = -1
    result[unknown[target_r, target_c]] = -1
    distance = np.linalg.norm(targets_xy - observer_xy, axis=1)
    result[distance > max_distance_m] = -2
    return result


def calculate(data, observers, targets, codes, weights, cell_count, engine):
    start = time.perf_counter()
    GDAL_DIAGNOSTICS["monotonic_conflicts"] = 0
    ray = direct_ray_visibility if engine == "direct" else gdal_visibility
    rr, cc = np.indices(data["ground"].shape)
    xx, yy = data["transform"] * (cc + 0.5, rr + 0.5)
    states = {k: [] for k in ("bare", "building", "joint")}
    zeros = np.zeros_like(data["ground"])
    known = np.zeros(zeros.shape, bool)
    for x, y, elevation in observers:
        clearance = np.hypot(xx - x, yy - y) <= 30
        r, c = rasterio.transform.rowcol(data["transform"], x, y)
        clearance[r, c] = True
        canopy = np.where(clearance, 0, data["canopy"])
        cu = data["canopy_unknown"] & ~clearance
        building = compose_above_ground_surface(
            zeros,
            known,
            data["building"],
            data["building_unknown"],
            qualified_water=data["qualified_water"],
            building_present=data["building_present"],
        )
        joint = compose_above_ground_surface(
            canopy,
            cu,
            data["building"],
            data["building_unknown"],
            qualified_water=data["qualified_water"],
            building_present=data["building_present"],
        )
        geometry = dict(
            transform=data["transform"],
            observer_xy=(x, y),
            targets_xy=targets,
            observer_ground_elevation_m=float(elevation),
            max_distance_m=5000,
        )
        bare_state = ray(data["ground"], zeros, known, **geometry)
        building_state = ray(data["ground"], building.lower_height, building.unknown, **geometry)
        # Floor is terrain + buildings; canopy ceiling crossings are ambiguous.
        floor = ray(data["ground"], building.lower_height, joint.unknown, **geometry)
        ceiling = ray(data["ground"], joint.lower_height, joint.unknown, **geometry)
        joint_state = np.where(
            floor == -2, -2, np.where(floor == 0, 0, np.where(ceiling == 1, 1, -1))
        )
        for key, value in (
            ("bare", bare_state),
            ("building", building_state),
            ("joint", joint_state),
        ):
            states[key].append(value)
    result = {
        key: aggregate(np.asarray(value), codes, weights, cell_count)
        for key, value in states.items()
    }
    return result, time.perf_counter() - start, dict(GDAL_DIAGNOSTICS)


def errors(result, reference):
    fields = ("lower", "upper", "unknown")
    delta = np.concatenate(
        [np.abs(result[k][f] - reference[k][f]) for k in reference for f in fields]
    )
    return dict(
        mean=float(delta.mean()), p95=float(np.quantile(delta, 0.95)), maximum=float(delta.max())
    )


def export_html(report, out):
    rows = []
    for site in report["sites"]:
        for profile in site["profiles"]:
            e = profile["errors"]
            status = (
                "Pass"
                if profile["accepted"]
                else (
                    "Uncertainty consistency failure"
                    if profile["native_diagnostics"]["monotonic_conflicts"]
                    else "Fails tolerance"
                )
            )
            rows.append(
                f'<tr><td>{html.escape(site["name"])}</td><td>{html.escape(profile["name"])}</td>'
                f'<td>{profile["median_seconds"]:.3f}</td><td>{profile["speedup"]:.1f}x</td>'
                f'<td>{100*e["p95"]:.1f}</td><td>{100*e["maximum"]:.1f}</td>'
                f"<td>{status}</td></tr>"
            )
    payload = json.dumps(report, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("cell_score_benchmark.html").read_text()
    (out / "benchmark.html").write_text(
        template.replace("__ROWS__", "".join(rows)).replace("__PAYLOAD__", payload)
    )


def run(config):
    if set(config) - {"input_dir", "output_dir", "sites", "repetitions"}:
        raise ValueError("Unknown benchmark configuration field")
    repetitions = config.get("repetitions", 3)
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        raise ValueError("Positive integer repetitions required")
    out, inputs = Path(config["output_dir"]), Path(config["input_dir"])
    out.mkdir(parents=True, exist_ok=True)
    all_rows, sites = [], []
    startup = time.perf_counter()
    for name in config["sites"]:
        prep_start = time.perf_counter()
        metadata = json.loads((inputs / (name + ".json")).read_text())
        if metadata.get("cache_contract") != "bounded_cell_surface_cache_v1" or digest(
            inputs / (name + ".npz")
        ) != metadata.get("cache_npz_sha256"):
            raise ValueError("Prepared cache checksum or contract mismatch")
        if metadata["building_inventory_policy"] != "assume_complete":
            raise ValueError(
                "This experiment requires the explicit complete-building inventory policy"
            )
        with np.load(inputs / (name + ".npz"), allow_pickle=False) as cached:
            data = {
                k: cached[k].copy()
                for k in (
                    "ground",
                    "canopy",
                    "canopy_unknown",
                    "building",
                    "building_unknown",
                    "building_present",
                    "qualified_water",
                )
            }
            data["transform"] = Affine(*cached["transform"])
            targets, target_h3 = cached["targets"].copy(), cached["target_h3"].copy()
            rows, cols = cached["target_rows"].copy(), cached["target_cols"].copy()
        cells, codes = np.unique(target_h3, return_inverse=True)
        if not len(cells):
            raise ValueError("No complete target cell populations")
        land = shape(metadata["source_land"])
        # Ground-level observer points cannot be inside modeled roof cells.
        mask = data["building_present"]
        roofs = [
            shape(g)
            for g, v in shapes(mask.astype("uint8"), mask=mask, transform=data["transform"])
            if v and shape(g).intersects(land)
        ]
        land = land.difference(unary_union(roofs)) if roofs else land
        samples = sample_points_in_source_geometry(
            metadata["site"]["source_h3"],
            land,
            15,
            geometry_crs=32610,
            projected_crs="EPSG:32610",
            max_design_points=15,
        ).to_crs(32610)
        if len(samples) != 15:
            raise ValueError("Reference requires 15 unique land observer points")
        ground_path = Path(metadata["site"]["projected_ground"])
        if digest(ground_path) != metadata["inputs"][name + "_ground"]["sha256"]:
            raise ValueError("Observer ground input checksum mismatch")
        observers = np.array(
            [
                (p.x, p.y, sample_observer_ground_elevation(ground_path, ground_path, p.x, p.y))
                for p in samples.geometry
            ]
        )
        for x, y, _ in observers:
            r, c = rasterio.transform.rowcol(data["transform"], x, y)
            assert land.covers(shape({"type": "Point", "coordinates": [x, y]})) and not mask[r, c]
        grids = {10: data, 30: coarsen(data)}
        prepared_seconds = time.perf_counter() - prep_start
        # Warm compilation/native initialization separately from measured profiles.
        warm_start = time.perf_counter()
        for engine in ("direct", "gdal"):
            calculate(data, observers[:1], targets[:2], np.zeros(2, int), np.ones(2), 1, engine)
        warmup_seconds = time.perf_counter() - warm_start
        reference_times = []
        reference = None
        for _ in range(repetitions):
            current, seconds, _ = calculate(
                data, observers, targets, codes, np.ones(len(codes)), len(cells), "direct"
            )
            if reference is not None:
                for component in reference:
                    for field in ("lower", "upper", "unknown", "out_of_range"):
                        assert np.array_equal(
                            reference[component][field], current[component][field]
                        )
            reference = current
            reference_times.append(seconds)
        ref_seconds = float(np.median(reference_times))
        profiles = []
        for profile, engine, n, pixel, stride in PROFILES:
            selected, weights = target_design(rows, cols, codes, stride, census_threshold=1000)
            elapsed = []
            result = None
            for _ in range(repetitions):
                current, seconds, diagnostics = calculate(
                    grids[pixel],
                    observers[:n],
                    targets[selected],
                    codes[selected],
                    weights,
                    len(cells),
                    engine,
                )
                if result is not None:
                    for component in result:
                        for field in ("lower", "upper", "unknown", "out_of_range"):
                            assert np.array_equal(
                                result[component][field], current[component][field]
                            )
                result = current
                elapsed.append(seconds)
            error = errors(result, reference)
            p = dict(
                name=profile,
                engine=engine,
                observers=n,
                pixel_m=pixel,
                target_stride=stride,
                small_cell_census_threshold=1000,
                evaluated_targets=len(selected),
                times_seconds=elapsed,
                median_seconds=float(np.median(elapsed)),
                errors=error,
                accepted=error["p95"] <= 0.05
                and error["maximum"] <= 0.10
                and diagnostics["monotonic_conflicts"] == 0,
                native_diagnostics=diagnostics,
                speedup=ref_seconds / float(np.median(elapsed)),
            )
            profiles.append(p)
            for component, values in result.items():
                for i, cell in enumerate(cells):
                    all_rows.append(
                        dict(
                            site=name,
                            source_type="land",
                            source_h3=metadata["site"]["source_h3"],
                            target_h3=str(cell),
                            profile=profile,
                            component=component,
                            observers=n,
                            target_population_count=int((codes == i).sum()),
                            evaluated_target_count=int((codes[selected] == i).sum()),
                            score=values["score"][i],
                            lower=float(values["lower"][i]),
                            upper=float(values["upper"][i]),
                            unknown_fraction=float(values["unknown"][i]),
                            out_of_range_fraction=float(values["out_of_range"][i]),
                        )
                    )
            print(json.dumps({"site": name, **p}), flush=True)
        for component, values in reference.items():
            for i, cell in enumerate(cells):
                all_rows.append(
                    dict(
                        site=name,
                        source_type="land",
                        source_h3=metadata["site"]["source_h3"],
                        target_h3=str(cell),
                        profile="reference",
                        component=component,
                        observers=15,
                        target_population_count=int((codes == i).sum()),
                        evaluated_target_count=int((codes == i).sum()),
                        score=values["score"][i],
                        lower=float(values["lower"][i]),
                        upper=float(values["upper"][i]),
                        unknown_fraction=float(values["unknown"][i]),
                        out_of_range_fraction=float(values["out_of_range"][i]),
                    )
                )
        exact_x, exact_y = Transformer.from_crs(4326, 32610, always_xy=True).transform(
            metadata["site"]["lon"], metadata["site"]["lat"]
        )
        exact_ground = sample_observer_ground_elevation(ground_path, ground_path, exact_x, exact_y)
        exact_result, exact_seconds, _ = calculate(
            data,
            np.array([[exact_x, exact_y, exact_ground]]),
            targets,
            codes,
            np.ones(len(codes)),
            len(cells),
            "direct",
        )
        for component, values in exact_result.items():
            for i, cell in enumerate(cells):
                all_rows.append(
                    dict(
                        site=name,
                        source_type="land",
                        source_h3=metadata["site"]["source_h3"],
                        target_h3=str(cell),
                        profile="exact_point",
                        component=component,
                        observers=1,
                        target_population_count=int((codes == i).sum()),
                        evaluated_target_count=int((codes == i).sum()),
                        score=values["score"][i],
                        lower=float(values["lower"][i]),
                        upper=float(values["upper"][i]),
                        unknown_fraction=float(values["unknown"][i]),
                        out_of_range_fraction=float(values["out_of_range"][i]),
                    )
                )
        candidates = [p for p in profiles if p["accepted"]]
        best = min(candidates, key=lambda p: p["median_seconds"]) if candidates else None
        sites.append(
            dict(
                name=name,
                source_h3=metadata["site"]["source_h3"],
                metadata=metadata,
                observers=observers.tolist(),
                preparation_seconds=prepared_seconds,
                warmup_seconds=warmup_seconds,
                reference_seconds=ref_seconds,
                reference_times_seconds=reference_times,
                target_count=len(codes),
                target_cells=cells.tolist(),
                profiles=profiles,
                recommended=best["name"] if best else "reference",
                exact_point=[exact_x, exact_y, exact_ground],
                exact_point_seconds=exact_seconds,
                target_polygons={
                    str(cell): [
                        list(Transformer.from_crs(4326, 32610, always_xy=True).transform(lon, lat))
                        for lat, lon in h3.cell_to_boundary(cell)
                    ]
                    for cell in cells
                },
            )
        )
    table = pl.DataFrame(all_rows)
    identity = ["site", "source_type", "source_h3", "target_h3", "profile", "component"]
    assert (
        table.unique(identity).height == table.height
        and not table.select(identity).null_count().to_numpy().any()
    )
    table.sort(identity).write_parquet(out / "cell_scores.parquet")
    # Fresh disk lookup timing includes reading the small table and selecting one source.
    lookup = []
    for _ in range(5):
        then = time.perf_counter()
        t = pl.read_parquet(out / "cell_scores.parquet").filter(
            pl.col("source_h3") == sites[0]["source_h3"]
        )
        assert t.height
        lookup.append(time.perf_counter() - then)
    common = [
        p[0]
        for p in PROFILES
        if all(next(v for v in s["profiles"] if v["name"] == p[0])["accepted"] for s in sites)
    ]
    common_best = (
        min(
            common,
            key=lambda name: sum(
                next(v for v in s["profiles"] if v["name"] == name)["median_seconds"] for s in sites
            ),
        )
        if common
        else "reference"
    )
    selected = table.filter(pl.col("profile") == common_best)
    keys = ["site", "source_type", "source_h3", "target_h3"]
    lookup_table = (
        selected.filter(pl.col("component") == "building")
        .select(
            *keys,
            "profile",
            "observers",
            "target_population_count",
            pl.col("score").alias("terrain_building_score"),
            pl.col("lower").alias("terrain_building_lower"),
            pl.col("upper").alias("terrain_building_upper"),
        )
        .join(
            selected.filter(pl.col("component") == "joint").select(
                *keys,
                pl.col("score").alias("combined_score"),
                pl.col("lower").alias("combined_lower"),
                pl.col("upper").alias("combined_upper"),
                pl.col("unknown_fraction").alias("combined_unknown_fraction"),
                "out_of_range_fraction",
            ),
            on=keys,
            how="inner",
            validate="1:1",
        )
    )
    assert lookup_table.height == sum(len(s["target_cells"]) for s in sites)
    assert (
        lookup_table.unique(["source_type", "source_h3", "target_h3"]).height == lookup_table.height
    )
    lookup_table.sort(keys).write_parquet(out / "view_scores.parquet")
    report = dict(
        contract=CONTRACT,
        sites=sites,
        config=config,
        repetitions=repetitions,
        recommended_common_profile=common_best,
        lookup_contract="one row per land source_h3/target_h3; conditional terrain_building score plus combined canopy bounds; research retained raster denominator",
        runtime_scope="serial warm CPU calculation including private surface construction and cell aggregation; excludes acquisition, source-cache preparation and JIT startup",
        reference="15 deterministic outdoor land observers; 10m direct grid rays; every retained water pixel in complete target cells; 30m terrain support",
        score_definition="mean visible target-water pixel fraction across ground-level outdoor source-cell observers; range-excluded pairs are zero; unknown retains bounds and null complete score",
        tolerance="p95 absolute difference <=0.05 and maximum <=0.10 across lower, upper and unknown fraction for all components, and no native height-monotonic conflicts; exploratory acceptance against model reference",
        limits="two source cells, 5km diagnostic window, partial regional candidate universe; pre-existing on-disk compiler cache, cold startup not measured; no calibrated canopy gaps, new terrain precision, production promotion or field acceptance",
        environment=dict(
            python=sys.version, machine=platform.machine(), platform=platform.platform()
        ),
        input_checksums={
            name: {ext: digest(inputs / (name + "." + ext)) for ext in ("npz", "json")}
            for name in config["sites"]
        },
        lookup_seconds=lookup,
        median_disk_lookup_seconds=float(np.median(lookup)),
        total_benchmark_seconds=time.perf_counter() - startup,
        pairs=table.to_dicts(),
    )
    (out / "benchmark.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    export_html(report, out)
    return report


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
