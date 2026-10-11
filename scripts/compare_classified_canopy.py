"""Bounded offline WorldCover/CHM trial on the saved cell-score populations.

Usage: PYTHONPATH=src python scripts/compare_classified_canopy.py CONFIG.json
No acquisition, cleanup, provider registration or production promotion.
"""

import base64
import hashlib
import inspect
import io
import json
import math
import platform
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl
import rasterio
from affine import Affine
from benchmark_cell_scores import digest, target_design
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit.pipeline.prepare.elevation.obstruction import compose_above_ground_surface
from viewshed_toolkit.pipeline.weights.vegetation.classified import (
    CLASSIFIED_CANOPY_CONTRACT,
    DEFAULT_BETA_PER_M,
    HEIGHT_BREAKS_M,
    HEIGHT_OBSTRUCTION,
    classified_ray_support,
)
from viewshed_toolkit.pipeline.weights.vegetation.defaults import ESA_WORLDCOVER_CLASS_WEIGHTS

CONTRACT = "bounded_worldcover_canopy_trial_v1"
COLORS = {
    0: "#ad9abc",
    10: "#35734e",
    20: "#9a9b4d",
    30: "#c4d08a",
    40: "#dfbb67",
    50: "#bf7868",
    60: "#d6ccba",
    70: "#fafafa",
    80: "#b1d5e1",
    90: "#78a89b",
    95: "#20715a",
    100: "#b7bc8b",
}


def aggregate_support(values, codes, weights, cell_count):
    """Full-population transmission bounds; no available-only renormalization."""
    if (
        values.ndim != 2
        or values.shape[1] != len(codes)
        or len(codes) != len(weights)
        or not np.isfinite(values).all()
        or (weights <= 0).any()
        or not ((values == -1) | (values == -2) | ((values >= 0) & (values <= 1))).all()
    ):
        raise ValueError("Aligned valid transmission states and positive weights required")
    total = np.bincount(codes, weights=weights, minlength=cell_count) * len(values)
    if (total <= 0).any():
        raise ValueError("Every target cell requires positive observer/target support")
    unknown = np.bincount(codes, weights=(values == -1).sum(0) * weights, minlength=cell_count)
    summed = np.bincount(
        codes, weights=np.maximum(values, 0).sum(0) * weights, minlength=cell_count
    )
    lower, upper = summed / total, (summed + unknown) / total
    return dict(
        score=[None if n else float(v) for v, n in zip(lower, unknown, strict=True)],
        lower=lower,
        upper=upper,
        unknown=unknown / total,
        out_of_range=np.bincount(
            codes, weights=(values == -2).sum(0) * weights, minlength=cell_count
        )
        / total,
    )


def load_worldcover(assets, shape, transform):
    """Nearest categorical alignment of verified retained clips; holes remain 0."""
    result = np.zeros(shape, dtype="uint8")
    for asset in assets:
        with rasterio.open(asset["path"]) as src:
            if src.count != 1 or src.crs is None or src.nodata != 0:
                raise ValueError("Single-band georeferenced WorldCover with nodata0 required")
            with WarpedVRT(
                src,
                crs="EPSG:32610",
                transform=transform,
                width=shape[1],
                height=shape[0],
                resampling=Resampling.nearest,
                src_nodata=0,
                nodata=0,
                warp_mem_limit=32,
            ) as vrt:
                layer = vrt.read(1)
        if ((result != 0) & (layer != 0) & (result != layer)).any():
            raise ValueError("Conflicting overlapping WorldCover classifications")
        result[layer != 0] = layer[layer != 0]
    return result


def raster_image(classes):
    pixels = np.zeros((*classes.shape, 3), dtype="uint8")
    for code, color in COLORS.items():
        pixels[classes == code] = tuple(bytes.fromhex(color[1:]))
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def calculate(data, classes, observers, targets, codes, weights, cells, betas, water_policy):
    started = time.perf_counter()
    ground = data["ground"]
    rr, cc = np.indices(ground.shape)
    xx, yy = data["transform"] * (cc + 0.5, rr + 0.5)
    known = np.zeros(ground.shape, bool)
    building = compose_above_ground_surface(
        np.zeros_like(ground),
        known,
        data["building"],
        data["building_unknown"],
        qualified_water=data["qualified_water"],
        building_present=data["building_present"],
    )
    water = building.qualified_water
    canopy = np.where(water, 0, data["canopy"])
    cu = data["canopy_unknown"] & ~water
    values = []
    for x, y, elevation in observers:
        clearance = np.hypot(xx - x, yy - y) <= 30
        row, col = rasterio.transform.rowcol(data["transform"], x, y)
        clearance[row, col] = True
        values.append(
            classified_ray_support(
                ground,
                canopy,
                cu,
                classes,
                building.lower_height,
                building.unknown,
                transform=data["transform"],
                observer_xy=(x, y),
                targets_xy=targets,
                observer_ground_elevation_m=float(elevation),
                beta_per_m=DEFAULT_BETA_PER_M,
                water_class_policy=water_policy,
                canopy_clearance_mask=clearance,
                max_distance_m=5000,
            )
        )
    original = np.asarray(values)
    results = {}
    # One ray evaluation supplies all beta scenarios; exponentiation is exact
    # within the bounded5km/default-beta domain (no optical-depth underflow).
    for beta in betas:
        scenario = original.copy()
        positive = scenario > 0
        scenario[positive] = scenario[positive] ** (beta / DEFAULT_BETA_PER_M)
        results[beta] = aggregate_support(scenario, codes, weights, len(cells))
    return results, time.perf_counter() - started


