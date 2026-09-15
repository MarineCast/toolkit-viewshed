"""Typed acquisition and composition contracts; legacy scientific sections stay intact."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PROVIDER_NAMES = {"local", "usgs_3dep", "global_canopy_height"}


def register_provider_name(name: str) -> None:
    """Called by the provider registry when installing a new provider."""
    _PROVIDER_NAMES.add(name)


class DatasetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    enabled: bool = True
    resolution_m: int = Field(gt=0)
    version: str
    endpoint: str | None = None
    assets: tuple[str, ...] = ()
    checksums: tuple[str, ...] = ()
    cache: bool = True
    land_tiles_only: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value not in _PROVIDER_NAMES:
            raise ValueError(f"Unknown dataset provider: {value}")
        return value

    @model_validator(mode="after")
    def validate_assets(self) -> "DatasetConfig":
        if self.checksums and len(self.checksums) != len(self.assets):
            raise ValueError("checksums must have one entry per asset")
        if self.provider == "local" and not self.assets:
            raise ValueError("local provider requires explicit assets")
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in self.checksums
        ):
            raise ValueError("checksums must be lowercase SHA-256 digests")
        return self


class DatasetsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dem: DatasetConfig = DatasetConfig(provider="usgs_3dep", resolution_m=30, version="live")
    chm: DatasetConfig = DatasetConfig(
        provider="global_canopy_height", resolution_m=10, version="2020"
    )


class CompositionConfig(BaseModel):
    """Existing physical kernel: distance is integrated within LOS aggregation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: Literal["distance_integrated_los"] = "distance_integrated_los"
    land: tuple[Literal["terrain", "canopy"], ...] = ("terrain", "canopy")
    water: tuple[Literal["terrain"], ...] = ("terrain",)

    @model_validator(mode="after")
    def validate_factors(self) -> "CompositionConfig":
        if self.land != ("terrain", "canopy") or self.water != ("terrain",):
            raise ValueError(
                "Supported scientific contract requires land terrain/canopy and water terrain"
            )
        return self
