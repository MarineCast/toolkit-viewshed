"""Read-only bounded configuration for provisioned persistent preparation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .distance import load_distance_weight_config
from .transmission import InputAsset


class TransmissionTileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal[1]
    method: Literal["worldcover_chm_intersection_transmission_v2"]
    store_dir: str
    score_dir: str
    provisioned_metadata: InputAsset
    source_inventory: InputAsset
    reporting_water: InputAsset
    global_transform: list[float]
    global_shape: list[int]
    source_policy: Literal["full_R7_land_eligible_within_report_water_reach_excluding_roofs"] = (
        "full_R7_land_eligible_within_report_water_reach_excluding_roofs"
    )
    max_distance_m: float = Field(default=24140.16, gt=0, le=30000.0)
    source_samples: Literal[15] = 15
    target_stride: Literal[2] = 2
    census_threshold: int = Field(default=1000, ge=0)
    census_sources: int = Field(default=12, ge=1)
    maximum_sampling_error: float = Field(default=0.10, ge=0, le=0.10)
    p95_sampling_error: float = Field(default=0.05, ge=0, le=0.05)
    observer_workers: int = Field(default=1, ge=1, le=8)
    calculation_mode: Literal["reference", "exact_fast", "block_jump"] = "block_jump"
    target_chunks: int = Field(default=4, ge=1, le=4)
    source_batch_size: int = Field(default=2, ge=1, le=2)
    max_store_bytes: int = Field(default=2 * 1024**3, gt=0, le=2 * 1024**3)
    max_memory_bytes: int = Field(default=4 * 1024**3, gt=0, le=4 * 1024**3)
    decoded_cache_bytes: int = Field(default=64 * 1024**2, ge=0, le=256 * 1024**2)
    wall_seconds: float = Field(default=180.0, gt=0, le=180)
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
    def validate_grid(self) -> Any:
        g, s = self.global_transform, self.global_shape
        if len(g) != 6 or g[0] != 10 or g[1] != 0 or g[3] != 0 or g[4] != -10:
            raise ValueError("North-up canonical10m transform required")
        if len(s) != 2 or any(n <= 0 or n % 3 for n in s):
            raise ValueError("Positive30m-nested canonical shape required")
        store, score = Path(self.store_dir).resolve(), Path(self.score_dir).resolve()
        if store.is_relative_to(score) or score.is_relative_to(store):
            raise ValueError("Store and score roots must be disjoint")
        if self.decoded_cache_bytes > self.max_memory_bytes // 4:
            raise ValueError("Decoded tile cache must fit within one quarter of the memory cap")
        load_distance_weight_config({"distance_weight": self.distance_weight})
        return self

    def distance_config(self) -> Any:
        return load_distance_weight_config({"distance_weight": self.distance_weight})

    def runtime(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_transmission_tile_config(path: str | Path) -> TransmissionTileConfig:
    path = Path(path).resolve()
    payload = TransmissionTileConfig.model_validate_json(path.read_text()).runtime()
    for field in ("store_dir", "score_dir"):
        payload[field] = str((path.parent / payload[field]).resolve())
    for field in ("provisioned_metadata", "source_inventory", "reporting_water"):
        payload[field]["path"] = str((path.parent / payload[field]["path"]).resolve())
    return TransmissionTileConfig.model_validate_json(json.dumps(payload))
