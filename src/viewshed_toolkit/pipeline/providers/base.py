"""Acquisition only: discover and download assets without preparing a raster stack."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import numpy as np
import rasterio
import requests

from viewshed_toolkit._internal.artifacts import checksum_path

from ..config.datasets import DatasetConfig
from ..contracts.components import cache_matches, fingerprint, record_product

BoundingBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Asset:
    id: str
    url: str
    checksum: str | None = None
    bbox: BoundingBox | None = None
    resolution_m: int | None = None


@dataclass(frozen=True)
class DownloadResult:
    paths: tuple[Path, ...]
    assets: tuple[Asset, ...]


class RasterProvider(Protocol):
    def discover(self, bbox: BoundingBox, config: DatasetConfig) -> list[Asset]: ...

    def download(
        self, assets: Sequence[Asset], destination: Path, *, cache: bool = True
    ) -> DownloadResult: ...


def validate_raster(path: Path) -> None:
    with rasterio.open(path) as source:
        if source.crs is None or source.count != 1 or min(source.shape) < 1:
            raise ValueError(f"Invalid single-band georeferenced raster: {path}")
        for _, window in source.block_windows(1):
            if not np.isfinite(source.read(1, window=window, masked=True).compressed()).all():
                raise ValueError(f"Nonfinite observed raster value: {path}")


class FileProvider:
    """Common atomic, checksum-verified file download implementation."""

    def discover(self, bbox: BoundingBox, config: DatasetConfig) -> list[Asset]:
        del bbox
        return [
            Asset(str(i), url, config.checksums[i] if config.checksums else None)
            for i, url in enumerate(config.assets)
        ]

    def fetch(self, asset: Asset, destination: Path) -> None:
        if asset.url.startswith(("https://", "http://")):
            with requests.get(asset.url, stream=True, timeout=(30, 300)) as response:
                response.raise_for_status()
                with destination.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        stream.write(chunk)
        else:
            shutil.copyfile(asset.url, destination)

    def download(
        self, assets: Sequence[Asset], destination: Path, *, cache: bool = True
    ) -> DownloadResult:
        if not assets:
            raise ValueError("Provider discovery returned no assets")
        paths: list[Path] = []
        for asset in assets:
            contract = asdict(asset)
            # A local input can change under an unchanged URL.
            if not asset.url.startswith(("http://", "https://")):
                contract["local_checksum"] = checksum_path(Path(asset.url))
            path = destination / f"{fingerprint(contract)}.tif"
            if not cache or not cache_matches(path, contract):
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.tif")
                try:
                    self.fetch(asset, temp)
                    validate_raster(temp)
                    if asset.checksum and checksum_path(temp) != asset.checksum:
                        raise ValueError(f"Checksum mismatch for asset {asset.id}")
                    temp.replace(path)
                    record_product(path, contract, downloaded_at=datetime.now(UTC).isoformat())
                finally:
                    temp.unlink(missing_ok=True)
            paths.append(path)
        return DownloadResult(tuple(paths), tuple(assets))
