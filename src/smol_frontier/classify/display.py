"""Render the published class map for web display and write it as a PNG.

``render_display_image`` warps the published class map (classes 0-5) to
EPSG:3857 with nearest-neighbour resampling and computes its EPSG:4326
bounds; ``write_display_png`` colours it and writes an RGBA PNG. Both pass
the class-map guard in ``classify.geotiff`` first, so no reflectance or
pre-made RGBA can ever reach the published map PNG. (Sentinel-2 photos
go through their own guard, ``publish.truecolour.guard_true_colour``.)
"""

import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.io import MemoryFile
from rasterio.warp import (
    Resampling,
    calculate_default_transform,
    reproject,
    transform_bounds,
)

from smol_frontier.classify.geotiff import PUBLISHED_COLORMAP, guard_published
from smol_frontier.config import settings

__all__ = [
    "DISPLAY_CRS",
    "DisplayImage",
    "render_display_image",
    "write_display_png",
]

# WHY: web-map display CRS (EPSG:3857) for the PNG overlay; bounds are
#   published in EPSG:4326 rounded to 6 decimals (~0.1 m), far below a pixel.
DISPLAY_CRS = "EPSG:3857"
BOUNDS_DECIMALS = 6


@dataclass(frozen=True, eq=False)
class DisplayImage:
    """The display raster: classes 0-5 warped to EPSG:3857 plus EPSG:4326 bounds.

    It carries classes, not colours: ``write_display_png`` guards and colours
    them itself, so no pre-made RGBA (e.g. imagery) can reach a PNG.
    """

    classes: np.ndarray  # (height, width) uint8, values 0-5
    transform: rasterio.Affine
    bounds: list[list[float]]  # [[south, west], [north, east]], 6 decimals


def render_display_image(
    class_map: np.ndarray, transform: rasterio.Affine, crs: str
) -> DisplayImage:
    """Warp the class map to EPSG:3857 (nearest) for the display PNG.

    Runs the C21 guard first. Pixels outside the warped footprint are nodata;
    colouring happens in ``write_display_png``.
    """
    array = guard_published(class_map)
    height, width = array.shape
    left, top = transform @ (0, 0)
    right, bottom = transform @ (width, height)
    # WHY the filter: rasterio's own internals still multiply affines with `*`,
    #   which the affine package flags as pending deprecation.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PendingDeprecationWarning)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            crs, DISPLAY_CRS, width, height, left, bottom, right, top
        )
    warped = np.full((dst_height, dst_width), settings.CLASS_NODATA, dtype=np.uint8)
    reproject(
        array,
        warped,
        src_transform=transform,
        src_crs=crs,
        dst_transform=dst_transform,
        dst_crs=DISPLAY_CRS,
        resampling=Resampling.nearest,
        src_nodata=settings.CLASS_NODATA,
        dst_nodata=settings.CLASS_NODATA,
    )
    west, south, east, north = transform_bounds(
        DISPLAY_CRS,
        "EPSG:4326",
        *_extent(dst_transform, dst_height, dst_width),
    )
    bounds = [
        [round(south, BOUNDS_DECIMALS), round(west, BOUNDS_DECIMALS)],
        [round(north, BOUNDS_DECIMALS), round(east, BOUNDS_DECIMALS)],
    ]
    return DisplayImage(warped, dst_transform, bounds)


def _extent(
    transform: rasterio.Affine, height: int, width: int
) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) of a north-up grid."""
    left, top = transform @ (0, 0)
    right, bottom = transform @ (width, height)
    return left, bottom, right, top


def write_display_png(path: str | Path, image: DisplayImage) -> Path:
    """Write the display image as an RGBA PNG with no sidecar files.

    Runs the C21 guard on the image's classes before anything is written, then
    colours them: nodata fully transparent, classes 1-5 their configured colour.
    """
    path = Path(path)
    classes = guard_published(image.classes)
    lookup = np.zeros((256, 4), dtype=np.uint8)
    for code, color in PUBLISHED_COLORMAP.items():
        lookup[code] = color
    lookup[settings.CLASS_NODATA] = (0, 0, 0, 0)
    rgba = np.moveaxis(lookup[classes], -1, 0).copy()
    _, height, width = rgba.shape
    # WHY: the PNG driver only supports CreateCopy, so stage in memory; PAM is
    #   disabled so GDAL never writes a .aux.xml beside the PNG.
    with (
        rasterio.Env(GDAL_PAM_ENABLED="NO"),
        MemoryFile() as mem,
        mem.open(
            driver="GTiff",
            height=height,
            width=width,
            count=4,
            dtype="uint8",
            crs=DISPLAY_CRS,
            transform=image.transform,
            photometric="RGB",
            alpha="YES",
        ) as src,
    ):
        src.write(rgba)
        rasterio.shutil.copy(src, path, driver="PNG")
    return path
