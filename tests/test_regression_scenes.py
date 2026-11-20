"""Regression on the control scenes (AGENTS.md golden numbers) + haze scenes.

Runs classify (with the day's water mirror, as `smolf classify` does) +
quality + bed correction from data/cache and asserts the control values
exactly. The ``mirror=None`` tests keep the pre-rescue v2026-10-22 figures.
Skips with a clear reason when the scene is not cached (CI). No network:
the cache-hit path never leaves the machine.
"""

from datetime import date, datetime

import numpy as np
import pytest

from smol_frontier import paths
from smol_frontier.classify import pipeline
from smol_frontier.classify.masks import scl_invalid, spectral_cloud
from smol_frontier.config import settings
from smol_frontier.hydro.bed import correct_bed, load_day_mirror
from smol_frontier.ingest import cache_path, ingest_scene
from smol_frontier.quality import QualityReport, assess_quality
from smol_frontier.sources.stac import SceneMeta

SCENE_2026_10_12 = "S2B_MSIL2A_20261012T170239_R069_T14QMH_20261012T223155"
SCENE_2024_06_09 = "S2A_MSIL2A_20240609T165901_R069_T14QMH_20240610T011221"
SCENE_2025_07_29 = "S2B_MSIL2A_20250729T165849_R069_T14QMH_20250729T221939"
SCENE_ES_2025_07_29 = "S2B_14QMH_20250729_0_L2A"
SCENE_ES_2025_07_24 = "S2C_14QMH_20250724_0_L2A"
SCENE_ES_2024_06_09 = "S2A_14QMH_20240609_0_L2A"
SCENE_ES_2023_01_21 = "S2B_14QMH_20230121_0_L2A"
SCENE_ES_2026_11_08 = "S2A_14QMH_20261108_0_L2A"


