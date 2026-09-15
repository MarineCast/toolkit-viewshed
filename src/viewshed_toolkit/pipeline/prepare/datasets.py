"""Independent raster mosaic/clip/reprojection stages with immutable input lineage."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pyproj import Transformer

from ..config import AppConfig
from ..config.datasets import DatasetsConfig
from ..config.paths import bbox_from_config
from ..contracts.components import cache_matches, component_root, provenance, record_product


def prepare_dataset(app: AppConfig, dataset: str, *, overwrite: bool = False) -> Path:
    if dataset not in {"dem", "chm"}:
        raise ValueError("dataset must be dem or chm")
    config = getattr(DatasetsConfig.model_validate(app.raw_config.get("datasets", {})), dataset)
    if not config.enabled:
        raise ValueError(f"Dataset {dataset} is disabled")
    manifest = component_root(app) / "inputs" / dataset / "download.json"
    downloaded = json.loads(manifest.read_text())
    inputs = {str(i): Path(value) for i, value in enumerate(downloaded["paths"])}
    contract = provenance(app, f"prepare_{dataset}_v1", inputs)
    if contract["inputs"] != downloaded["checksums"]:
        raise ValueError(f"Downloaded {dataset} assets changed; rerun download-{dataset}")
    contract["dataset"] = downloaded["dataset"]
    contract["source_manifest_checksum"] = provenance(app, "source", {"manifest": manifest})[
        "inputs"
    ]
    output = app.paths.regional_dem_path if dataset == "dem" else app.paths.canopy_height_path
    if not overwrite and cache_matches(output, contract):
        return output

    from osgeo import gdal

    gdal.UseExceptions()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp.tif")
    vrt_path = temporary.with_suffix(".vrt")
    transformer = Transformer.from_crs("EPSG:4326", app.viewshed.crs_projected, always_xy=True)
    bounds = transformer.transform_bounds(*bbox_from_config(app.raw_config), densify_pts=21)
    # Buffer supports the full target domain and the edge observer windows.
    margin = app.viewshed.max_distance_m + app.viewshed.aoi_margin_m
    bounds = (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)
    resolution = app.viewshed.dem_resolution_m
    try:
        import rasterio

        source_crs = set()
        for path in inputs.values():
            with rasterio.open(path) as source:
                source_crs.add(source.crs)
        if len(source_crs) != 1 or None in source_crs:
            raise ValueError("Provider assets must share one declared CRS before mosaicking")
        # ETH encodes missing canopy as 255. Interpret that source sentinel here,
        # leaving downloaded source bytes immutable and checksum-verifiable.
        options = {"srcNodata": 255} if config.provider == "global_canopy_height" else {}
        vrt = gdal.BuildVRT(str(vrt_path), [str(path) for path in inputs.values()], **options)
        if vrt is None:
            raise ValueError("Cannot mosaic source rasters with incompatible grids/CRS")
        warped = gdal.Warp(
            str(temporary),
            vrt,
            dstSRS=app.viewshed.crs_projected,
            outputBounds=bounds,
            xRes=resolution,
            yRes=resolution,
            targetAlignedPixels=True,
            resampleAlg=("bilinear" if dataset == "dem" else app.viewshed.canopy_resampling),
            outputType=gdal.GDT_Float32,
            dstNodata=-9999,
            warpMemoryLimit=128,
            creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
        )
        if warped is None:
            raise ValueError("Raster preparation failed")
        warped.FlushCache()
        warped = None
        vrt = None
        import rasterio

        with rasterio.open(temporary) as source:
            if source.crs.to_string() != app.viewshed.crs_projected or source.res != (
                resolution,
                resolution,
            ):
                raise ValueError("Prepared raster grid does not match configuration")
            observed = sum(
                int(source.read(1, window=window, masked=True).count())
                for _, window in source.block_windows(1)
            )
            if not observed:
                raise ValueError("Prepared raster contains no observed values")
        temporary.replace(output)
        record_product(output, contract, observed_pixels=observed)
    finally:
        temporary.unlink(missing_ok=True)
        vrt_path.unlink(missing_ok=True)
    return output
