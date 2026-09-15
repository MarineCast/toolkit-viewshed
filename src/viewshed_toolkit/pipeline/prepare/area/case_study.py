"""Acquire case-study coast geometry and construct the buffered water domain."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import requests
from pyproj import Transformer
from shapely.geometry import box

from ...config import AppConfig
from ...config.case_study import CaseStudyConfig
from ...config.paths import bbox_from_config, resolve_path
from ...contracts.components import cache_matches, provenance, record_product, write_json


def prepare_case_geometry(app: AppConfig) -> Path:
    study = CaseStudyConfig.model_validate(app.raw_config["case_study"])
    root = resolve_path(study.data_directory, app.config_path.parent)
    raw = root / "raw" / "land"
    raw.mkdir(parents=True, exist_ok=True)
    archive = raw / "ne_10m_land.zip"
    if not archive.exists():
        temporary = archive.with_suffix(".part")
        try:
            with requests.get(study.land_url, stream=True, timeout=(30, 300)) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            with zipfile.ZipFile(temporary) as bundle:
                if bundle.testzip() is not None:
                    raise ValueError("Corrupt Natural Earth archive")
            temporary.replace(archive)
            write_json(
                archive.with_suffix(".source.json"),
                {
                    "url": study.land_url,
                    "downloaded_at": datetime.now(UTC).isoformat(),
                    "license": "Natural Earth public domain",
                    "license_url": "https://www.naturalearthdata.com/about/terms-of-use/",
                },
            )
        finally:
            temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = Path(member.filename)
            if name.name != member.filename or not name.name.startswith("ne_10m_land."):
                continue
            destination = raw / name
            if not destination.exists():
                destination.write_bytes(bundle.read(member))
    if not app.paths.land_polygon_path.exists():
        raise ValueError("Configured land path does not match the acquired Natural Earth bundle")
    contract = provenance(
        app, "natural_earth_water_complement_v1", {"land": app.paths.land_polygon_path}
    )
    output = app.paths.water_polygon_path
    if cache_matches(output, contract):
        return output
    forward = Transformer.from_crs(4326, app.viewshed.crs_projected, always_xy=True)
    inverse = Transformer.from_crs(app.viewshed.crs_projected, 4326, always_xy=True)
    bounds = forward.transform_bounds(*bbox_from_config(app.raw_config), densify_pts=21)
    margin = app.viewshed.max_distance_m + app.viewshed.aoi_margin_m
    buffered = (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)
    geographic = inverse.transform_bounds(*buffered, densify_pts=21)
    land = gpd.read_file(app.paths.land_polygon_path, bbox=geographic)
    land.geometry = land.geometry.make_valid().intersection(box(*geographic))
    land = land.to_crs(app.viewshed.crs_projected)
    land.geometry = land.geometry.make_valid()
    water = box(*buffered).difference(land.geometry.union_all())
    frame = gpd.GeoDataFrame(
        {"domain": [study.name]}, geometry=[water], crs=app.viewshed.crs_projected
    ).to_crs(4326)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.parquet")
    frame.to_parquet(temporary, index=False)
    temporary.replace(output)
    record_product(
        output,
        contract,
        interpretation="buffered area minus Natural Earth land; not access or territorial jurisdiction",
    )
    return output