def _classify_cached(scene_id: str, day: date, *, rescue: bool = True):
    """Classify a cached scene (skip when absent); return result, quality, bed.

    With ``rescue`` the day's water mirror is loaded and passed, as the CLI
    and the series do; without it classify() gets ``mirror=None``.
    """
    if not cache_path(paths.cache_dir(), scene_id).exists():
        pytest.skip(f"scene {scene_id} not in data/cache (CI): run `smolf fetch {day}`")
    meta = SceneMeta(
        id=scene_id,
        datetime=datetime(day.year, day.month, day.day),
        source="cache",
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    ing = ingest_scene(meta)
    mirror = None
    if rescue:
        mirror = load_day_mirror(day, ing.transform, ing.crs, np.shape(ing.inside))
        assert mirror is not None
    result = pipeline.classify(ing, mirror)
    quality = assess_quality(result)
    bed = correct_bed(result.class_map, ing.transform, ing.crs, day)
    return result, quality, bed


def _assert_control_figures(result, dense, interface, water, soil, total, pct) -> None:
    ha = result.ha_per_class
    assert round(ha[settings.CLASS_DENSE_HYACINTH], 2) == dense
    assert round(ha[settings.CLASS_INTERFACE], 2) == interface
    assert round(ha[settings.CLASS_WATER], 2) == water
    assert round(ha[settings.CLASS_BARE_SOIL], 2) == soil
    assert round(result.hyacinth_total_ha, 2) == total
    assert round(result.pct_reservoir, 2) == pct
    assert round(result.reservoir_area_ha, 2) == 1261.75


def _assert_quality(quality: QualityReport, light: str, regime: str) -> None:
    assert quality.light == light
    assert quality.regime == regime


def _ingest_cached(scene_id: str, day: date):
    """Ingest a cached scene (skip when absent); return the ingested scene."""
    if not cache_path(paths.cache_dir(), scene_id).exists():
        pytest.skip(f"scene {scene_id} not in data/cache (CI): run `smolf fetch {day}`")
    meta = SceneMeta(
        id=scene_id,
        datetime=datetime(day.year, day.month, day.day),
        source="cache",
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    return ingest_scene(meta)


def _spectral_only_fraction(ing) -> float:
    """Fraction of reservoir pixels flagged by spectral_cloud, not by SCL."""
    inside = np.asanyarray(ing.inside, dtype=bool)
    spec = spectral_cloud(
        ing.reflectance("B02"), ing.reflectance("B11"), ing.reflectance("B04")
    )
    only = spec & ~scl_invalid(ing.bands["SCL"]) & inside
    return float(only.sum()) / float(inside.sum())


def test_control_scene_2026_10_12() -> None:
    """High-water control (moved by #69): 6.29 ha rescued under haze.

    Author (v2026-11-19, Earth Search): 824.63 = dense 347.49 + interface
    477.14, under haze 6.29. Same total and haze area; our dense/interface
    split differs (350.24 / 474.39), a gap that already existed in
    v2026-10-22 (see test_control_scene_2026_10_12_no_rescue).
    """
    result, quality, bed = _classify_cached(SCENE_2026_10_12, date(2026, 10, 12))
    _assert_control_figures(result, 350.24, 474.39, 415.86, 0.11, 824.63, 65.36)
    assert round(result.hyacinth_under_haze_ha, 2) == 6.29
    assert round(result.valid_fraction, 4) == 0.9782
    assert round(result.cloud_fraction, 4) == 0.0218
    _assert_quality(quality, "GOOD", "")
    assert bed.applied is True
    assert bed.level_m == 2018.29
    assert bed.margin_m == 2018.50
    assert round(bed.expected_bed_ha, 2) == 0.00
    assert round(bed.removed_ha, 2) == 0.00
    assert round(bed.hyacinth_corrected_ha, 2) == 824.63


def test_control_scene_2026_10_12_no_rescue() -> None:
    """mirror=None keeps the v2026-10-22 control: 818.34 ha, nothing under haze."""
    result, quality, bed = _classify_cached(
        SCENE_2026_10_12, date(2026, 10, 12), rescue=False
    )
    _assert_control_figures(result, 350.24, 468.10, 415.86, 0.11, 818.34, 64.86)
    assert result.hyacinth_under_haze_ha == 0.0
    _assert_quality(quality, "GOOD", "")
    assert round(bed.hyacinth_corrected_ha, 2) == 818.34


def test_control_scene_2024_06_09() -> None:
    """Dry-season control (moved by #69): raw 434.21 ha → 15.70 ha kept.

    Planetary Computer item. Author (Earth Search): raw 434.28 → corrected
    15.63, under haze 1.90; the Earth Search copy here measures 434.35 →
    15.66, haze 1.90 (test_control_scene_2024_06_09_earth_search) — the
    ~0.1 ha gap is the STAC backend, as before.
    """
    result, quality, bed = _classify_cached(SCENE_2024_06_09, date(2024, 6, 9))
    _assert_control_figures(result, 171.36, 262.85, 700.98, 98.67, 434.21, 34.41)
    assert round(result.hyacinth_under_haze_ha, 2) == 1.96
    assert round(result.valid_fraction, 4) == 0.9763
    _assert_quality(quality, "GOOD", "EXPOSED_BED")
    assert bed.applied is True
    assert bed.level_m == 2004.60
    assert bed.margin_m == 2005.54
    assert round(bed.expected_bed_ha, 2) == 696.80
    assert round(bed.removed_ha, 2) == 418.51
    assert round(bed.hyacinth_corrected_ha, 2) == 15.70
    assert round(bed.dense_corrected_ha, 2) == 4.18
    assert round(bed.interface_corrected_ha, 2) == 11.52
    assert round(bed.pct_corrected, 2) == 1.24


def test_control_scene_2024_06_09_earth_search() -> None:
    """Earth Search copy of the dry-season control, the author's backend.

    Author: raw 434.28 → corrected 15.63, under haze 1.90 (Δ 0.07 / 0.03).
    """
    result, quality, bed = _classify_cached(SCENE_ES_2024_06_09, date(2024, 6, 9))
    assert round(result.hyacinth_total_ha, 2) == 434.35
    assert round(result.hyacinth_under_haze_ha, 2) == 1.90
    _assert_quality(quality, "GOOD", "EXPOSED_BED")
    assert round(bed.removed_ha, 2) == 418.69
    assert round(bed.hyacinth_corrected_ha, 2) == 15.66


def test_control_scene_2024_06_09_no_rescue() -> None:
    """mirror=None keeps the v2026-10-22 control: raw 432.25 → 13.74 ha."""
    result, quality, bed = _classify_cached(
        SCENE_2024_06_09, date(2024, 6, 9), rescue=False
    )
    _assert_control_figures(result, 171.02, 261.23, 700.98, 98.65, 432.25, 34.26)
    assert result.hyacinth_under_haze_ha == 0.0
    _assert_quality(quality, "GOOD", "EXPOSED_BED")
    assert round(bed.expected_bed_ha, 2) == 696.80
    assert round(bed.removed_ha, 2) == 418.51
    assert round(bed.hyacinth_corrected_ha, 2) == 13.74
    assert round(bed.dense_corrected_ha, 2) == 3.84
    assert round(bed.interface_corrected_ha, 2) == 9.90
    assert round(bed.pct_corrected, 2) == 1.09


def test_haze_rescue_2023_01_21() -> None:
    """Hyacinth under thin haze: 71.27 ha before the rescue, 158.18 after.

    Author (v2026-11-19): 158.18 ha raw, 86.88 under haze (Δ 0.03).
    """
    result, quality, bed = _classify_cached(SCENE_ES_2023_01_21, date(2023, 1, 21))
    assert round(result.hyacinth_total_ha, 2) == 158.18
    assert round(result.hyacinth_under_haze_ha, 2) == 86.91
    assert round(result.valid_fraction, 4) == 0.9110
    assert round(result.cloud_fraction, 4) == 0.0890
    _assert_quality(quality, "GOOD", "TURBIDITY")
    assert round(bed.hyacinth_corrected_ha, 2) == 158.18


def test_haze_rescue_2026_11_08() -> None:
    """Hyacinth under thin haze: 730.99 ha before the rescue, 780.95 after.

    Author (v2026-11-19): 780.95 ha, 61.9 % of the reservoir, 49.96 under
    haze. The Planetary Computer copy of this date measures the same.
    """
    result, quality, bed = _classify_cached(SCENE_ES_2026_11_08, date(2026, 11, 8))
    assert round(result.hyacinth_total_ha, 2) == 780.95
    assert round(result.pct_reservoir, 2) == 61.89
    assert round(result.hyacinth_under_haze_ha, 2) == 49.96
    assert round(result.valid_fraction, 4) == 0.9587
    assert round(result.cloud_fraction, 4) == 0.0413
    _assert_quality(quality, "GOOD", "")
    assert round(bed.hyacinth_corrected_ha, 2) == 780.95


def test_haze_scene_2025_07_29_is_poor() -> None:
    """Spectral-cloud regression: SCL-missed haze → POOR, out of the series.

    The Earth Search copy of this date measures identically (valid 0.5462,
    cloud 0.4538, POOR), so the verdict is backend-stable. The haze rescue
    (NDVI > 0.5 inside the mirror) adds 93.36 ha (451.65 → 545.01 ha raw,
    the author's "93 ha") but never touches the valid mask, so the scene
    stays POOR.
    """
    result, quality, _bed = _classify_cached(SCENE_2025_07_29, date(2025, 7, 29))
    assert quality.light == "POOR"
    assert result.valid_fraction < settings.MIN_VALID_FRACTION
    assert round(result.valid_fraction, 4) == 0.5462
    assert round(result.cloud_fraction, 4) == 0.4538
    assert round(result.hyacinth_total_ha, 2) == 545.01
    assert round(result.hyacinth_under_haze_ha, 2) == 93.36


def test_cloud_test_separates_haze_from_control() -> None:
    """Spectral cloud test (CLOUD_B02_MIN/B11_MIN/WHITENESS): haze vs control.

    2025-07-29 haze: 43.44 % of reservoir pixels are flagged by
    spectral_cloud that SCL did not flag, so the scene is POOR. 2025-07-24
    turbid control: the spectral test flags nothing (0.0000), so the scene
    stays GOOD / TURBIDITY with the reference figures (valid 1.0,
    total 486.41 ha).
    """
    haze = _ingest_cached(SCENE_ES_2025_07_29, date(2025, 7, 29))
    haze_result = pipeline.classify(haze)
    haze_quality = assess_quality(haze_result)
    assert haze_quality.light == "POOR"
    assert round(_spectral_only_fraction(haze), 4) == 0.4344
    assert round(haze_result.valid_fraction, 4) == 0.5462
    assert round(haze_result.cloud_fraction, 4) == 0.4538
    assert round(haze_result.hyacinth_total_ha, 2) == 451.64

    control = _ingest_cached(SCENE_ES_2025_07_24, date(2025, 7, 24))
    control_result = pipeline.classify(control)
    control_quality = assess_quality(control_result)
    _assert_quality(control_quality, "GOOD", "TURBIDITY")
    assert round(_spectral_only_fraction(control), 4) == 0.0
    assert round(control_result.valid_fraction, 4) == 1.0
    assert round(control_result.hyacinth_total_ha, 2) == 486.41
