"""Write the classified map as a styled GeoTIFF, and the published display PNG.

The GeoTIFF opens colored in QGIS: a palette color table on band 1, nodata 0
(transparent), a band description, and per-class legend tags. Writers:
``write_classified_geotiff`` for the classifier's map (classes 0-4), and the
guarded ``write_published_geotiff`` and ``write_display_png`` for the
published map (classes 0-5), which refuse anything that is not a uint8 class
map and colour it themselves — so reflectance or source imagery can never be
written through them.
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

from smol_frontier.config import settings

__all__ = [
    "BAND_DESCRIPTION",
    "CLASS_COLORMAP",
    "CLASS_NAMES",
    "PUBLISHED_CLASS_NAMES",
    "PUBLISHED_COLORMAP",
    "DisplayImage",
    "hex_to_rgba",
    "render_display_image",
    "write_classified_geotiff",
    "write_display_png",
    "write_published_geotiff",
]

# WHY: web-map display CRS (EPSG:3857) for the PNG overlay; bounds are
#   published in EPSG:4326 rounded to 6 decimals (~0.1 m), far below a pixel.
DISPLAY_CRS = "EPSG:3857"
BOUNDS_DECIMALS = 6

# WHY: one-line band description shown by GIS clients in layer properties.
BAND_DESCRIPTION = "hyacinth classification"

# WHY: human-readable legend that travels with the file as band tags
#   (rasterio exposes no GDAL category-names API, so tags carry the names).
# HOW: fixed strings per project spec, keyed by class code.
CLASS_NAMES = {
    settings.CLASS_NODATA: "nodata",
    settings.CLASS_WATER: "water",
    settings.CLASS_DENSE_HYACINTH: "dense hyacinth",
    settings.CLASS_INTERFACE: "interface",
    settings.CLASS_BARE_SOIL: "bare soil",
}


def hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    """Parse #rrggbb (opaque) or #rrggbbaa into an (R, G, B, A) tuple."""
    digits = value.removeprefix("#")
    if len(digits) not in (6, 8):
        msg = f"expected #rrggbb or #rrggbbaa, got {value!r}"
        raise ValueError(msg)
    red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    alpha = int(digits[6:8], 16) if len(digits) == 8 else 255
    return (red, green, blue, alpha)


# WHY: band-1 color table, parsed once from the palette in config.py so the
#   GeoTIFF, the demo UI, and charts can never disagree on class colors.
#   Nodata transparency in QGIS comes from nodata=0; the palette alpha is
#   best-effort (the TIFF palette tag itself is RGB).
CLASS_COLORMAP = {
    settings.CLASS_NODATA: hex_to_rgba(settings.COLOR_NODATA),
    settings.CLASS_WATER: hex_to_rgba(settings.COLOR_WATER),
    settings.CLASS_DENSE_HYACINTH: hex_to_rgba(settings.COLOR_DENSE_HYACINTH),
    settings.CLASS_INTERFACE: hex_to_rgba(settings.COLOR_INTERFACE),
    settings.CLASS_BARE_SOIL: hex_to_rgba(settings.COLOR_BARE_SOIL),
}


# WHY: the published map adds class 5 (removed by exposed-bed correction);
#   the classifier's own legend and colour table above stay 0-4.
PUBLISHED_CLASS_NAMES = {
    **CLASS_NAMES,
    settings.CLASS_BED_REMOVED: "removed by exposed-bed correction",
}
PUBLISHED_COLORMAP = {
    **CLASS_COLORMAP,
    settings.CLASS_BED_REMOVED: hex_to_rgba(settings.COLOR_BED_REMOVED),
}


def write_classified_geotiff(
    path: str | Path,
    class_map: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
) -> Path:
    """Write the class map as a styled uint8 GeoTIFF; return the path."""
    return _write_styled(path, class_map, transform, crs, CLASS_COLORMAP, CLASS_NAMES)


def write_published_geotiff(
    path: str | Path,
    class_map: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
) -> Path:
    """Write the published class map (0-5) as a styled GeoTIFF; return the path.

    Source-imagery guard: raises ValueError, before anything is written, for
    an array that is not 2-D uint8 or holds a value outside the published
    classes.
    """
    array = _guard_published(class_map)
    return _write_styled(
        path, array, transform, crs, PUBLISHED_COLORMAP, PUBLISHED_CLASS_NAMES
    )


def _guard_published(class_map: np.ndarray) -> np.ndarray:
    """The source-imagery guard shared by every published raster (C21)."""
    array = np.asarray(class_map)
    if array.dtype != np.uint8 or array.ndim != 2:
        msg = f"published map must be a 2-D uint8 class map, got {array.dtype} {array.shape}"
        raise ValueError(msg)
    unknown = sorted(set(np.unique(array).tolist()) - set(PUBLISHED_COLORMAP))
    if unknown:
        msg = f"published map holds values outside classes 0-5: {unknown}"
        raise ValueError(msg)
    return array


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
    array = _guard_published(class_map)
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
    classes = _guard_published(image.classes)
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


def _write_styled(
    path: str | Path,
    class_map: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
    colormap: dict[int, tuple[int, int, int, int]],
    names: dict[int, str],
) -> Path:
    """Write a uint8 class map with a colour table, nodata 0 and legend tags."""
    path = Path(path)
    height, width = np.shape(class_map)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs=crs,
        transform=transform,
        nodata=settings.CLASS_NODATA,
        tiled=True,
        compress="deflate",
    ) as dst:
        dst.write(np.asanyarray(class_map, dtype=np.uint8), 1)
        dst.write_colormap(1, colormap)
        dst.set_band_description(1, BAND_DESCRIPTION)
        dst.update_tags(1, **{f"class_{code}": name for code, name in names.items()})
    return path
