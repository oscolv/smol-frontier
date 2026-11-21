"""The published map: rebuild the latest scene's class map and verify it
against the run (cache hash, per-class hectares, inputs, removed hectares)."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS

from smol_frontier import ingest, paths, series
from smol_frontier.classify.display import DisplayImage, render_display_image
from smol_frontier.classify.pipeline import classify as classify_scene
from smol_frontier.config import settings
from smol_frontier.hydro import curve, flood_map
from smol_frontier.hydro.bed import bed_margin, day_mirror, exposed_bed_mask
from smol_frontier.ingest import ingest_scene
from smol_frontier.publish.errors import PublishRefused, _require_dict
from smol_frontier.sources.stac import SceneMeta, get_scene

# WHY: the one subdirectory a contract has (holds the map files).
_MAP_DIR = "map"

# WHY: the classified hectares publish re-derives must equal the run row to
#   the hundredth (the published decimals) — (class code, run row field).
_HECTARE_CHECKS = (
    (settings.CLASS_DENSE_HYACINTH, "dense_ha"),
    (settings.CLASS_INTERFACE, "interface_ha"),
    (settings.CLASS_WATER, "water_ha"),
    (settings.CLASS_BARE_SOIL, "soil_ha"),
)


@dataclass(frozen=True, eq=False)
class PublishedMap:
    """The published class map (0-5) on the scene's native grid."""

    class_map: np.ndarray
    transform: rasterio.Affine
    crs: str
    display: DisplayImage


def _map_stem(record: dict[str, object]) -> str:
    """The one place a map file name is derived: ``map/<date>_<item_id>``."""
    return f"{_MAP_DIR}/{record['date']}_{record['item_id_used']}"


def map_path(record: dict[str, object]) -> str:
    """Relative path of a scene's map GeoTIFF: ``map/<date>_<item_id>.tif``."""
    return f"{_map_stem(record)}.tif"


def png_path(record: dict[str, object]) -> str:
    """Relative path of a scene's display PNG: ``map/<date>_<item_id>.png``."""
    return f"{_map_stem(record)}.png"


