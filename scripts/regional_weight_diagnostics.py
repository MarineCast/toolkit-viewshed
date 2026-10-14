"""Add inspectable weight diagnostics to a completed retained research pilot.

Usage: PYTHONPATH=src python scripts/regional_weight_diagnostics.py CONFIG.json
Config: pilot, output_dir, distance_config, workers, memory_budget_gib.
Read-only retained arrays, exact saved observers and target sampling. No acquisition,
canonical pipeline, overwrite, cleanup or modification of the original scores.
"""

import gc
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import polars as pl
import rasterio
from affine import Affine
from benchmark_cell_scores import digest, target_design
from map_average_viewability import target_population
from parallel_viewability_demo import run_jobs
from pyproj import Transformer
from regional_viewability_pilot import RADIUS, load_cache, source_window
from shapely.geometry import shape

from viewshed_toolkit import direct_ray_visibility
from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet
from viewshed_toolkit.pipeline.config.distance import load_distance_weight_config
from viewshed_toolkit.pipeline.config.schema import load_yaml_config
from viewshed_toolkit.pipeline.prepare.area.lookup import _haversine_expr
from viewshed_toolkit.pipeline.prepare.elevation.obstruction import compose_above_ground_surface
from viewshed_toolkit.pipeline.weights.distance.compute import (
    distance_weight_expr,
    distance_weight_values,
)
from viewshed_toolkit.pipeline.weights.transmission import reduce_weights
from viewshed_toolkit.pipeline.weights.vegetation.classified import classified_ray_support

CONTRACT = "retained_regional_weight_diagnostics_v1"


def calculate(data, classes, observers, targets, codes, weights, cells, parent, cfg):
    ground = data["ground"]
    buildings = compose_above_ground_surface(
        np.zeros_like(ground),
        np.zeros(ground.shape, bool),
        data["building"],
        data["building_unknown"],
        qualified_water=data["qualified_water"],
        building_present=data["building_present"],
    )
    canopy = np.where(buildings.qualified_water, 0, data["canopy"])
    unknown = data["canopy_unknown"] & ~buildings.qualified_water
    bare = np.empty((len(observers), len(targets)), dtype="int8")
    joint = np.empty(bare.shape, dtype="float64")
    decay = np.empty(bare.shape, dtype="float32")
    zero = np.zeros_like(ground)
    known = np.zeros(ground.shape, bool)
    # A saved zero upper bound proves every sampled ray in that cell blocked.
    # Skip only those joint rays; terrain is independently evaluated everywhere.
    positive = np.array([parent[str(cell)]["upper"] > 0 for cell in cells])[codes]
    for i, (x, y, elevation) in enumerate(observers):
        bare[i] = direct_ray_visibility(
            ground,
            zero,
            known,
            transform=data["transform"],
            observer_xy=(x, y),
            targets_xy=targets,
            observer_ground_elevation_m=float(elevation),
            max_distance_m=RADIUS,
        )
        joint[i] = np.where(bare[i] == -2, -2, 0)
        indices = np.flatnonzero((bare[i] == 1) & positive)
        if len(indices):
            clearance = np.zeros(ground.shape, bool)
            row, col = rasterio.transform.rowcol(data["transform"], x, y)
            r0, r1, c0, c1 = max(0, row - 4), row + 5, max(0, col - 4), col + 5
            rr, cc = np.mgrid[r0:r1, c0:c1]
            xx, yy = data["transform"] * (cc + 0.5, rr + 0.5)
            clearance[r0:r1, c0:c1] = np.hypot(xx - x, yy - y) <= 30
            clearance[row, col] = True
            joint[i, indices] = classified_ray_support(
                ground,
                canopy,
                unknown,
                classes,
                buildings.lower_height,
                buildings.unknown,
                transform=data["transform"],
                observer_xy=(x, y),
                targets_xy=targets[indices],
                observer_ground_elevation_m=float(elevation),
                water_class_policy="zero_if_missing",
                canopy_clearance_mask=clearance,
                max_distance_m=RADIUS,
            )
        decay[i] = distance_weight_values(
            np.hypot(targets[:, 0] - x, targets[:, 1] - y) / 1000, cfg, RADIUS / 1000
        )
    return reduce_weights(bare, joint, decay, codes, weights, len(cells))


