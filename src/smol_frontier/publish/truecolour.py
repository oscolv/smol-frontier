"""Sentinel-2 true-colour images of published scenes (contract 3.0.0):
the latest scene's photo and the reservoir-clipped same-month photos.

``read_true_colour`` is the one network boundary of publish besides a
cache miss: it fetches the scene's STAC item by exact id and reads
B04/B03/B02 straight from the item's COGs over the ``map.png`` grid (not
from the scene cache, which is zeroed outside the reservoir), converts DN
to reflectance through ``sources.radiometry`` and warps bilinearly onto the
grid. ``stretch`` maps reflectance to uint8 with the fixed window and gamma in
``config.py``; ``to_rgba`` adds the alpha band that hides everything
outside the reservoir and every no-data pixel (every published photo);
``write_true_colour_webp`` passes ``guard_true_colour`` and writes an RGBA
WebP. The guard is separate from the class-map guard: it admits
Sentinel-2 L2A imagery only, which the Copernicus licence lets us publish
with attribution (decision 13, ADR 0001); PlanetScope never qualifies.
"""

import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, reproject, transform_bounds

from smol_frontier import aoi
from smol_frontier.classify.display import DISPLAY_CRS, DisplayImage
from smol_frontier.config import settings
from smol_frontier.ingest import _inside_mask, _read_window
from smol_frontier.publish.errors import PublishRefused
from smol_frontier.sources.radiometry import (
    boa_offset,
    dn_to_reflectance,
    guard_boa_offset,
)
from smol_frontier.sources.stac import get_scene

__all__ = [
    "COLLECTION",
    "TrueColour",
    "attribution",
    "build_true_colour",
    "clipped_image_path",
    "guard_true_colour",
    "image_path",
    "read_true_colour",
    "stretch",
    "to_rgba",
    "write_true_colour_webp",
]

# WHY: the only STAC collection whose imagery may be published — Sentinel-2
#   L2A under the Copernicus licence (free use with attribution).
COLLECTION = "sentinel-2-l2a"

# WHY: red, green, blue in display order.
_RGB_BANDS = ("B04", "B03", "B02")

IMAGE_DIR = "image"
CLIPPED_DIR = f"{IMAGE_DIR}/clipped"

# WHY: WebP quality 90 keeps the photo free of visible blocking while
#   staying under the 300 KB homepage budget (spec #44).
# HOW: measured on the map.png grid: 88-166 KB for the unclipped RGB photo
#   of the control scenes (contract 3.0.0); 17-25 KB for the clipped RGBA
#   gamma-1.6 photos of the 2017-2026 November scenes (3.2.0).
WEBP_QUALITY = 90


@dataclass(frozen=True, eq=False)
class TrueColour:
    """Surface reflectance of B04/B03/B02 on the ``map.png`` grid."""

    reflectance: np.ndarray  # (3, height, width) float, NaN where no data
    collection: str | None


def image_path(record: dict[str, object]) -> str:
    """Relative path of a scene's photo: ``image/<date>_<item_id>.webp``."""
    return f"{IMAGE_DIR}/{record['date']}_{record['item_id_used']}.webp"


def clipped_image_path(scene: dict[str, object]) -> str:
    """Relative path of a same-month photo of a published scene entry:
    ``image/clipped/<date>_<item_id>.webp``."""
    return f"{CLIPPED_DIR}/{scene['date']}_{scene['item_id']}.webp"


def attribution(day: object) -> str:
    """Copernicus attribution for a scene acquired on ``day`` (YYYY-MM-DD)."""
    return f"Copernicus Sentinel data {str(day)[:4]}"


def _warp_band(
    href: str,
    bbox: tuple[float, float, float, float],
    transform: rasterio.Affine,
    shape: tuple[int, int],
) -> np.ndarray:
    array, src_transform, src_crs = _read_window(href, bbox)
    warped = np.zeros(shape, dtype=np.float64)
    reproject(
        array,
        warped,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=DISPLAY_CRS,
        resampling=Resampling.bilinear,
        src_nodata=0,
        dst_nodata=0,
    )
    return warped


