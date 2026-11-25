"""Render the published class map for web display and write it as a PNG.

``render_display_image`` warps the published class map (classes 0-5) to
EPSG:3857 with nearest-neighbour resampling and computes its EPSG:4326
bounds; ``write_display_png`` colours it and writes an RGBA PNG (and
``write_display_webp`` a lossless WebP for the per-date viewer). All pass
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
from rasterio.errors import NotGeoreferencedWarning
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
    "decode_display_classes",
    "render_display_image",
    "write_display_png",
    "write_display_webp",
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


def _rgba(image: DisplayImage) -> np.ndarray:
    """The guarded (4, h, w) colours: nodata transparent, 1-5 their colour."""
    classes = guard_published(image.classes)
    lookup = np.zeros((256, 4), dtype=np.uint8)
    for code, color in PUBLISHED_COLORMAP.items():
        lookup[code] = color
    lookup[settings.CLASS_NODATA] = (0, 0, 0, 0)
    return np.moveaxis(lookup[classes], -1, 0).copy()


def write_display_png(path: str | Path, image: DisplayImage) -> Path:
    """Write the display image as an RGBA PNG with no sidecar files.

    Runs the C21 guard on the image's classes before anything is written, then
    colours them: nodata fully transparent, classes 1-5 their configured colour.
    """
    path = Path(path)
    rgba = _rgba(image)
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


def write_display_webp(path: str | Path, image: DisplayImage) -> Path:
    """Write the display image as a lossless RGBA WebP (the viewer's map).

    WHY lossless: lossy WebP blends class colours at their edges, so the
    file would no longer decode to the published classes.
    """
    path = Path(path)
    rgba = _rgba(image)
    _, height, width = rgba.shape
    # WHY the filter: the staging copy is deliberately not georeferenced
    #   (bounds travel in figures.json), as for the true-colour WebP.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with (
            rasterio.Env(GDAL_PAM_ENABLED="NO"),
            MemoryFile() as mem,
            mem.open(
                driver="GTiff",
                height=height,
                width=width,
                count=4,
                dtype="uint8",
                photometric="RGB",
                alpha="YES",
            ) as src,
        ):
            src.write(rgba)
            rasterio.shutil.copy(src, path, driver="WEBP", LOSSLESS="YES")
    return path


def decode_display_classes(rgba: np.ndarray) -> np.ndarray:
    """Class codes of a decoded (4, h, w) display image; refuse unknown colours.

    Transparent pixels are nodata; every opaque pixel must carry exactly one
    published class colour.
    """
    rgba = np.asarray(rgba)
    classes = np.full(rgba.shape[1:], 255, dtype=np.uint8)
    classes[rgba[3] == 0] = settings.CLASS_NODATA
    for code, color in PUBLISHED_COLORMAP.items():
        if code == settings.CLASS_NODATA:
            continue
        match = (rgba[3] == 255) & np.all(
            rgba[:3] == np.array(color[:3], dtype=np.uint8)[:, None, None], axis=0
        )
        classes[match] = code
    if (classes == 255).any():
        msg = f"display image holds {(classes == 255).sum()} pixels of no class colour"
        raise ValueError(msg)
    return classes
