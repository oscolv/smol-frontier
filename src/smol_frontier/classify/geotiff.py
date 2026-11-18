"""Write the classified and published maps as styled GeoTIFFs.

The GeoTIFF opens colored in QGIS: a palette color table on band 1, nodata 0
(transparent), a band description, and per-class legend tags. Writers:
``write_classified_geotiff`` for the classifier's map (classes 0-4), and the
guarded ``write_published_geotiff`` for the published map (classes 0-5).
``guard_published`` is the guard every published class-map raster passes
(this writer and ``classify.display``): it refuses anything that is not a
uint8 class map, so reflectance or imagery can never be written as a map.
Sentinel-2 photos have their own guard, ``publish.truecolour.guard_true_colour``.
"""

from pathlib import Path

import numpy as np
import rasterio

from smol_frontier.config import settings

__all__ = [
    "BAND_DESCRIPTION",
    "CLASS_COLORMAP",
    "CLASS_NAMES",
    "PUBLISHED_CLASS_NAMES",
    "PUBLISHED_COLORMAP",
    "guard_published",
    "hex_to_rgba",
    "write_classified_geotiff",
    "write_published_geotiff",
]

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
    array = guard_published(class_map)
    return _write_styled(
        path, array, transform, crs, PUBLISHED_COLORMAP, PUBLISHED_CLASS_NAMES
    )


def guard_published(class_map: np.ndarray) -> np.ndarray:
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
