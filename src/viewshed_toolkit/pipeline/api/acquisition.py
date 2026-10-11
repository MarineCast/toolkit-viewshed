"""Provider dispatch and acquisition manifests; never prepares a raster."""

import json
from dataclasses import asdict
from pathlib import Path

from viewshed_toolkit._internal.artifacts.checksums import checksum_unchanged_file

from ..config import AppConfig, load_app_config
from ..config.datasets import DatasetConfig
from ..config.paths import bbox_from_config
from ..config.study import with_study_config
from ..contracts.components import component_root, input_checksums, write_json
from ..providers import DownloadResult, get_provider


@with_study_config
def download_dataset(
    config: str | Path | AppConfig, dataset: str, *, overwrite: bool = False
) -> DownloadResult:
    app = config if isinstance(config, AppConfig) else load_app_config(config)
    if dataset not in {"dem", "chm"}:
        raise ValueError("dataset must be dem or chm")
    from ..contracts.components import acquisition_request

    request = acquisition_request(app, dataset)
    settings = DatasetConfig.model_validate(request["dataset"])
    if not settings.enabled:
        raise ValueError(f"Dataset {dataset} is disabled")
    provider = get_provider(settings.provider)
    root = component_root(app) / "inputs" / dataset
    acquisition_bbox = tuple(request["acquisition_bbox"])
    assets = provider.discover(acquisition_bbox, settings)
    excluded_assets = []
    if settings.land_tiles_only:
        if dataset != "chm" or settings.provider != "global_canopy_height" or settings.assets:
            raise ValueError("land_tiles_only requires discovered global canopy tiles")
        import geopandas as gpd
        from pyproj import Transformer
        from shapely.geometry import box

        from ..prepare.vegetation.sources import eth_tile_bounds

        source_crs = gpd.read_file(app.paths.land_polygon_path, rows=0).crs
        if not source_crs:
            raise ValueError("Land geometry requires an explicit CRS for canopy tile selection")
        west, south, east, north = acquisition_bbox
        source_bbox = Transformer.from_crs(4326, source_crs, always_xy=True).transform_bounds(
            west, south, east, north, densify_pts=21
        )
        land = gpd.read_file(app.paths.land_polygon_path, bbox=source_bbox).to_crs(4326)
        land.geometry = land.geometry.make_valid().intersection(box(*acquisition_bbox))
        selected = []
        for asset in assets:
            if land.geometry.intersects(box(*eth_tile_bounds(asset.id))).any():
                selected.append(asset)
            else:
                excluded_assets.append(
                    {"id": asset.id, "reason": "no_mapped_land_in_acquisition_area"}
                )
        assets = selected
    result = provider.download(assets, root / "assets", cache=settings.cache and not overwrite)
    checksums = input_checksums({str(i): path for i, path in enumerate(result.paths)})
    source_records = []
    for index, (asset, path) in enumerate(zip(result.assets, result.paths, strict=True)):
        sidecar = path.with_suffix(path.suffix + ".json")
        metadata = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        source_records.append(
            {
                **asdict(asset),
                "downloaded_at": metadata.get("downloaded_at"),
                "source_year": (
                    settings.source_year
                    if settings.source_year is not None
                    else (2020 if settings.provider == "global_canopy_height" else None)
                ),
                "source_version": settings.version,
                "observed_sha256": checksum_unchanged_file(path, raw_bytes=True),
                "artifact_checksum": checksums[str(index)],
                "artifact_checksum_algorithm": "sha256_filename_then_bytes",
            }
        )
    write_json(
        root / "download.json",
        {
            "manifest_schema_version": 2,
            "observed_checksum_algorithm": "sha256_raw_bytes",
            "dataset": settings.model_dump(mode="json"),
            "bbox": list(bbox_from_config(app.raw_config)),
            "config_hash": app.config_hash,
            "acquisition_bbox": list(acquisition_bbox),
            "assets": source_records,
            "excluded_assets": excluded_assets,
            "paths": [str(path) for path in result.paths],
            "checksums": checksums,
        },
    )
    return result