def read_true_colour(
    backend: str, item_id: str, transform: rasterio.Affine, shape: tuple[int, int]
) -> TrueColour:
    """B04/B03/B02 reflectance of one item on a web-Mercator grid.

    The BOA offset is derived once from the warped B02 over the reservoir
    (the pixels ingest derives it from) and applied to all three bands.
    """
    scene = get_scene(backend, item_id)
    height, width = shape
    left, top = transform @ (0, 0)
    right, bottom = transform @ (width, height)
    bbox = transform_bounds(DISPLAY_CRS, "EPSG:4326", left, bottom, right, top)
    dn = {
        band: _warp_band(scene.assets[band], bbox, transform, shape)
        for band in _RGB_BANDS
    }
    inside = _inside_mask(aoi.utm_geometry(), DISPLAY_CRS, transform, shape)
    rule = boa_offset(scene.processing_baseline, scene.boa_offset_applied)
    offset = guard_boa_offset(rule, np.where(inside, dn["B02"], 0.0))
    reflectance = np.stack([dn_to_reflectance(dn[band], offset) for band in _RGB_BANDS])
    return TrueColour(reflectance, scene.collection)


def stretch(reflectance: np.ndarray) -> np.ndarray:
    """Fixed window, then gamma, to uint8; NaN (no data) becomes 0."""
    low, high = settings.TRUE_COLOUR_MIN, settings.TRUE_COLOUR_MAX
    scaled = np.clip((np.nan_to_num(reflectance, nan=low) - low) / (high - low), 0, 1)
    lifted = scaled ** (1.0 / settings.TRUE_COLOUR_GAMMA)
    return np.rint(lifted * 255.0).astype(np.uint8)


def to_rgba(reflectance: np.ndarray, transform: rasterio.Affine) -> np.ndarray:
    """The published RGBA of a (3, h, w) reflectance photo.

    Opaque (255) where the pixel is inside the reservoir and any band is
    above ``TRUE_COLOUR_VALID_MIN``; transparent (0) elsewhere. The polygon
    is rasterised on the photo's own grid (pixel centres, i.e. nearest).
    """
    inside = _inside_mask(
        aoi.utm_geometry(), DISPLAY_CRS, transform, reflectance.shape[1:]
    )
    valid = np.nan_to_num(reflectance, nan=-np.inf) > settings.TRUE_COLOUR_VALID_MIN
    alpha = np.where(inside & valid.any(axis=0), 255, 0).astype(np.uint8)
    return np.concatenate([stretch(reflectance), alpha[None]])


def guard_true_colour(array: np.ndarray, collection: str | None) -> np.ndarray:
    """The guard every published photo passes: S2 L2A, uint8, RGBA.

    Refuses (ValueError) anything else, so PlanetScope imagery or raw
    reflectance can never be written by construction.
    """
    if collection != COLLECTION:
        msg = f"published true colour must come from {COLLECTION}, got {collection!r}"
        raise ValueError(msg)
    array = np.asarray(array)
    if array.dtype != np.uint8:
        msg = f"published true colour must be uint8, got {array.dtype}"
        raise ValueError(msg)
    if array.ndim != 3 or array.shape[0] != 4:
        msg = f"published true colour must be RGBA (4 bands), got shape {array.shape}"
        raise ValueError(msg)
    return array


def write_true_colour_webp(
    path: str | Path, array: np.ndarray, collection: str | None
) -> Path:
    """Write a guarded (4, height, width) uint8 RGBA array as a WebP."""
    path = Path(path)
    image = guard_true_colour(array, collection)
    _, height, width = image.shape
    # WHY: the WEBP driver only supports CreateCopy, so stage in memory; PAM
    #   is disabled so GDAL never writes a .aux.xml beside the image, and the
    #   staging copy is deliberately not georeferenced (bounds live in
    #   latest.json), hence the warning filter.
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
            src.write(image)
            rasterio.shutil.copy(src, path, driver="WEBP", QUALITY=WEBP_QUALITY)
    return path


def build_true_colour(record: dict[str, object], display: DisplayImage) -> TrueColour:
    """Read the record's photo on the ``map.png`` grid; refuse on any failure.

    WHY a refusal: a contract without its photo would be partial, so any
    read failure keeps the previous contract (exit 2).
    """
    item_id = str(record["item_id_used"])
    try:
        return read_true_colour(
            str(record["backend_used"]),
            item_id,
            display.transform,
            display.classes.shape,
        )
    except Exception as exc:
        msg = f"true-colour image of {item_id} could not be read: {exc}"[:300]
        raise PublishRefused(msg) from exc
