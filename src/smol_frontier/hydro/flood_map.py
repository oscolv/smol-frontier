"""Per-pixel flood stage: at which SIH level each reservoir pixel gets wet.

Built once from two classifier-independent inputs: JRC Global Surface Water
occurrence (which pixels dry first) and the stage–area curve from curve.py
(how much area sits under each stage). Method reproduced from the author's
prior research: windowed GSW clip, bilinear resample to the classifier grid,
interior sink fill by morphological reconstruction, then hypsometric matching
so the area under every stage matches the official curve by construction.
"""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from scipy.ndimage import distance_transform_edt
from skimage.morphology import reconstruction

from smol_frontier.config import PROJECT_ROOT
from smol_frontier.hydro.curve import DEFAULT_CURVE_CSV_PATH, load_curve
from smol_frontier.ingest import ingest_scene
from smol_frontier.sources.stac import SceneMeta

logger = logging.getLogger(__name__)

__all__ = [
    "CONTROL_STAGES",
    "DEFAULT_FLOOD_PATH",
    "DEFAULT_GSW_PATH",
    "FLOOD_NODATA",
    "FloodGrid",
    "FloodMapBuild",
    "FloodMapError",
    "assign_stages",
    "build_flood_map",
    "fetch_gsw_clip",
    "fill_interior_sinks",
    "load_flood_map",
    "pixel_ranking",
]

# WHY: locates the cached scene whose grid the classifier produces — the
#   transform, shape, and mask all come from ingest, never hard-coded.
GRID_SCENE_ID = "S2B_MSIL2A_20261012T170239_R069_T14QMH_20261012T223155"
GRID_SCENE_DAY = datetime(2026, 10, 12)

# WHY: JRC Global Surface Water occurrence tile holding Endho (third-party
#   data, credited in PRIOR_WORK.md); read windowed, never whole.
GSW_URL = (
    "https://storage.googleapis.com/global-surface-water/downloads2021/"
    "occurrence/occurrence_100W_30Nv1_4_2021.tif"
)
GSW_SOURCE = "JRC Global Surface Water v1.4 (1984–2021), occurrence"

# WHY: margin around the grid bounds for the GSW window, so bilinear
#   resampling at the reservoir edge never runs out of source pixels.
GSW_MARGIN_DEG = 0.01

# WHY: GSW occurrence nodata code — remapped to 0 (never water) before
#   resampling, so it cannot leak into the reservoir through bilinear.
GSW_NODATA = 255

# WHY: hectares per grid pixel — the 10 m ingest grid, so 10 x 10 / 10 000.
#   Parallels pipeline.py's PIXEL_AREA_M2; the reservoir area itself still
#   comes only from aoi.py.
PIXEL_HA = 0.01

# WHY: flood-stage nodata outside the mask — negative, so it can never be
#   mistaken for a level.
FLOOD_NODATA = -9999.0

# WHY: reporting threshold for the sink-fill control — pixels whose filled
#   occurrence exceeds the resampled value by more than this count as changed.
SINK_FILL_DELTA = 0.5

# WHY: stages where map area is checked against curve area — spread over the
#   operating range from the 2024 low (2004.6) to the spillway crest (2018).
CONTROL_STAGES = (2004.6, 2006, 2008, 2010, 2012, 2014, 2016, 2018)

FLOOD_UNIT = "m a.s.l.; SIH level at which the pixel gets wet"
FLOOD_METHOD = (
    "hypsometric matching of JRC GSW occurrence to the stage-area curve "
    "with interior sink fill by morphological reconstruction"
)

# WHY: committed flood-stage map and its GSW source clip — the exposed-bed
#   correction reads the map from here, never by rebuilding, so runs are
#   reproducible offline.
# HOW: repo layout anchored at PROJECT_ROOT, on the classifier's 10 m grid.
DEFAULT_GSW_PATH = PROJECT_ROOT / "data/hydro/gsw_occurrence_endho_v1_4_2021.tif"
DEFAULT_FLOOD_PATH = PROJECT_ROOT / "data/hydro/flood_stage.tif"


class FloodMapError(Exception):
    """The flood map cannot be built (missing cache, bad inputs)."""


@dataclass(frozen=True, eq=False)
class FloodGrid:
    """Classifier grid: transform, shape, CRS, and inside mask."""

    transform: rasterio.Affine
    shape: tuple[int, int]
    crs: str
    mask: np.ndarray


