"""Exact shared-study source/target selection without physical fraction thresholds."""

from pathlib import Path
import uuid

import geopandas as gpd
import h3
from shapely.geometry import Polygon

from ...config.shared_study import runtime_memberships
from ...contracts.study import _data_artifact


def shared_land_frame(raw, *, projected_crs):
    memberships = runtime_memberships(raw)
    if memberships is None:
        return None
    land = memberships["land_source"]
    identity, manifest = land["study_identity"], land["manifest"]
    selected = Path(raw["shared_study"]["config_path"])
    extent_info = manifest["producer_source_extents"]["viewshed"]
    extent_path, _ = _data_artifact(
        selected, identity, extent_info["relative_path"], extent_info["sha256"]
    )
    extent = gpd.read_parquet(extent_path).to_crs(4326).geometry.union_all()
    physical_land = gpd.read_parquet(raw["paths"]["land_polygon_path"]).to_crs(4326)
    physical_land = physical_land.geometry.union_all().intersection(extent)
    cells = land["cells"]
    full = gpd.GeoSeries(
        [Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]) for cell in cells],
        crs=4326,
    )
    active = full.intersection(physical_land)
    metric = active.to_crs(projected_crs)
    if active.is_empty.any() or not active.is_valid.all() or not (metric.area > 0).all():
        raise ValueError("Shared land membership lacks positive physical land geometry")
    points = metric.representative_point().to_crs(4326)
    full_area = full.to_crs(projected_crs).area
    return gpd.GeoDataFrame(
        {
            "h3_cell": cells,
            "h3_resolution": 7,
            "cell_area_m2": full_area,
            "land_area_m2": metric.area,
            "land_fraction": metric.area / full_area,
            "centroid_lon": points.x,
            "centroid_lat": points.y,
            "full_h3_geometry": full,
            "geometry": active,
            "study_config_sha256": identity["config_sha256"],
            "source_membership_sha256": land["artifact_sha256"],
            "source_extent_sha256": extent_info["sha256"],
        },
        geometry="geometry",
        crs=4326,
    )


def write_shared_land_frame(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
