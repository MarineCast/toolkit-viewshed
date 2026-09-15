"""Configuration-owned case-study directories; loading never writes to disk."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CaseStudyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    analysis_root: Path = Path("analysis")
    data_root: Path = Path("data")
    land_url: str = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip"

    @property
    def analysis_directory(self) -> Path:
        return self.analysis_root / self.name

    @property
    def data_directory(self) -> Path:
        return self.data_root / self.name
