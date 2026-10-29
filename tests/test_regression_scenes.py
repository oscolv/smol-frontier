"""Regression on the two control scenes (AGENTS.md golden numbers).

Runs classify + quality + bed correction from data/cache and asserts the
control values exactly. Skips with a clear reason when the scene is not
cached (CI). No network: the cache-hit path never leaves the machine.
"""

from datetime import date, datetime

import pytest

from smol_frontier.classify import pipeline
from smol_frontier.config import settings
from smol_frontier.hydro.bed import correct_bed
from smol_frontier.ingest import DEFAULT_CACHE_DIR, _cache_path, ingest_scene
from smol_frontier.quality import QualityReport, assess_quality
from smol_frontier.sources.stac import SceneMeta

SCENE_2026_10_12 = "S2B_MSIL2A_20261012T170239_R069_T14QMH_20261012T223155"
SCENE_2024_06_09 = "S2A_MSIL2A_20240609T165901_R069_T14QMH_20240610T011221"


def _classify_cached(scene_id: str, day: date):
    """Classify a cached scene (skip when absent); return ing, result, quality, bed."""
    if not _cache_path(DEFAULT_CACHE_DIR, scene_id).exists():
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
    result = pipeline.classify(ing)
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


def test_control_scene_2026_10_12() -> None:
    """High-water control: GOOD, clean regime, bed correction removes nothing."""
    result, quality, bed = _classify_cached(SCENE_2026_10_12, date(2026, 10, 12))
    _assert_control_figures(result, 350.24, 468.10, 415.86, 0.11, 818.34, 64.86)
    _assert_quality(quality, "GOOD", "")
    assert bed.applied is True
    assert bed.level_m == 2018.29
    assert bed.margin_m == 2018.50
    assert round(bed.expected_bed_ha, 2) == 0.00
    assert round(bed.removed_ha, 2) == 0.00
    assert round(bed.hyacinth_corrected_ha, 2) == 818.34


def test_control_scene_2024_06_09() -> None:
    """Dry-season control: raw 432.25 ha of bed vegetation → 13.74 ha kept."""
    result, quality, bed = _classify_cached(SCENE_2024_06_09, date(2024, 6, 9))
    _assert_control_figures(result, 171.02, 261.23, 700.98, 98.65, 432.25, 34.26)
    _assert_quality(quality, "GOOD", "EXPOSED_BED")
    assert bed.applied is True
    assert bed.level_m == 2004.60
    assert bed.margin_m == 2005.54
    assert round(bed.expected_bed_ha, 2) == 696.80
    assert round(bed.removed_ha, 2) == 418.51
    assert round(bed.hyacinth_corrected_ha, 2) == 13.74
    assert round(bed.dense_corrected_ha, 2) == 3.84
    assert round(bed.interface_corrected_ha, 2) == 9.90
    assert round(bed.pct_corrected, 2) == 1.09
