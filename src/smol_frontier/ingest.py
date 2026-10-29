"""Scene ingest: windowed COG reads, resampling, masking, and local cache.

Reads ONLY the AOI window from each band COG (never the full ~1 GB tile),
resamples the 20 m bands onto the 10 m B04 grid (bilinear for reflectance,
nearest neighbour for SCL), and masks pixels outside the reservoir polygon to
nodata.

The cache stores RAW DN + SCL + metadata tags; the radiometric offset is
re-derived at read time from B02, so a radiometry fix never requires
re-downloading. One offset per scene, applied to every band.
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom
from rasterio.windows import Window
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry.base import BaseGeometry

from smol_frontier.aoi import utm_geometry, wgs84_bbox
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.sources.radiometry import (
    boa_offset,
    dn_to_reflectance,
    guard_boa_offset,
)
from smol_frontier.sources.stac import SceneMeta

logger = logging.getLogger(__name__)

__all__ = ["BAND_ORDER", "DEFAULT_CACHE_DIR", "IngestedScene", "ingest_scene"]

BAND_ORDER = ("B02", "B03", "B04", "B05", "B08", "B8A", "B11", "SCL")
BANDS_20M = frozenset({"B05", "B8A", "B11", "SCL"})
REF_GRID_BAND = "B04"
# WHY: shared on-disk cache for ingested scenes, so re-runs never re-download.
# HOW: repo layout (gitignored data/cache/) anchored at PROJECT_ROOT, so the
#   caller's cwd cannot scatter or orphan cache copies.
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data/cache"


@dataclass(frozen=True, eq=False)
class IngestedScene:
    """One scene on the 10 m grid: raw DN, mask, grid, and scene offset."""

    scene: SceneMeta
    bands: dict[str, np.ndarray]
    inside: np.ndarray
    transform: rasterio.Affine
    crs: str
    offset: float

    def reflectance(self, band: str) -> np.ndarray:
        """Band reflectance with the scene offset (SCL is not DN)."""
        if band == "SCL":
            msg = "SCL is a class map, not DN"
            raise ValueError(msg)
        return dn_to_reflectance(self.bands[band], self.offset)


def _covering_window(
    bounds: tuple[float, float, float, float], transform: rasterio.Affine
) -> Window:
    """Smallest integer window covering bounds (floor start, ceil end)."""
    left, bottom, right, top = bounds
    res_x = transform.a
    res_y = -transform.e
    col_off = math.floor((left - transform.c) / res_x)
    row_off = math.floor((transform.f - top) / res_y)
    col_end = math.ceil((right - transform.c) / res_x)
    row_end = math.ceil((transform.f - bottom) / res_y)
    return Window(col_off, row_off, col_end - col_off, row_end - row_off)


def _read_window(
    href: str, bbox: tuple[float, float, float, float]
) -> tuple[np.ndarray, rasterio.Affine, rasterio.CRS]:
    """Read just the AOI window of one COG (boundless, nodata-filled)."""
    with rasterio.open(href) as src:
        if src.crs is None:
            msg = f"missing CRS: {href}"
            raise ValueError(msg)
        if src.transform.b != 0.0 or src.transform.d != 0.0:
            msg = f"rotated transform unsupported: {href}"
            raise ValueError(msg)
        bounds = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
        window = _covering_window(bounds, src.transform)
        array = src.read(1, window=window, boundless=True, fill_value=0)
        return array, src.window_transform(window), src.crs


def _resample_to_ref(
    array: np.ndarray,
    src_transform: rasterio.Affine,
    src_crs: rasterio.CRS,
    ref_transform: rasterio.Affine,
    ref_shape: tuple[int, int],
    ref_crs: rasterio.CRS,
    resampling: Resampling,
) -> np.ndarray:
    """Reproject one band array onto the 10 m reference grid."""
    dst = np.zeros(ref_shape, dtype=np.uint16)
    reproject(
        array,
        dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=ref_transform,
        dst_crs=ref_crs,
        resampling=resampling,
        src_nodata=0,
        dst_nodata=0,
    )
    return dst


def _inside_mask(
    aoi_geom: BaseGeometry,
    dst_crs: rasterio.CRS,
    dst_transform: rasterio.Affine,
    shape: tuple[int, int],
) -> np.ndarray:
    """Rasterize the reservoir polygon onto the grid (True = inside)."""
    geom = transform_geom(settings.UTM_CRS, dst_crs, shapely_mapping(aoi_geom))
    return rasterize(
        [(geom, 1)], out_shape=shape, transform=dst_transform, fill=0, dtype="uint8"
    ).astype(bool)


def _scene_offset(scene: SceneMeta, b2_dn: np.ndarray) -> float:
    """The scene offset, derived once from B02 (rule + guard)."""
    rule = boa_offset(scene.processing_baseline, scene.boa_offset_applied)
    return guard_boa_offset(rule, b2_dn)


def _cache_path(cache_dir: Path, scene_id: str) -> Path:
    return cache_dir / f"{scene_id.replace('/', '_')}.tif"


def _write_cache(
    path: Path,
    scene: SceneMeta,
    bands: dict[str, np.ndarray],
    transform: rasterio.Affine,
    crs: rasterio.CRS,
) -> None:
    """Write raw DN + SCL + metadata tags (never the derived offset)."""
    height, width = bands[REF_GRID_BAND].shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": len(BAND_ORDER),
        "dtype": "uint16",
        "crs": crs,
        "transform": transform,
        "tiled": True,
        "compress": "deflate",
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(path, "w", **profile) as dst:
        for index, band in enumerate(BAND_ORDER, start=1):
            dst.write(bands[band], index)
            dst.set_band_description(index, band)
        dst.update_tags(
            item_id=scene.id,
            source=scene.source,
            processing_baseline=scene.processing_baseline or "",
            boa_offset_applied=str(scene.boa_offset_applied),
            datetime=scene.datetime.isoformat(),
            assets=json.dumps(scene.assets),
            tile_cloud_cover=""
            if scene.cloud_cover is None
            else str(scene.cloud_cover),
        )


def _read_cache(
    path: Path,
) -> tuple[SceneMeta, dict[str, np.ndarray], rasterio.Affine, rasterio.CRS]:
    """Read raw DN + SCL + metadata tags back from the cache."""
    with rasterio.open(path) as src:
        order = tuple(src.descriptions)
        if order != BAND_ORDER:
            msg = f"unexpected band order in {path}: {order}"
            raise ValueError(msg)
        bands = {band: src.read(index) for index, band in enumerate(order, start=1)}
        transform, crs = src.transform, src.crs
        tags = src.tags()
    if crs is None:
        msg = f"missing CRS in {path}"
        raise ValueError(msg)
    # WHY .get: cache files written before the tile-cloud tag existed have no
    #   such tag; they stay readable (cloud cover None) instead of stale.
    raw_cloud = tags.get("tile_cloud_cover") or ""
    try:
        cloud_cover = float(raw_cloud) if raw_cloud else None
    except ValueError:
        cloud_cover = None
    scene = SceneMeta(
        id=tags["item_id"],
        datetime=datetime.fromisoformat(tags["datetime"]),
        source=tags["source"],
        processing_baseline=tags["processing_baseline"] or None,
        boa_offset_applied=tags["boa_offset_applied"] == "True",
        assets=json.loads(tags["assets"]),
        cloud_cover=cloud_cover,
    )
    return scene, bands, transform, crs


def ingest_scene(
    scene: SceneMeta,
    bbox: tuple[float, float, float, float] | None = None,
    aoi_geom: BaseGeometry | None = None,
    cache_dir: Path | None = None,
) -> IngestedScene:
    """Ingest a scene to the 10 m grid, using the cache when available.

    ``bbox``/``aoi_geom`` default to the AOI from aoi.py; ``cache_dir``
    defaults to ``data/cache``. A cache hit re-derives the offset and mask
    instead of trusting stored derivatives.
    """
    cache_dir = DEFAULT_CACHE_DIR if cache_dir is None else Path(cache_dir)
    if aoi_geom is None:
        aoi_geom = utm_geometry()
    path = _cache_path(cache_dir, scene.id)
    if path.exists():
        try:
            scene, bands, transform, crs = _read_cache(path)
        except ValueError as exc:
            logger.info("stale cache %s (%s); re-ingesting", scene.id, exc)
        else:
            logger.info("cache hit: %s", scene.id)
            shape = bands[REF_GRID_BAND].shape
            inside = _inside_mask(aoi_geom, crs, transform, shape)
            offset = _scene_offset(scene, bands["B02"])
            return IngestedScene(scene, bands, inside, transform, str(crs), offset)
    if bbox is None:
        bbox = wgs84_bbox()
    logger.info("cache miss, ingesting: %s", scene.id)
    ref_array, ref_transform, ref_crs = _read_window(scene.assets[REF_GRID_BAND], bbox)
    shape = ref_array.shape
    bands = {}
    for band in BAND_ORDER:
        if band == REF_GRID_BAND:
            bands[band] = np.asarray(ref_array, dtype=np.uint16)
        elif band in BANDS_20M:
            array, src_transform, src_crs = _read_window(scene.assets[band], bbox)
            resampling = Resampling.nearest if band == "SCL" else Resampling.bilinear
            bands[band] = _resample_to_ref(
                array, src_transform, src_crs, ref_transform, shape, ref_crs, resampling
            )
        else:
            array, _, _ = _read_window(scene.assets[band], bbox)
            if array.shape != shape:
                msg = f"{band} grid {array.shape} != B04 grid {shape}"
                raise ValueError(msg)
            bands[band] = np.asarray(array, dtype=np.uint16)
    inside = _inside_mask(aoi_geom, ref_crs, ref_transform, shape)
    for band in BAND_ORDER:
        bands[band][~inside] = 0
    offset = _scene_offset(scene, bands["B02"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    _write_cache(path, scene, bands, ref_transform, ref_crs)
    return IngestedScene(scene, bands, inside, ref_transform, str(ref_crs), offset)
