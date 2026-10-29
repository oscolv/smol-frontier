"""Exposed-bed correction: hyacinth on safely exposed bed does not count.

In the dry season, vegetation on the exposed reservoir bed classifies as
hyacinth. Crossing the class map with the flood-stage map and the day's SIH
level removes hyacinth on safe bed (above the level plus an uncertainty
margin); hyacinth in the mirror or the margin band keeps counting. A
post-process of the report only — the class map is NOT changed. Method
reproduced from the author's prior research.
"""

from dataclasses import dataclass, replace
from datetime import date

import numpy as np
import rasterio
from rasterio.crs import CRS

from smol_frontier.aoi import reservoir_area_ha
from smol_frontier.config import settings
from smol_frontier.hydro.curve import DEFAULT_CURVE_CSV_PATH, load_curve
from smol_frontier.hydro.flood_map import (
    DEFAULT_FLOOD_PATH,
    FLOOD_NODATA,
    PIXEL_HA,
    load_flood_map,
)
from smol_frontier.hydro.levels import level_on

__all__ = [
    "BedCorrection",
    "compute_bed_correction",
    "correct_bed",
    "cushion_ha",
]


@dataclass(frozen=True)
class BedCorrection:
    """Exposed-bed verdict: corrected figures, or a reason they stand raw."""

    applied: bool
    reason: str
    level_m: float | None
    margin_m: float | None
    expected_bed_ha: float | None
    removed_ha: float | None
    hyacinth_corrected_ha: float | None
    dense_corrected_ha: float | None
    interface_corrected_ha: float | None
    pct_corrected: float | None


def cushion_ha(
    h: float,
    stage_m: np.ndarray,
    area_p2_5: np.ndarray,
    area_p97_5: np.ndarray,
) -> float:
    """Uncertainty cushion in ha: model error vs bootstrap half-width.

    The model leg interpolates the author's anchor-weight errors (clipped
    outside 2005–2018); the bootstrap leg is half the CI width at h
    (clipped to the curve range). Pure.
    """
    table = settings.BED_CUSHION_MODEL_HA
    model = np.interp(h, [point[0] for point in table], [point[1] for point in table])
    half_width = (
        (
            float(np.interp(h, stage_m, area_p97_5))
            - float(np.interp(h, stage_m, area_p2_5))
        )
        / 2.0
        * 100.0
    )
    return float(max(model, half_width))


def compute_bed_correction(
    class_map: np.ndarray,
    flood_stage: np.ndarray,
    level: float,
    stage_m: np.ndarray,
    area_km2: np.ndarray,
    area_p2_5: np.ndarray,
    area_p97_5: np.ndarray,
    reservoir_ha: float,
) -> BedCorrection:
    """Correct hyacinth for exposed bed from arrays (pure; always applied).

    margin_m carries full precision here; correct_bed() rounds it for the
    report. Zones use the full-precision margin.
    """
    s = settings
    classes = np.asanyarray(class_map)
    flood = np.asanyarray(flood_stage, dtype=np.float64)
    curve_h = np.asanyarray(stage_m, dtype=np.float64)
    curve_a = np.asanyarray(area_km2, dtype=np.float64) * 100.0
    level_area = float(np.interp(level, curve_h, curve_a))
    cushion = cushion_ha(level, curve_h, area_p2_5, area_p97_5)
    margin = max(level, float(np.interp(level_area + cushion, curve_a, curve_h)))

    staged = flood != FLOOD_NODATA
    bed = staged & (flood > level)
    mirror = staged & ~bed
    band = bed & (flood <= margin)
    safe_bed = bed & ~band
    hyacinth = (classes == s.CLASS_DENSE_HYACINTH) | (classes == s.CLASS_INTERFACE)
    kept = hyacinth & (mirror | band)
    removed = hyacinth & safe_bed
    corrected_ha = float(kept.sum()) * PIXEL_HA
    return BedCorrection(
        applied=True,
        reason="",
        level_m=level,
        margin_m=margin,
        expected_bed_ha=float(bed.sum()) * PIXEL_HA,
        removed_ha=float(removed.sum()) * PIXEL_HA,
        hyacinth_corrected_ha=corrected_ha,
        dense_corrected_ha=float((kept & (classes == s.CLASS_DENSE_HYACINTH)).sum())
        * PIXEL_HA,
        interface_corrected_ha=float((kept & (classes == s.CLASS_INTERFACE)).sum())
        * PIXEL_HA,
        pct_corrected=corrected_ha / reservoir_ha * 100.0,
    )


def _not_applied(reason: str, level: float | None) -> BedCorrection:
    """BedCorrection with no figures (raw stands) and the reason why."""
    return BedCorrection(
        applied=False,
        reason=reason,
        level_m=level,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
        hyacinth_corrected_ha=None,
        dense_corrected_ha=None,
        interface_corrected_ha=None,
        pct_corrected=None,
    )


def correct_bed(
    class_map: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
    day: date,
) -> BedCorrection:
    """Load inputs, check the grid, and correct hyacinth for exposed bed.

    Missing level, missing files, or a grid (shape, transform, crs) that
    differs from the flood map → applied=False with the reason. Nothing is
    ever downloaded here.
    """
    level = level_on(day)
    if level is None:
        return _not_applied(f"no SIH level for {day.isoformat()}", None)
    if not DEFAULT_FLOOD_PATH.exists():
        return _not_applied(f"flood map not found: {DEFAULT_FLOOD_PATH}", level)
    if not DEFAULT_CURVE_CSV_PATH.exists():
        return _not_applied(
            f"stage-area curve not found: {DEFAULT_CURVE_CSV_PATH}", level
        )
    flood, flood_transform, flood_crs = load_flood_map()
    problems: list[str] = []
    if tuple(np.shape(class_map)) != tuple(np.shape(flood)):
        problems.append(
            f"shape {tuple(np.shape(class_map))} != flood {tuple(np.shape(flood))}"
        )
    if transform != flood_transform:
        problems.append("transform differs from flood map")
    if CRS.from_string(crs) != CRS.from_string(flood_crs):
        problems.append(f"crs {crs} != flood {flood_crs}")
    if problems:
        return _not_applied(
            "grid differs from flood map: " + "; ".join(problems), level
        )
    curve = load_curve()
    numbers = compute_bed_correction(
        class_map,
        flood,
        level,
        curve.stage_m,
        curve.area_km2,
        curve.area_p2_5,
        curve.area_p97_5,
        reservoir_area_ha(),
    )
    assert numbers.margin_m is not None  # pure result always carries a margin
    return replace(numbers, margin_m=round(numbers.margin_m, 2))