@dataclass(frozen=True)
class FloodMapBuild:
    """Result of a flood-map build: controls and output paths."""

    n_pixels: int
    stage_min: float
    stage_max: float
    frac_at_h_min: float
    sink_fill_ha: float
    controls: dict[float, tuple[float, float]]
    tif_path: Path
    json_path: Path


def _grid_from_cache() -> FloodGrid:
    """Classifier grid from the cached 2026-10-12 scene (cache hit only)."""
    meta = SceneMeta(
        id=GRID_SCENE_ID,
        datetime=GRID_SCENE_DAY,
        source="cache",
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    try:
        ing = ingest_scene(meta)
    except KeyError as exc:
        msg = (
            f"cached scene {GRID_SCENE_ID} not found; "
            "run `smolf classify 2026-10-12` first"
        )
        raise FloodMapError(msg) from exc
    return FloodGrid(
        transform=ing.transform,
        shape=ing.inside.shape,
        crs=ing.crs,
        mask=np.asanyarray(ing.inside, dtype=bool),
    )


def fetch_gsw_clip(
    bounds_4326: tuple[float, float, float, float], path: Path | str | None = None
) -> Path:
    """Fetch the windowed GSW occurrence clip, reusing the file if present."""
    target = DEFAULT_GSW_PATH if path is None else Path(path)
    if target.exists():
        logger.info("GSW clip exists, reusing: %s", target)
        return target
    west, south, east, north = bounds_4326
    with (
        rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"),
        rasterio.open(f"/vsicurl/{GSW_URL}") as src,
    ):
        window = src.window(west, south, east, north)
        window = window.round_offsets().round_lengths()
        data = src.read(1, window=window)
        profile = {
            "driver": "GTiff",
            "height": int(window.height),
            "width": int(window.width),
            "count": 1,
            "dtype": src.dtypes[0],
            "crs": src.crs,
            "transform": src.window_transform(window),
            "nodata": GSW_NODATA,
        }
    # Atomic write: the clip is reused blindly, so a crashed download must
    # never leave a partial file behind.
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tif.tmp")
    os.close(fd)
    try:
        with rasterio.open(tmp_name, "w", **profile) as dst:
            dst.write(data, 1)
            dst.update_tags(
                source=GSW_SOURCE,
                url=GSW_URL,
                download_date=datetime.now(UTC).date().isoformat(),
                nodata=str(GSW_NODATA),
            )
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("GSW clip saved: %s", target)
    return target


def fill_interior_sinks(occ: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill interior low-occurrence pockets; keep shore-connected shallows.

    Grayscale morphological reconstruction by erosion: the padded zero ring
    is the drain, so enclosed pockets (historical mats, not low spots) fill
    to their spill level while zones connected to the shore stay. Pure.
    """
    inside = np.asanyarray(mask, dtype=bool)
    base = np.where(inside, np.asanyarray(occ, dtype=np.float64), 0.0)
    occ_padded = np.pad(base, 1)
    mask_padded = np.pad(inside, 1)
    seed = np.where(mask_padded, occ_padded[mask_padded].max(), 0.0)
    filled = reconstruction(seed, occ_padded, method="erosion")[1:-1, 1:-1]
    filled[~inside] = 0.0
    return filled


def pixel_ranking(occ: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Flat pixel indices in flood order: occurrence desc, distance desc.

    Ties on occurrence break by shore distance (deeper first); remaining
    ties break by raster order (lexsort is stable over flatnonzero). Pure.
    """
    inside = np.asanyarray(mask, dtype=bool)
    dist = distance_transform_edt(inside)
    idx = np.flatnonzero(inside)
    order = np.lexsort((-dist.ravel()[idx], -np.asanyarray(occ).ravel()[idx]))
    return idx[order]


def assign_stages(
    ranked: np.ndarray,
    shape: tuple[int, int],
    mask: np.ndarray,
    stage_m: np.ndarray,
    area_ha: np.ndarray,
) -> np.ndarray:
    """Give the i-th ranked pixel (1-based) the stage with A(h) = i pixels.

    Below the curve minimum → h_min (bottom never seen dry); above the
    maximum → h_max (interp clips). Outside the mask → nodata. Pure.
    """
    count = len(ranked)
    areas = np.arange(1, count + 1, dtype=np.float64) * PIXEL_HA
    stages = np.interp(
        areas,
        np.asanyarray(area_ha, dtype=np.float64),
        np.asanyarray(stage_m, dtype=np.float64),
    )
    flood = np.full(shape, FLOOD_NODATA, dtype=np.float32)
    flood.ravel()[np.asanyarray(ranked)] = stages
    return flood


def _flood_json_path(flood_path: Path) -> Path:
    """Controls sidecar next to the flood map."""
    return flood_path.with_name(f"{flood_path.name}.json")


def _curve_file_tag(curve_path: Path) -> str:
    """Curve path for the tags: repo-relative when possible (stable)."""
    try:
        return str(curve_path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(curve_path)


def build_flood_map(
    *,
    grid: FloodGrid | None = None,
    gsw_path: Path | str | None = None,
    curve_path: Path | str | None = None,
    flood_path: Path | str | None = None,
) -> FloodMapBuild:
    """Build the flood-stage map on the classifier grid; write tif + json."""
    grid = _grid_from_cache() if grid is None else grid
    mask = np.asanyarray(grid.mask, dtype=bool)
    height, width = grid.shape
    west, south, east, north = transform_bounds(
        grid.crs, "EPSG:4326", *array_bounds(height, width, grid.transform)
    )
    margin = GSW_MARGIN_DEG
    gsw_file = fetch_gsw_clip(
        (west - margin, south - margin, east + margin, north + margin), gsw_path
    )
    with rasterio.open(gsw_file) as src:
        raw = src.read(1)
        clip_transform = src.transform
        clip_crs = src.crs
        clip_tags = src.tags()
    occ = np.where(raw == GSW_NODATA, 0, raw).astype(np.float32)
    occ_grid = np.zeros(grid.shape, dtype=np.float32)
    reproject(
        occ,
        occ_grid,
        src_transform=clip_transform,
        src_crs=clip_crs,
        dst_transform=grid.transform,
        dst_crs=grid.crs,
        resampling=Resampling.bilinear,
        src_nodata=None,
        dst_nodata=None,
    )
    filled = fill_interior_sinks(occ_grid, mask)
    ranked = pixel_ranking(filled, mask)
    curve = load_curve(curve_path)
    flood = assign_stages(
        ranked, grid.shape, mask, curve.stage_m, curve.area_km2 * 100.0
    )

    inside = flood[mask]
    n_pixels = int(mask.sum())
    h_min = float(curve.stage_m[0])
    frac_at_h_min = float((inside == h_min).sum()) / n_pixels
    sink_fill_ha = (
        float((filled[mask] > occ_grid[mask] + SINK_FILL_DELTA).sum()) * PIXEL_HA
    )
    controls: dict[float, tuple[float, float]] = {}
    for stage in CONTROL_STAGES:
        map_ha = float((inside <= stage).sum()) * PIXEL_HA
        curve_ha = float(np.interp(stage, curve.stage_m, curve.area_km2)) * 100.0
        controls[stage] = (map_ha, curve_ha)

    tif_path = DEFAULT_FLOOD_PATH if flood_path is None else Path(flood_path)
    tif_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=grid.crs,
        transform=grid.transform,
        nodata=FLOOD_NODATA,
        tiled=True,
        compress="deflate",
    ) as dst:
        dst.write(flood, 1)
        dst.update_tags(
            method=FLOOD_METHOD,
            gsw_md5=hashlib.md5(gsw_file.read_bytes()).hexdigest(),
            curve_file=_curve_file_tag(
                DEFAULT_CURVE_CSV_PATH if curve_path is None else Path(curve_path)
            ),
            date=clip_tags["download_date"],
            unit=FLOOD_UNIT,
        )
    summary = {
        "n_pixels": n_pixels,
        "stage_min": round(float(inside.min()), 3),
        "stage_max": round(float(inside.max()), 3),
        "frac_at_h_min": round(frac_at_h_min, 4),
        "sink_fill_ha": round(sink_fill_ha, 2),
        "control_stages": {
            str(stage): {"map_ha": round(map_ha, 2), "curve_ha": round(curve_ha, 2)}
            for stage, (map_ha, curve_ha) in controls.items()
        },
    }
    json_path = _flood_json_path(tif_path)
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return FloodMapBuild(
        n_pixels=n_pixels,
        stage_min=float(inside.min()),
        stage_max=float(inside.max()),
        frac_at_h_min=frac_at_h_min,
        sink_fill_ha=sink_fill_ha,
        controls=controls,
        tif_path=tif_path,
        json_path=json_path,
    )


def load_flood_map(
    path: Path | str | None = None,
) -> tuple[np.ndarray, rasterio.Affine, str]:
    """Load the flood map: raw array (-9999 outside), transform, CRS."""
    target = DEFAULT_FLOOD_PATH if path is None else Path(path)
    with rasterio.open(target) as src:
        return src.read(1), src.transform, str(src.crs)