def run(config):
    fields = {
        "benchmark_report",
        "worldcover_manifest",
        "output_dir",
        "beta_per_m",
        "repetitions",
        "water_class_policy",
    }
    if set(config) - fields:
        raise ValueError("Unknown classified-canopy configuration field")
    betas = config.get("beta_per_m", [0.001, 0.002, 0.004])
    if (
        not isinstance(betas, list)
        or not betas
        or len(set(betas)) != len(betas)
        or DEFAULT_BETA_PER_M not in betas
        or any(
            isinstance(b, bool) or not isinstance(b, (int, float)) or not math.isfinite(b) or b <= 0
            for b in betas
        )
    ):
        raise ValueError("Unique positive finite beta scenarios including0.002 required")
    repetitions = config.get("repetitions", 3)
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("Positive integer repetitions required")
    water_policy = config.get("water_class_policy", "zero_if_missing")
    if water_policy not in ("retain_unknown", "zero_if_missing"):
        raise ValueError("Invalid WorldCover water-class policy")
    benchmark_path, wc_path = Path(config["benchmark_report"]), Path(config["worldcover_manifest"])
    benchmark, wc = json.loads(benchmark_path.read_text()), json.loads(wc_path.read_text())
    if benchmark.get("contract") != "bounded_cell_score_benchmark_v1":
        raise ValueError("Retained cell-score benchmark contract required")
    if wc.get("status") != "completed_diagnostic_only" or not wc.get("assets"):
        raise ValueError("Completed retained WorldCover inventory required")
    for asset in wc["assets"]:
        if digest(asset["path"]) != asset["sha256"]:
            raise ValueError("WorldCover source checksum mismatch")
    out = Path(config["output_dir"])
    if out.resolve() in {benchmark_path.parent.resolve(), wc_path.parent.resolve()}:
        raise ValueError("Use a separate classified-canopy output directory")
    out.mkdir(parents=True, exist_ok=True)
    inputs = Path(benchmark["config"]["input_dir"])
    rows, sites = [], []
    for site in benchmark["sites"]:
        prep_start = time.perf_counter()
        name = site["name"]
        hashes = benchmark["input_checksums"][name]
        if any(digest(inputs / f"{name}.{ext}") != hashes[ext] for ext in ("npz", "json")):
            raise ValueError("Benchmark prepared cache checksum mismatch")
        metadata = json.loads((inputs / f"{name}.json").read_text())
        if (
            metadata.get("cache_contract") != "bounded_cell_surface_cache_v1"
            or metadata["cache_npz_sha256"] != hashes["npz"]
            or metadata["building_inventory_policy"] != "assume_complete"
        ):
            raise ValueError("Qualified prepared cache and complete building policy required")
        with np.load(inputs / f"{name}.npz", allow_pickle=False) as cached:
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
            target_rows, target_cols = cached["target_rows"].copy(), cached["target_cols"].copy()
        cells, codes = np.unique(target_h3, return_inverse=True)
        if cells.tolist() != site["target_cells"]:
            raise ValueError("Retained target population mismatch")
        classes = load_worldcover(wc["assets"], data["ground"].shape, data["transform"])
        observers = np.asarray(site["observers"])
        selected, weights = target_design(target_rows, target_cols, codes, 2, census_threshold=1000)
        support = rasterize(
            [(metadata["source_land"], 1)],
            out_shape=classes.shape,
            transform=data["transform"],
            dtype="uint8",
        ).astype(bool)
        support &= ~data["building_present"]
        unique, counts = np.unique(classes[support], return_counts=True)
        class_counts = {str(k): int(v) for k, v in zip(unique, counts, strict=True)}
        preparation = time.perf_counter() - prep_start
        warm_start = time.perf_counter()
        calculate(
            data,
            classes,
            observers[:1],
            targets[:1],
            np.zeros(1, int),
            np.ones(1),
            cells[:1],
            betas,
            water_policy,
        )
        warmup = time.perf_counter() - warm_start
        profiles = {}
        for profile, points, index, represented, policy in (
            ("reference", observers, np.arange(len(targets)), np.ones(len(targets)), water_policy),
            ("fast", observers, selected, weights, water_policy),
            ("fast_strict", observers, selected, weights, "retain_unknown"),
            (
                "exact_point",
                np.asarray([site["exact_point"]]),
                np.arange(len(targets)),
                np.ones(len(targets)),
                water_policy,
            ),
        ):
            times, result = [], None
            for _ in range(repetitions):
                current, seconds = calculate(
                    data,
                    classes,
                    points,
                    targets[index],
                    codes[index],
                    represented,
                    cells,
                    betas,
                    policy,
                )
                if result is not None:
                    for beta in betas:
                        for field in ("lower", "upper", "unknown", "out_of_range"):
                            np.testing.assert_array_equal(result[beta][field], current[beta][field])
                result = current
                times.append(seconds)
            profiles[profile] = dict(
                times_seconds=times,
                median_seconds=float(np.median(times)),
                observer_count=len(points),
                evaluated_targets=len(index),
            )
            baseline_profile = (
                "direct_15_10m_stride2" if profile in ("fast", "fast_strict") else profile
            )
            baseline = [
                r
                for r in benchmark["pairs"]
                if r["site"] == name and r["profile"] == baseline_profile
            ]
            for beta, values in result.items():
                for i, cell in enumerate(cells):
                    building = next(
                        r
                        for r in baseline
                        if r["target_h3"] == cell and r["component"] == "building"
                    )
                    envelope = next(
                        r for r in baseline if r["target_h3"] == cell and r["component"] == "joint"
                    )
                    lower, upper = float(values["lower"][i]), float(values["upper"][i])
                    if lower + 1e-10 < envelope["lower"] or upper > envelope["upper"] + 1e-10:
                        raise RuntimeError(
                            "Transmission scenario falls outside retained canopy envelope"
                        )
                    rows.append(
                        dict(
                            site=name,
                            source_type="land",
                            source_h3=site["source_h3"],
                            target_h3=str(cell),
                            profile=profile,
                            beta_per_m=beta,
                            model_contract=CLASSIFIED_CANOPY_CONTRACT,
                            water_class_policy=policy,
                            observer_count=len(points),
                            target_population_count=int((codes == i).sum()),
                            evaluated_target_count=int((codes[index] == i).sum()),
                            terrain_building_score=building["score"],
                            terrain_building_lower=building["lower"],
                            terrain_building_upper=building["upper"],
                            canopy_envelope_lower=envelope["lower"],
                            canopy_envelope_upper=envelope["upper"],
                            canopy_envelope_unknown_fraction=envelope["unknown_fraction"],
                            modeled_score=values["score"][i],
                            modeled_lower=lower,
                            modeled_upper=upper,
                            input_unknown_fraction=float(values["unknown"][i]),
                            out_of_range_fraction=float(values["out_of_range"][i]),
                        )
                    )
        error_fields = ("modeled_lower", "modeled_upper", "input_unknown_fraction")
        site_rows = [r for r in rows if r["site"] == name]
        reference = {
            (r["target_h3"], r["beta_per_m"]): r for r in site_rows if r["profile"] == "reference"
        }
        deltas = [
            abs(r[f] - reference[(r["target_h3"], r["beta_per_m"])][f])
            for r in site_rows
            if r["profile"] == "fast"
            for f in error_fields
        ]
        profiles["fast"]["errors"] = dict(maximum=max(deltas), p95=float(np.quantile(deltas, 0.95)))
        profiles["fast"]["accepted"] = bool(
            max(deltas) <= 0.10 and np.quantile(deltas, 0.95) <= 0.05
        )
        observer_classes = []
        for x, y, z in observers:
            r, c = rasterio.transform.rowcol(data["transform"], x, y)
            observer_classes.append(
                dict(
                    x=x,
                    y=y,
                    ground_m=z,
                    worldcover=int(classes[r, c]),
                    canopy_m=float(data["canopy"][r, c]),
                    canopy_missing=bool(data["canopy_unknown"][r, c]),
                )
            )
        sites.append(
            dict(
                name=name,
                source_h3=site["source_h3"],
                observers=site["observers"],
                exact_point=site["exact_point"],
                observer_classes=observer_classes,
                metadata=metadata,
                target_polygons=site["target_polygons"],
                profiles=profiles,
                preparation_seconds=preparation,
                warmup_seconds=warmup,
                source_class_counts=class_counts,
                missing_height_class_counts={
                    str(k): int(v)
                    for k, v in zip(
                        *np.unique(classes[data["canopy_unknown"]], return_counts=True), strict=True
                    )
                },
                water_class_resolved_pixels=(
                    int(((classes == 80) & (data["canopy"] == 0) & data["canopy_unknown"]).sum())
                    if water_policy == "zero_if_missing"
                    else 0
                ),
                worldcover_png=raster_image(classes),
                grid=dict(
                    shape=list(classes.shape),
                    transform=list(data["transform"])[:6],
                    crs="EPSG:32610",
                ),
            )
        )
        print(json.dumps({"site": name, "profiles": profiles}), flush=True)
    table = pl.DataFrame(rows)
    keys = ["source_type", "source_h3", "target_h3", "profile", "beta_per_m"]
    if (
        table.unique(keys).height != table.height
        or table.select(keys).null_count().to_numpy().any()
    ):
        raise ValueError("Unique nonnull source/target/profile/scenario keys required")
    selected_profile = (
        "fast" if all(s["profiles"]["fast"]["accepted"] for s in sites) else "reference"
    )
    atomic_sink_parquet(
        table.lazy().sort(keys), out / "worldcover_canopy_scores.parquet", overwrite=True
    )
    lookup = table.filter(
        (pl.col("profile") == selected_profile) & (pl.col("beta_per_m") == DEFAULT_BETA_PER_M)
    )
    if lookup.unique(keys[:3]).height != lookup.height:
        raise ValueError("Lookup pair identity mismatch")
    atomic_sink_parquet(
        lookup.lazy().sort(keys[:3]), out / "worldcover_view_scores.parquet", overwrite=True
    )
    lookup_times = []
    for _ in range(5):
        started = time.perf_counter()
        pl.read_parquet(out / "worldcover_view_scores.parquet").filter(
            pl.col("source_h3") == sites[0]["source_h3"]
        )
        lookup_times.append(time.perf_counter() - started)
    model = dict(
        contract=CLASSIFIED_CANOPY_CONTRACT,
        height_breaks_m=HEIGHT_BREAKS_M,
        height_obstruction=HEIGHT_OBSTRUCTION,
        beta_per_m=betas,
        default_beta_per_m=DEFAULT_BETA_PER_M,
        water_class_policy=water_policy,
        classes={
            str(k): {
                field: v[field]
                for field in ("class_name", "obstruction_multiplier", "obstruction_floor")
            }
            for k, v in ESA_WORLDCOVER_CLASS_WEIGHTS.items()
        },
        equation="T=exp(-beta*sum(class-height coefficient * ray metres below CHM top)); averaged over all observer/target comparisons",
        limits="unvalidated ground-to-canopy-top attenuation columns; coefficients, beta and optional zero vegetation on WorldCover water with missing zero-floor canopy are assumptions; positive-height water conflicts and missing land canopy remain unknown; no access weights, no distance attenuation",
    )
    report = dict(
        contract=CONTRACT,
        config=config,
        model=model,
        sites=sites,
        pairs=table.to_dicts(),
        selected_profile=selected_profile,
        worldcover=dict(
            product="ESA WorldCover2020v100",
            year=2020,
            license="CC BY 4.0",
            attribution="© ESA WorldCover project 2020 / Contains modified Copernicus Sentinel data (2020) processed by ESA WorldCover consortium",
            documentation="https://esa-worldcover.org/en/data-access",
            assets=wc["assets"],
            manifest_sha256=digest(wc_path),
            alignment="nearest categorical onto retained10mEPSG32610; missing remains0",
        ),
        benchmark_sha256=digest(benchmark_path),
        prepared_checksums=benchmark["input_checksums"],
        median_disk_lookup_seconds=float(np.median(lookup_times)),
        environment=dict(python=sys.version, platform=platform.platform()),
        validation_scope="two source cells,18 complete retained raster-water target cells within5km; fixed15observer design; modeled transmission, not empirical viewability; canonical production unchanged",
    )
    report["code_checksums"] = {
        "classified.py": digest(inspect.getfile(classified_ray_support)),
        "compare_classified_canopy.py": digest(__file__),
        "classified_canopy_report.html.in": digest(
            Path(__file__).with_name("classified_canopy_report.html.in")
        ),
    }
    signature = {
        key: report[key]
        for key in (
            "model",
            "worldcover",
            "benchmark_sha256",
            "prepared_checksums",
            "code_checksums",
        )
    }
    report["scientific_signature"] = hashlib.sha256(
        json.dumps(signature, sort_keys=True).encode()
    ).hexdigest()
    atomic_write_json(out / "report.json", report, overwrite=True)
    payload = json.dumps(report, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("classified_canopy_report.html.in").read_text()
    temporary = out / "report.html.tmp"
    temporary.write_text(template.replace("__PAYLOAD__", payload))
    temporary.replace(out / "report.html")
    return report


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
