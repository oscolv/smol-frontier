"""Write the classified map as a styled GeoTIFF.

The output opens colored in QGIS: a palette color table on band 1, nodata 0
(transparent), a band description, and per-class legend tags.
"""

from pathlib import Path

import numpy as np
import rasterio

from smol_frontier.config import settings

__all__ = [
    "BAND_DESCRIPTION",
    "CLASS_COLORMAP",
    "CLASS_NAMES",
    "hex_to_rgba",
    "write_classified_geotiff",
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


def write_classified_geotiff(
    path: str | Path,
    class_map: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
) -> Path:
    """Write the class map as a styled uint8 GeoTIFF; return the path."""
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
        dst.write_colormap(1, CLASS_COLORMAP)
        dst.set_band_description(1, BAND_DESCRIPTION)
        dst.update_tags(
            1, **{f"class_{code}": name for code, name in CLASS_NAMES.items()}
        )
    return path
