"""Export retained comparison counts without recomputing LOS or changing weights."""

from collections import defaultdict
from copy import deepcopy
import csv
from importlib.resources import files
import json
import math
from pathlib import Path

from ..weights.comparison_mean import comparison_mean

REVISION = "source_cell_arithmetic_visibility_v10"
METRICS = (
    "ui_obstruction_mean",
    "ui_obstruction_known_only_mean",
    "ui_obstruction_mean_lower",
    "ui_obstruction_mean_upper",
    "ui_building_mean",
    "ui_building_known_only_mean",
    "ui_building_mean_lower",
    "ui_building_mean_upper",
    "weight_obstruction",
    "weight_obstruction_combined",
    "obstruction_combined_lower",
    "obstruction_combined_upper",
    "ui_obstruction_ratio_lower",
    "ui_obstruction_ratio_upper",
    "ui_canopy_mean",
    "ui_known_only_mean",
    "ui_canopy_mean_lower",
    "ui_canopy_mean_upper",
    "ui_terrain_mean",
    "weight_terrain",
    "weight_distance",
    "weight_vegetation",
    "weight_combined",
    "combined_lower",
    "combined_upper",
    "ui_vegetation_lower",
    "ui_vegetation_upper",
)


def metric_summary(values):
    """Summarize available values while retaining complete-population missingness."""
    valid = [v for v in values if v is not None]
    if any(not math.isfinite(v) for v in valid):
        raise ValueError("Nonfinite metric")
    return dict(
        valid=len(valid),
        null=len(values) - len(valid),
        zero=sum(v == 0 for v in valid),
        positive=sum(v > 0 for v in valid),
        available_sum=sum(valid) if valid else None,
        mean_valid=sum(valid) / len(valid) if valid else None,
        full_sum=sum(valid) if valid and len(valid) == len(values) else None,
    )


def prepare_comparison_payload(payload):
    """Validate pair counts, rebuild diagnostics and derive distinct map aggregates.

    Input rows carry retained counts, actual target_sample_count, n_observers and
    bare_clear_observations. Pair geometry and source polygons are display inputs.
    Original weighted artifacts are never modified.
    """
    data = deepcopy(payload)
    data["presentation_revision"] = REVISION
    results = []
    for product, item in data["viewpoints"].items():
        seen = set()
        for row in item["rows"]:
            key = (row["source_h3"], row["target_h3"])
            if any(not v for v in key) or key in seen:
                raise ValueError("Null or duplicate pair identity")
            seen.add(key)
            mean = comparison_mean(
                row["canopy_clear_observations"],
                row["canopy_zero_observations"],
                row["canopy_unknown_observations"],
            )
            n, targets = row["n_observers"], row["target_sample_count"]
            comparison_mean(n, targets, 0)  # Validate integer support.
            if n <= 0 or mean["total"] != n * targets:
                raise ValueError("Actual observer × target population mismatch")
            bare = row["bare_clear_observations"]
            comparison_mean(bare, 0, 0)
            if bare > mean["total"]:
                raise ValueError("Bare clear count exceeds population")
            row.update(
                sample_comparisons_total=mean["total"],
                sample_comparisons_known=mean["known"],
                ui_canopy_mean=mean["complete"],
                ui_known_only_mean=mean["known_only"],
                ui_canopy_mean_lower=mean["lower"],
                ui_canopy_mean_upper=mean["upper"],
                ui_terrain_mean=bare / mean["total"] if mean["total"] else None,
                observer_scope="source_cell_samples" if n > 1 else "single_point",
                viewpoint_id=None if n > 1 else product,
                observer_latitude=None if n > 1 else row.get("observer_latitude"),
                observer_longitude=None if n > 1 else row.get("observer_longitude"),
            )
            for component in ("obstruction", "building"):
                if component + "_clear_observations" not in row:
                    continue
                counts = comparison_mean(
                    *[
                        row[component + suffix]
                        for suffix in (
                            "_clear_observations",
                            "_zero_observations",
                            "_unknown_observations",
                        )
                    ]
                )
                if counts["total"] != mean["total"]:
                    raise ValueError("Diagnostic comparison population mismatch")
                row.update(
                    {
                        "ui_" + component + "_mean": counts["complete"],
                        "ui_" + component + "_known_only_mean": counts["known_only"],
                        "ui_" + component + "_mean_lower": counts["lower"],
                        "ui_" + component + "_mean_upper": counts["upper"],
                    }
                )
            terrain = row.get("weight_terrain")
            for bound in ("lower", "upper"):
                value = row.get("combined_" + bound)
                row["ui_vegetation_" + bound] = (
                    value / terrain if terrain and value is not None else None
                )
            for bound in ("lower", "upper"):
                value = row.get("obstruction_combined_" + bound)
                row["ui_obstruction_ratio_" + bound] = (
                    value / terrain if terrain and value is not None else None
                )
        item["rows"].sort(key=lambda r: (r["source_h3"], r["target_h3"]))
        rows = item["rows"]
        nonempty = [r for r in rows if r["sample_comparisons_total"]]
        result = dict(
            product=product,
            candidate_pairs=len(rows),
            empty_target_populations=len(rows) - len(nonempty),
            ui_canopy_mean=metric_summary([r["ui_canopy_mean"] for r in nonempty]),
            ui_canopy_mean_interval_sum=[
                sum(r["ui_canopy_mean_" + bound] for r in nonempty) if nonempty else None
                for bound in ("lower", "upper")
            ],
        )
        result["count_pooled"] = comparison_mean(
            *[
                sum(r[k] for r in rows)
                for k in (
                    "canopy_clear_observations",
                    "canopy_zero_observations",
                    "canopy_unknown_observations",
                )
            ]
        )
        if data.get("primary_visibility") == "above_ground_obstruction":
            if any("obstruction_clear_observations" not in r for r in rows):
                raise ValueError("Joint obstruction payload requires joint counts")
            result["ui_obstruction_mean"] = metric_summary(
                [r["ui_obstruction_mean"] for r in nonempty]
            )
            result["obstruction_count_pooled"] = comparison_mean(
                *[
                    sum(r["obstruction_" + k + "_observations"] for r in rows)
                    for k in ("clear", "zero", "unknown")
                ]
            )
        results.append(result)
    data["results"] = results
    composites = [r for p in data["composite_sources"] for r in data["viewpoints"][p]["rows"]]
    data["target_rows"] = _aggregate(composites, "target_h3", data)
    data["source_rows"] = _aggregate(composites, "source_h3", data)
    return data


