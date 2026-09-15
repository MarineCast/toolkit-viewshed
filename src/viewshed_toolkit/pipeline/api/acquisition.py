"""Provider dispatch and acquisition manifests; never prepares a raster."""

import json
from dataclasses import asdict
from pathlib import Path

from ..config import AppConfig, load_app_config
from ..config.datasets import DatasetsConfig
from ..config.paths import bbox_from_config
from ..contracts.components import component_root, input_checksums, write_json
from ..providers import DownloadResult, get_provider


def download_dataset(
    config: str | Path | AppConfig, dataset: str, *, overwrite: bool = False
) -> DownloadResult:
    app = config if isinstance(config, AppConfig) else load_app_config(config)
    if dataset not in {"dem", "chm"}:
        raise ValueError("dataset must be dem or chm")
    settings = getattr(DatasetsConfig.model_validate(app.raw_config.get("datasets", {})), dataset)
    if not settings.enabled:
        raise ValueError(f"Dataset {dataset} is disabled")
    provider = get_provider(settings.provider)
    # Local paths use the same project-root contract as other configured paths.
    if settings.provider == "local":
        from ..config.paths import resolve_path

        settings = settings.model_copy(
            update={
                "assets": tuple(
                    str(resolve_path(path, app.config_path.parent)) for path in settings.assets
                )
            }
        )
    root = component_root(app) / "inputs" / dataset
    from pyproj import Transformer

    forward = Transformer.from_crs(4326, app.viewshed.crs_projected, always_xy=True)
    inverse = Transformer.from_crs(app.viewshed.crs_projected, 4326, always_xy=True)
    west, south, east, north = forward.transform_bounds(
        *bbox_from_config(app.raw_config), densify_pts=21
    )
    margin = app.viewshed.max_distance_m + app.viewshed.aoi_margin_m
    acquisition_bbox = inverse.transform_bounds(
        west - margin, south - margin, east + margin, north + margin, densify_pts=21
    )
    assets = provider.discover(acquisition_bbox, settings)
    excluded_assets = []
    if settings.land_tiles_only:
        if dataset != "chm" or settings.provider != "global_canopy_height" or settings.assets:
            raise ValueError("land_tiles_only requires discovered global canopy tiles")
        import geopandas as gpd
        from shapely.geometry import box

        from ..prepare.vegetation.sources import eth_tile_bounds

        land = gpd.read_file(app.paths.land_polygon_path, bbox=acquisition_bbox).to_crs(4326)
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
                "observed_sha256": checksums[str(index)],
            }
        )
    write_json(
        root / "download.json",
        {
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
