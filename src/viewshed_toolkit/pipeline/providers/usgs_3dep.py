"""USGS 3DEP acquisition through the existing py3dep implementation."""

from pathlib import Path

from ..config.datasets import DatasetConfig
from ..prepare.elevation.acquire import download_3dep_chunk, infer_chunk_grid, split_bbox
from .base import Asset, BoundingBox, FileProvider

ENDPOINT = "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WCSServer"


class USGS3DEPProvider(FileProvider):
    def discover(self, bbox: BoundingBox, config: DatasetConfig) -> list[Asset]:
        if config.assets:
            return super().discover(bbox, config)
        if config.endpoint not in {None, ENDPOINT}:
            raise ValueError(
                "py3dep manages its WCS endpoint; use explicit assets for another service"
            )
        nx, ny = infer_chunk_grid(bbox)
        return [
            Asset(
                f"3dep-{config.version}-{i}",
                ENDPOINT,
                bbox=chunk[2],
                resolution_m=config.resolution_m,
            )
            for i, chunk in enumerate(split_bbox(bbox, nx=nx, ny=ny))
        ]

    def fetch(self, asset: Asset, destination: Path) -> None:
        if asset.bbox is None:
            super().fetch(asset, destination)
        else:
            if asset.resolution_m is None:
                raise ValueError("3DEP assets require resolution_m")
            download_3dep_chunk(asset.bbox, destination, asset.resolution_m, overwrite=True)
