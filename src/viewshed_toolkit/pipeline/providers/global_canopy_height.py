"""ETH 2020 canopy tiles using the existing deterministic tile index."""

from ..config.datasets import DatasetConfig
from ..prepare.vegetation.sources import (
    deterministic_eth_tile_ids_for_bbox,
    eth_chm_filename,
    eth_download_canopy_url,
)
from .base import Asset, BoundingBox, FileProvider


class GlobalCanopyHeightProvider(FileProvider):
    def discover(self, bbox: BoundingBox, config: DatasetConfig) -> list[Asset]:
        if config.assets:
            return super().discover(bbox, config)
        if config.version != "2020":
            raise ValueError(
                "ETH provider supports the 2020 release; use explicit assets for others"
            )
        return [
            Asset(
                tile,
                (
                    (config.endpoint.rstrip("/") + "/" + eth_chm_filename(tile))
                    if config.endpoint
                    else eth_download_canopy_url(eth_chm_filename(tile))
                ),
            )
            for tile in deterministic_eth_tile_ids_for_bbox(bbox)
        ]
