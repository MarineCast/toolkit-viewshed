"""Strict, explicit configuration for the supported direct transmission method."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .distance import DistanceWeightConfig, load_distance_weight_config

METHOD = "worldcover_chm_intersection_transmission_v2"


class InputAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransmissionQualification(BaseModel):
    """Declared scenario provenance; declarations never certify field accuracy."""

    model_config = ConfigDict(extra="forbid", strict=True)
    terrain_vertical_reference: str = Field(min_length=1)
    water_vertical_reference: str = Field(min_length=1)
    vertical_units: Literal["m"]
    canopy_height_reference: Literal["above_ground"]
    building_height_reference: Literal["above_ground"]
    source_epochs: dict[str, str]
    source_licenses: dict[str, str]
    empirical_status: Literal["uncalibrated_scenario"]

    @model_validator(mode="after")
    def attribution(self) -> Any:
        for values in (self.source_epochs, self.source_licenses):
            if set(values) != {"terrain", "canopy", "buildings", "worldcover", "coastline"} or any(
                not value.strip() for value in values.values()
            ):
                raise ValueError("Explicit epochs and licenses required for all five inputs")
        return self


class TransmissionConfig(BaseModel):
    """V2 method recipe; regional acquisition and existing generation overwrite are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal[2]
    method: Literal["worldcover_chm_intersection_transmission_v2"]
    output_dir: str
    source_bbox_wgs84: list[float]
    inputs: dict[str, InputAsset]
    qualification: TransmissionQualification
    workers: int = Field(default=1, ge=1, le=2)
    memory_budget_gib: float = Field(default=6.0, gt=0, le=6)
    max_prepared_bytes: int = Field(default=4 * 1024**3, gt=0, le=4 * 1024**3)
    max_raster_pixels: int = Field(default=128_000_000, gt=0, le=128_000_000)
    wall_seconds: float = Field(default=21600.0, gt=0, le=21600)
    max_distance_m: float = Field(default=24140.16, gt=0, le=24140.16)
    source_samples: int = Field(default=15, ge=1, le=15)
    target_stride: int = Field(default=2, ge=1, le=2)
    census_threshold: int = Field(default=1000, ge=0)
    census_sources: int = Field(default=12, ge=1)
    maximum_sampling_error: float = Field(default=0.10, ge=0, le=0.10)
    p95_sampling_error: float = Field(default=0.05, ge=0, le=0.05)
    building_inventory_policy: Literal["assume_complete", "unverified"] = "assume_complete"
    water_class_policy: Literal["zero_if_missing", "retain_unknown"] = "zero_if_missing"
    distance_weight: dict[str, Any] = Field(
        default_factory=lambda: dict(
            selected_model="logistic",
            logistic_d50_km=9.0,
            logistic_slope_km=2.7,
            normalize_at_zero=True,
            hard_cutoff_km=30.0,
        )
    )

    @model_validator(mode="after")
    def validated(self) -> Any:
        b = self.source_bbox_wgs84
        if (
            len(b) != 4
            or not all(math.isfinite(x) for x in b)
            or not (-180 <= b[0] < b[2] <= 180 and -90 < b[1] < b[3] < 90)
            or (b[2] - b[0]) * (b[3] - b[1]) > 1
        ):
            raise ValueError("Ordered bounded WGS84 source rectangle required")
        required = {
            "coastline",
            "terrain_vrt",
            "terrain_provenance",
            "canopy_inventory",
            "worldcover_manifest",
            "gba_subset",
            "gba_manifest",
        }
        if not required <= set(self.inputs):
            raise ValueError("Missing provisioned transmission inputs")
        self.distance_config()
        return self

    def distance_config(self) -> DistanceWeightConfig:
        config = load_distance_weight_config({"distance_weight": self.distance_weight})
        if any(isinstance(v, float) and not math.isfinite(v) for v in vars(config).values()):
            raise ValueError("Finite distance parameters required")
        return config

    def runtime(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_transmission_config(path: str | Path) -> TransmissionConfig:
    """Read-only load; relative paths resolve against the caller's JSON file."""
    path = Path(path).resolve()
    config = TransmissionConfig.model_validate_json(path.read_text())
    payload = config.runtime()
    payload["output_dir"] = str((path.parent / payload["output_dir"]).resolve())
    for asset in payload["inputs"].values():
        asset["path"] = str((path.parent / asset["path"]).resolve())
    # JSON validation preserves strict integer/boolean distinctions.
    return TransmissionConfig.model_validate_json(json.dumps(payload))