def _cached_scene(record: dict[str, object]) -> ingest.IngestedScene:
    """The scene exactly as the run cached it, verified by sha256.

    A scene missing from the cache is re-ingested by its exact item id and
    backend (no search, no fallback: another item could never match the
    recorded hash). Either way the cache file must hash to the record's
    ``cache_sha256`` before it is read.
    """
    item_id = str(record["item_id_used"])
    backend = str(record["backend_used"])
    cache_dir = paths.cache_dir()
    path = ingest.cache_path(cache_dir, item_id)
    if not path.is_file():
        try:
            ingest_scene(get_scene(backend, item_id), cache_dir=cache_dir)
        except Exception as exc:  # WHY broad: any re-ingest failure refuses
            msg = f"scene {item_id} is not cached and re-ingest failed: {exc}"[:300]
            raise PublishRefused(msg) from exc
    expected = record.get("cache_sha256")
    actual = series.sha256_file(path)
    if actual is None or actual != expected:
        msg = (
            f"cache sha256 of {item_id} ({actual}) differs from the scene "
            f"record ({expected})"
        )
        raise PublishRefused(msg)
    # WHY a stub: on a cache hit ingest_scene rebuilds the scene metadata
    #   from the cache tags; only the id (cache key) is used from here.
    stub = SceneMeta(
        id=item_id,
        datetime=datetime.fromisoformat(str(record["date"])),
        source=backend,
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    return ingest_scene(stub, cache_dir=cache_dir)


def _check_input(manifest: dict[str, object], key: str, path: Path, label: str) -> str:
    """Refuse unless ``path`` hashes to the run manifest's ``inputs[key]``.

    Returns the verified sha256.
    """
    inputs = manifest.get("inputs")
    expected = inputs.get(key) if isinstance(inputs, dict) else None
    actual = series.sha256_file(path)
    if expected is None or actual != expected:
        msg = (
            f"{label} sha256 ({actual}) differs from the run manifest "
            f"inputs ({expected})"
        )
        raise PublishRefused(msg)
    return actual


@dataclass(frozen=True, eq=False)
class CommittedCurve:
    """The committed stage-area curve, verified against the run manifest."""

    grid: curve.StageAreaCurve
    sha256: str


def committed_curve(manifest: dict[str, object]) -> CommittedCurve:
    """The committed stage-area curve, if it is the curve the run used.

    WHY on every publish: series.json publishes the curve itself, so it must
    be the run's input even when no scene's correction read it.
    """
    path = curve.DEFAULT_CURVE_CSV_PATH
    sha256 = _check_input(manifest, "stage_area_curve_csv", path, "stage-area curve")
    return CommittedCurve(curve.load_curve(path), sha256)


def _run_margin(grid: curve.StageAreaCurve, row: dict[str, object]) -> float:
    """Full-precision margin the run's bed correction used for the row level.

    WHY recomputed: the run row stores ``margin_m`` rounded to 2 dp, while
    the correction removed pixels with the full-precision margin; masking
    with the rounded value moves pixels whose stage lies between the two
    and breaks the removed_ha check. So the margin is recomputed from the
    row's ``level_m`` (no level lookup) with the committed stage-area curve
    (``grid``, from ``committed_curve``), which must reproduce the row margin.
    """
    margin = bed_margin(
        float(row["level_m"]),
        grid.stage_m,
        grid.area_km2,
        grid.area_p2_5,
        grid.area_p97_5,
    )
    if round(margin, 2) != round(float(row["margin_m"]), 2):
        msg = (
            f"recomputed margin {round(margin, 2)} m differs from the run "
            f"margin_m {round(float(row['margin_m']), 2)} m"
        )
        raise PublishRefused(msg)
    return margin


def _flood_stage_for(
    manifest: dict[str, object], ing: ingest.IngestedScene, shape: tuple[int, ...]
) -> np.ndarray:
    """The flood-stage map the run used, on the scene's grid (else refuse)."""
    path = flood_map.DEFAULT_FLOOD_PATH
    _check_input(manifest, "flood_stage_tif", path, "flood-stage map")
    flood, transform, crs = flood_map.load_flood_map(path)
    if (
        flood.shape != shape
        or transform != ing.transform
        or CRS.from_string(crs) != CRS.from_string(ing.crs)
    ):
        msg = "flood-stage map grid differs from the scene grid"
        raise PublishRefused(msg)
    return flood


def build_map(
    record: dict[str, object],
    manifest: dict[str, object],
    committed: CommittedCurve,
) -> PublishedMap:
    """Re-classify the record's scene and overlay class 5 (C17-C19).

    Refuses (PublishRefused) when the cache hash, the per-class hectares,
    the flood-stage map hash or the removed hectares disagree with the run.
    ``committed`` is the curve ``committed_curve`` verified for this run.
    Class 5 and the haze-rescue mirror use the run row's level and the
    margin recomputed from it (never a fresh level lookup), and only when
    the correction was applied.
    """
    row = _require_dict(record["row"], f"row of {record['item_id_used']}")
    item_id = record["item_id_used"]
    ing = _cached_scene(record)
    # WHY the mirror follows bed_applied: the run loaded a mirror exactly
    #   when the correction applied (same level, flood map, curve and grid
    #   checks), so re-classifying needs the same one to match its hectares.
    flood = margin = mirror = None
    if row["bed_applied"]:
        flood = _flood_stage_for(manifest, ing, np.shape(ing.inside))
        margin = _run_margin(committed.grid, row)
        mirror = day_mirror(flood, margin)
    result = classify_scene(ing, mirror)
    mismatches = [
        f"{field} {round(result.ha_per_class[code], 2)} != run "
        f"{round(float(row[field]), 2)}"
        for code, field in _HECTARE_CHECKS
        if round(result.ha_per_class[code], 2) != round(float(row[field]), 2)
    ]
    under_haze = round(result.hyacinth_under_haze_ha, 2)
    if under_haze != round(float(row["hyacinth_under_haze_ha"]), 2):
        mismatches.append(
            f"hyacinth_under_haze_ha {under_haze} != run "
            f"{round(float(row['hyacinth_under_haze_ha']), 2)}"
        )
    if mismatches:
        msg = f"re-classified {item_id} differs from the run: " + "; ".join(mismatches)
        raise PublishRefused(msg)
    # WHY no dtype cast: the guarded writer must see the classifier's own
    #   dtype, so a non-uint8 map is rejected instead of silently truncated.
    published = np.array(result.class_map, copy=True)
    if flood is not None and margin is not None:
        removed = exposed_bed_mask(published, flood, float(row["level_m"]), margin)
        removed_ha = round(float(removed.sum()) * flood_map.PIXEL_HA, 2)
        if removed_ha != round(float(row["removed_ha"]), 2):
            msg = (
                f"class-5 area of {item_id} {removed_ha} ha differs from the run "
                f"removed_ha {round(float(row['removed_ha']), 2)}"
            )
            raise PublishRefused(msg)
        published[removed] = settings.CLASS_BED_REMOVED
    # the C21 guard runs here and again in each published-raster writer
    display = render_display_image(published, ing.transform, ing.crs)
    return PublishedMap(published, ing.transform, ing.crs, display)
