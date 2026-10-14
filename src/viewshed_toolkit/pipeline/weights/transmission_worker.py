"""Source-window execution for the supported transmission workflow."""

import gc
import json
import os
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import h3
import numpy as np
import polars as pl
from affine import Affine
from pyproj import Transformer
from shapely.geometry import shape

from viewshed_toolkit._internal.artifacts.contracts import atomic_write_json
from viewshed_toolkit._internal.data.parquet import atomic_sink_parquet

from ..config.distance import load_distance_weight_config
from ..contracts.transmission import TRANSMISSION_PAIR_SCHEMA
from ..prepare.area.lookup import _haversine_expr
from ..prepare.area.transmission import target_design, target_population
from ..prepare.transmission import load_cache, observer_design, source_window
from .distance.compute import distance_weight_expr
from .transmission import calculate


def worker(config: Any, index: Any) -> Any:
    out = Path(config["output_dir"])
    distance = load_distance_weight_config({"distance_weight": config["distance_weight"]})
    metadata = json.loads((out / "prepared.json").read_text())
    grid = Affine(*metadata["transform"])
    water = gpd.read_parquet(out / "water_area.parquet").geometry.iloc[0]
    sources = json.loads((out / "source_cells.json").read_text())
    # Evenly spaced sources cover the whole box; 12 census checks do not imply
    # empirical calibration or a per-source accuracy guarantee for the remainder.
    references = set(
        np.linspace(0, len(sources) - 1, min(config["census_sources"], len(sources)), dtype=int)
    )
    folder = out / "partitions"
    folder.mkdir(exist_ok=True)
    to_w = Transformer.from_crs(32610, 4326, always_xy=True)
    for ordinal in range(index, len(sources), config["workers"]):
        source = sources[ordinal]
        started = time.perf_counter()
        arrays = load_cache(out, metadata)
        data = source_window(
            arrays, grid, shape(source["source_land"]), radius=config["max_distance_m"]
        )
        # Release mapped file pages between sources; keep only the contiguous
        # private window. Persistent mappings would inflate the summed RSS guard.
        arrays.clear()
        classes = data.pop("classes")
        observers, outdoor = observer_design(source, data, samples=config["source_samples"])
        targets, rr, cc, inside = target_population(
            water,
            data["transform"],
            data["ground"].shape,
            observers,
            max_distance_m=config["max_distance_m"],
        )
        if not inside.all():
            raise ValueError("Incomplete target coverage")
        if not len(targets):
            atomic_sink_parquet(
                pl.DataFrame(schema=TRANSMISSION_PAIR_SCHEMA).lazy(),
                folder / f"{source['source_h3']}.parquet",
                overwrite=False,
            )
            atomic_write_json(
                folder / f"{source['source_h3']}.json",
                dict(
                    source_h3=source["source_h3"],
                    source_state="no_target_population",
                    observers=observers.tolist(),
                    target_cells=0,
                    target_water_points=0,
                    sampled_target_points=0,
                    sampled_result_used=None,
                    sampling_error=None,
                    lower=None,
                    upper=None,
                    unknown=None,
                ),
                overwrite=False,
            )
            continue
        lon, lat = to_w.transform(targets[:, 0], targets[:, 1])
        cells, codes = np.unique(
            np.asarray([h3.latlng_to_cell(y, x, 7) for x, y in zip(lon, lat, strict=True)]),
            return_inverse=True,
        )
        selected, weights = target_design(
            rr, cc, codes, config["target_stride"], census_threshold=config["census_threshold"]
        )
        prep_s = time.perf_counter() - started
        fast, fast_s = calculate(
            data,
            classes,
            observers,
            targets[selected],
            codes[selected],
            weights,
            cells,
            config["max_distance_m"],
            distance_config=distance,
            water_class_policy=config["water_class_policy"],
        )
        reference, full_s, error = None, 0.0, None
        if ordinal in references:
            reference, full_s = calculate(
                data,
                classes,
                observers,
                targets,
                codes,
                np.ones(len(targets)),
                cells,
                config["max_distance_m"],
                distance_config=distance,
                water_class_policy=config["water_class_policy"],
            )
            differences = np.concatenate(
                [np.abs(reference[k] - fast[k]) for k in ("lower", "upper", "unknown")]
            )
            for sampled, census in zip(fast["diagnostics"], reference["diagnostics"], strict=True):
                if (sampled["above_ground_status"] == "not_applicable") != (
                    census["above_ground_status"] == "not_applicable"
                ):
                    differences = np.append(differences, 1.0)
                for key in ("weight_terrain", "above_ground_lower", "above_ground_upper"):
                    if sampled[key] is not None and census[key] is not None:
                        differences = np.append(differences, abs(sampled[key] - census[key]))
            error = dict(
                maximum=float(differences.max()), p95=float(np.quantile(differences, 0.95))
            )
        accepted = error is None or (
            error["maximum"] <= config["maximum_sampling_error"]
            and error["p95"] <= config["p95_sampling_error"]
        )
        result = fast if accepted else reference
        if result is None:
            raise RuntimeError("Rejected sampling without a census reference")
        counts = np.bincount(codes, minlength=len(cells))
        rows = [
            dict(
                **result["diagnostics"][i],
                source_type="land",
                support_status="unknown" if result["unknown"][i] > 0 else "known",
                target_support="partial_h3_in_observer_reach",
                method=config["method"],
                source_h3=source["source_h3"],
                target_h3=str(cell),
                lower=float(result["lower"][i]),
                upper=float(result["upper"][i]),
                average_viewability=result["score"][i],
                input_unknown_fraction=float(result["unknown"][i]),
                out_of_range_fraction=float(result["out_of_range"][i]),
                target_water_pixel_count=int(counts[i]),
                sampled_target_points=int((codes[selected] == i).sum()),
                observer_count=len(observers),
                max_distance_m=config["max_distance_m"],
                sampling=f"stride{config['target_stride']}" if accepted else "census_fallback",
                census_checked=reference is not None,
            )
            for i, cell in enumerate(cells)
        ]
        frame = pl.DataFrame(
            rows,
            schema_overrides={
                key: pl.Float64
                for key in (
                    "average_viewability",
                    "above_ground_lower",
                    "above_ground_upper",
                    "weight_above_ground",
                )
            },
        )
        slat, slon = h3.cell_to_latlng(source["source_h3"])
        centres = [h3.cell_to_latlng(str(cell)) for cell in cells]
        distances = pl.DataFrame(
            dict(
                source_lat=[slat] * len(cells),
                source_lon=[slon] * len(cells),
                target_lat=[p[0] for p in centres],
                target_lon=[p[1] for p in centres],
            )
        ).select(
            _haversine_expr("source_lat", "source_lon", "target_lat", "target_lon").alias(
                "distance_km"
            )
        )
        frame = frame.hstack(distances).with_columns(
            distance_weight_expr(distance, distance.hard_cutoff_km)
        )
        frame = frame.select(list(TRANSMISSION_PAIR_SCHEMA)).cast(
            pl.Schema(TRANSMISSION_PAIR_SCHEMA)
        )
        atomic_sink_parquet(
            frame.lazy(), folder / f"{source['source_h3']}.parquet", overwrite=False
        )
        info = dict(
            source_state="complete",
            source_h3=source["source_h3"],
            ordinal=ordinal,
            source_land=source["source_land"],
            source_outdoor_land=outdoor,
            observers=observers.tolist(),
            preparation_seconds=prep_s,
            scoring_seconds=fast_s,
            reference_seconds=full_s,
            total_seconds=time.perf_counter() - started,
            target_cells=len(cells),
            target_water_points=len(targets),
            sampled_target_points=len(selected),
            sampling_error=error,
            sampled_result_used=accepted,
            pid=os.getpid(),
            lower=float(np.average(result["lower"], weights=counts)),
            upper=float(np.average(result["upper"], weights=counts)),
            unknown=float(np.average(result["unknown"], weights=counts)),
        )
        atomic_write_json(folder / f"{source['source_h3']}.json", info, overwrite=False)
        print(
            f"Source {ordinal + 1}/{len(sources)}: {source['source_h3']} "
            f"{len(cells)} targets; prep {prep_s:.2f}s score {fast_s:.2f}s census {full_s:.2f}s",
            flush=True,
        )
        del data, classes, targets, rr, cc, selected, weights, fast, reference, result
        gc.collect()