def worker(config, index):
    pilot, out = Path(config["pilot"]), Path(config["output_dir"])
    metadata = json.loads((pilot / "prepared.json").read_text())
    runtime = json.loads((pilot / "runtime.json").read_text())
    sources = json.loads((pilot / "source_cells.json").read_text())
    infos = {s["source_h3"]: s for s in runtime["sources"]}
    water = gpd.read_parquet(pilot / "water_area.parquet").geometry.iloc[0]
    to_w = Transformer.from_crs(32610, 4326, always_xy=True)
    cfg = load_distance_weight_config(load_yaml_config(config["distance_config"]))
    for ordinal in range(index, len(sources), config["workers"]):
        started = time.perf_counter()
        source = sources[ordinal]
        sid = source["source_h3"]
        arrays = load_cache(pilot, metadata)
        data = source_window(arrays, Affine(*metadata["transform"]), shape(source["source_land"]))
        arrays.clear()
        classes = data.pop("classes")
        observers = np.asarray(infos[sid]["observers"])
        targets, rr, cc, inside = target_population(
            water, data["transform"], data["ground"].shape, observers, max_distance_m=RADIUS
        )
        if not inside.all():
            raise ValueError("Incomplete retained raster coverage")
        lon, lat = to_w.transform(targets[:, 0], targets[:, 1])
        cells, codes = np.unique(
            np.array([h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)]),
            return_inverse=True,
        )
        parent = {
            p["target_h3"]: p
            for p in pl.read_parquet(pilot / "partitions" / f"{sid}.parquet").to_dicts()
        }
        counts = np.bincount(codes, minlength=len(cells))
        selected, weights = target_design(rr, cc, codes, 2, census_threshold=1000)
        if not infos[sid]["sampled_result_used"]:
            raise ValueError("Only accepted stride2 pilot supported")
        if set(parent) != set(cells) or any(
            parent[str(c)]["target_water_pixel_count"] != int(counts[i])
            or parent[str(c)]["sampled_target_points"] != int((codes[selected] == i).sum())
            for i, c in enumerate(cells)
        ):
            raise ValueError("Target sampling differs from parent")
        result = calculate(
            data,
            classes,
            observers,
            targets[selected],
            codes[selected],
            weights,
            cells,
            parent,
            cfg,
        )
        rows = [
            dict(source_type="land", source_h3=sid, target_h3=str(c), **result[i])
            for i, c in enumerate(cells)
        ]
        frame = pl.DataFrame(
            rows,
            schema_overrides={
                k: pl.Float64
                for k in ["above_ground_lower", "above_ground_upper", "weight_above_ground"]
            },
        )
        centers = [h3.cell_to_latlng(str(c)) for c in cells]
        slat, slon = h3.cell_to_latlng(sid)
        distances = pl.DataFrame(
            dict(
                source_lat=[slat] * len(cells),
                source_lon=[slon] * len(cells),
                target_lat=[c[0] for c in centers],
                target_lon=[c[1] for c in centers],
            )
        ).select(
            _haversine_expr("source_lat", "source_lon", "target_lat", "target_lon")
            .cast(pl.Float32)
            .alias("distance_km")
        )
        # Canonical centroid profile retains its configured 30km cutoff. Actual
        # terrain comparisons retain the pilot's stricter 15-mile observer reach.
        frame = frame.hstack(distances).with_columns(distance_weight_expr(cfg, cfg.hard_cutoff_km))
        atomic_sink_parquet(frame.lazy(), out / "partitions" / f"{sid}.parquet", overwrite=False)
        print(f"{ordinal+1}/{len(sources)} {sid}: {time.perf_counter()-started:.1f}s", flush=True)
        del data, classes, targets, result, frame, arrays
        gc.collect()


