"""Reservoir area of interest (AOI).

Provenance: polygon created by the author from JRC Global Surface Water v1.4,
occurrence >= 25 % (Pekel et al., 2016, Nature 540:418-422).

The reservoir area is always computed from the polygon — never hard-coded.
"""

import geopandas as gpd
from shapely.geometry.base import BaseGeometry

from smol_frontier.config import settings

__all__ = [
    "reservoir_area_ha",
    "utm_geometry",
    "wgs84_bbox",
    "wgs84_geometry",
]

_WGS84 = "EPSG:4326"


def wgs84_geometry() -> BaseGeometry:
    """Dissolved reservoir geometry in WGS84 (single Polygon/MultiPolygon)."""
    gdf = gpd.read_file(settings.AOI_PATH)
    return gpd.GeoSeries([gdf.union_all()], crs=gdf.crs).to_crs(_WGS84).iloc[0]


def utm_geometry() -> BaseGeometry:
    """Dissolved reservoir geometry reprojected to the UTM CRS (metres)."""
    return (
        gpd.GeoSeries([wgs84_geometry()], crs=_WGS84).to_crs(settings.UTM_CRS).iloc[0]
    )


def wgs84_bbox() -> tuple[float, float, float, float]:
    """WGS84 bounding box (minx, miny, maxx, maxy) for STAC search."""
    minx, miny, maxx, maxy = wgs84_geometry().bounds
    return (minx, miny, maxx, maxy)


def reservoir_area_ha() -> float:
    """Reservoir area in hectares, computed from the polygon in UTM."""
    return utm_geometry().area / 10_000.0