def _aggregate(rows, key, data):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    output = []
    for cell, members in sorted(groups.items()):
        nonempty = [r for r in members if r["sample_comparisons_total"]]
        row = dict(
            cell=cell,
            polygon=members[0]["polygon"] if key == "target_h3" else data["source_polygons"][cell],
            pair_count=len(members),
            nonempty_pair_count=len(nonempty),
            n_observers=None,
            target_sample_count=None,
        )
        for field in (
            "canopy_clear_observations",
            "canopy_zero_observations",
            "canopy_unknown_observations",
            "bare_clear_observations",
            "sample_comparisons_total",
            "sample_comparisons_known",
        ):
            row[field] = sum(r[field] for r in members)
        for component in ("obstruction", "building"):
            if component + "_clear_observations" in members[0]:
                for status in ("clear", "zero", "unknown"):
                    field = component + "_" + status + "_observations"
                    row[field] = sum(r[field] for r in members)
        mean = comparison_mean(
            row["canopy_clear_observations"],
            row["canopy_zero_observations"],
            row["canopy_unknown_observations"],
        )
        for field in METRICS:
            eligible = (
                [r for r in nonempty if r.get("weight_terrain", 0) > 0]
                if field
                in (
                    "weight_vegetation",
                    "ui_vegetation_lower",
                    "ui_vegetation_upper",
                    "weight_obstruction",
                    "ui_obstruction_ratio_lower",
                    "ui_obstruction_ratio_upper",
                )
                else nonempty
            )
            summary = metric_summary([r.get(field) for r in eligible])
            row[field + "_missing_count"] = summary["null"]
            row[field + "_valid_count"] = summary["valid"]
            row[field + "_applicable_count"] = len(eligible)
            row[field + "_sum"] = summary["full_sum"]
            row[field + "_mean_valid"] = summary["mean_valid"]
            row[field + "_available"] = summary["available_sum"]
            row[field] = (
                summary["full_sum"] / len(eligible) if summary["full_sum"] is not None else None
            )
        row["zero_baseline_pair_count"] = sum(r.get("weight_terrain") == 0 for r in nonempty)
        row.update(
            ui_canopy_mean=mean["complete"],
            ui_known_only_mean=mean["known_only"],
            ui_canopy_mean_lower=mean["lower"],
            ui_canopy_mean_upper=mean["upper"],
            ui_terrain_mean=(
                row["bare_clear_observations"] / mean["total"] if mean["total"] else None
            ),
        )
        for component in ("obstruction", "building"):
            if component + "_clear_observations" in row:
                counts = comparison_mean(
                    *[
                        row[component + "_" + k + "_observations"]
                        for k in ("clear", "zero", "unknown")
                    ]
                )
                row.update(
                    {
                        "ui_" + component + "_mean": counts["complete"],
                        "ui_" + component + "_known_only_mean": counts["known_only"],
                        "ui_" + component + "_mean_lower": counts["lower"],
                        "ui_" + component + "_mean_upper": counts["upper"],
                    }
                )
        output.append(row)
    return output


def export_comparison_maps(payload, output_directory):
    """Write three offline maps and a CSV from validated retained pair populations."""
    data = prepare_comparison_payload(payload)
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    template = files("viewshed_toolkit.resources").joinpath("comparison_map.html").read_text()
    raster_script = files("viewshed_toolkit.resources").joinpath("raster_overlay.js").read_text()
    template = template.replace("__RASTER_SCRIPT__", raster_script)
    paths = []
    for name, kind, title in (
        ("target_cells.html", "target", "Target cells: sources per target"),
        ("source_cells.html", "source", "Source cells: targets per source"),
        (
            "viewpoints.html",
            "point",
            "Selected source cell: "
            + " / ".join(
                item.get("name", source) for source, item in data["composite_sources"].items()
            ),
        ),
    ):
        current = dict(data, kind=kind, title=title, rows=data.get(kind + "_rows", []))
        current.pop("target_rows", None)
        current.pop("source_rows", None)
        encoded = json.dumps(current, separators=(",", ":"), allow_nan=False).replace(
            "<", "\\u003c"
        )
        path = directory / name
        path.write_text(template.replace("__TITLE__", title).replace("__PAYLOAD__", encoded))
        paths.append(path)
    rows = [dict(r, product=p) for p, item in data["viewpoints"].items() for r in item["rows"]]
    columns = [
        "product",
        "source_h3",
        "target_h3",
        "observer_scope",
        "viewpoint_id",
        "n_observers",
        "target_sample_count",
        "sample_comparisons_total",
        "sample_comparisons_known",
        "canopy_clear_observations",
        "canopy_zero_observations",
        "canopy_unknown_observations",
        "bare_clear_observations",
        *[
            c + "_" + k + "_observations"
            for c in ("obstruction", "building")
            for k in ("clear", "zero", "unknown")
        ],
        *METRICS,
    ]
    with (directory / "SOURCE_CELL_COMPARISON_MEANS.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return paths