def run(config):
    required = {"pilot", "output_dir", "distance_config", "workers", "memory_budget_gib"}
    if set(config) != required:
        raise ValueError("Expected pilot, output_dir, distance_config, workers, memory_budget_gib")
    pilot, out = Path(config["pilot"]), Path(config["output_dir"])
    if out.exists():
        raise ValueError("Use a new output directory")
    runtime = json.loads((pilot / "runtime.json").read_text())
    if (
        runtime["contract"] != "bounded_regional_classified_viewability_pilot_v1"
        or digest(pilot / "average_viewability.parquet") != runtime["output_sha256"]
    ):
        raise ValueError("Invalid parent pilot")
    distance_path = Path(config["distance_config"])
    distance_bytes = distance_path.read_bytes()
    distance_sha256 = hashlib.sha256(distance_bytes).hexdigest()
    out.mkdir(parents=True)
    (out / "partitions").mkdir()
    snapshot = out / "distance-config.yaml"
    snapshot.write_bytes(distance_bytes)
    cfg = load_distance_weight_config(load_yaml_config(snapshot))
    worker_config = dict(config, distance_config=str(snapshot.resolve()))
    inputs = {
        str(p.relative_to(pilot)): digest(p)
        for p in [
            pilot / "prepared.json",
            pilot / "runtime.json",
            pilot / "source_cells.json",
            pilot / "water_area.parquet",
            *sorted((pilot / "prepared").glob("*.npy")),
        ]
    }
    code_paths = [
        Path(__file__),
        Path(__file__).parent / "regional_viewability_pilot.py",
        Path(__file__).parent / "benchmark_cell_scores.py",
        Path(__file__).parent / "map_average_viewability.py",
        Path(__file__).parents[1] / "src/viewshed_toolkit/pipeline/weights/rays.py",
        Path(__file__).parents[1]
        / "src/viewshed_toolkit/pipeline/weights/vegetation/classified.py",
        Path(__file__).parents[1] / "src/viewshed_toolkit/pipeline/weights/distance/compute.py",
    ]
    code_paths.append(
        Path(__file__).parents[1] / "src/viewshed_toolkit/pipeline/weights/transmission.py"
    )
    code_hashes = {str(p.relative_to(Path(__file__).parents[1])): digest(p) for p in code_paths}
    atomic_write_json(out / "config.json", worker_config, overwrite=False)
    started = time.perf_counter()
    execution = run_jobs(
        [
            dict(name=f"weights-{i}", index=i, log=str(out / f"worker-{i}.log"))
            for i in range(config["workers"])
        ],
        config["workers"],
        int(config["memory_budget_gib"] * 1024**3),
        lambda j: [sys.executable, __file__, str(out / "config.json"), "worker", str(j["index"])],
    )
    table = pl.concat(
        [pl.read_parquet(p) for p in sorted((out / "partitions").glob("*.parquet"))]
    ).sort(["source_type", "source_h3", "target_h3"])
    keys = ["source_type", "source_h3", "target_h3"]
    parent = pl.read_parquet(pilot / "average_viewability.parquet").select(keys).sort(keys)
    if not table.select(keys).equals(parent) or table.unique(keys).height != table.height:
        raise ValueError("Diagnostic pair universe differs from parent")
    if digest(distance_path) != distance_sha256 or digest(snapshot) != distance_sha256:
        raise ValueError("Distance configuration changed during diagnostic run")
    if any(digest(pilot / name) != sha for name, sha in inputs.items()) or any(
        digest(Path(__file__).parents[1] / name) != sha for name, sha in code_hashes.items()
    ):
        raise ValueError("Inputs or implementation changed during diagnostic run")
    atomic_sink_parquet(table.lazy(), out / "weights.parquet", overwrite=False)
    atomic_write_json(
        out / "manifest.json",
        dict(
            contract=CONTRACT,
            parent_sha256=runtime["output_sha256"],
            output_sha256=digest(out / "weights.parquet"),
            pair_count=table.height,
            inputs=inputs,
            code_hashes=code_hashes,
            distance=asdict(cfg),
            distance_config_sha256=distance_sha256,
            code_sha256=digest(Path(__file__)),
            elapsed_seconds=time.perf_counter() - started,
            execution=execution,
            definitions=dict(
                weight_terrain="mean(bare_LOS * observer_pixel_decay); pilot in-reach portion denominator",
                weight_above_ground="conditional distance-weighted roof/WorldCover/CHM transmission; null if no terrain-clear support or unknown inputs",
                weight_distance="canonical H3 centroid profile diagnostic; never multiply into terrain again",
                target_mean="terrain/distance: all-source mean with absent pairs zero; above-ground: mean over terrain-clear source pairs, unknowns retain null",
            ),
        ),
        overwrite=False,
    )


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    if len(sys.argv) > 2:
        worker(config, int(sys.argv[3]))
    else:
        run(config)
