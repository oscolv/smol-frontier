"""Tests for the per-scene quality traffic light and regime.

Pure functions over hand-built SceneResult objects — no scenes are ingested
or classified here.
"""

import math

import numpy as np
import pytest

import smol_frontier.classify.gmm as gmm_module
from smol_frontier.classify.pipeline import SceneResult
from smol_frontier.config import settings
from smol_frontier.quality import assess_quality, regime


def _scene(**overrides: object) -> SceneResult:
    """Hand-built SceneResult: GOOD, clean, publishable unless overridden."""
    s = settings
    fields: dict[str, object] = {
        "scene_id": "test",
        "class_map": np.zeros((2, 2), dtype=np.uint8),
        "ha_per_class": {
            s.CLASS_NODATA: 0.0,
            s.CLASS_WATER: 100.0,
            s.CLASS_DENSE_HYACINTH: 50.0,
            s.CLASS_INTERFACE: 10.0,
            s.CLASS_BARE_SOIL: 0.0,
        },
        "hyacinth_total_ha": 60.0,
        "pct_reservoir": 10.0,
        "reservoir_area_ha": 600.0,
        "valid_fraction": 0.99,
        "cloud_fraction": 0.0,
        "gmm1_sep": 5.0,
        "gmm2_sep": 3.0,
        "nir_thr_used": None,
        "nir_water_frac": 0.3,
        "n_rescued": 0,
        "n_substrate": 0,
    }
    fields.update(overrides)
    return SceneResult(**fields)  # type: ignore[arg-type]


def _soil_scene(soil_ha: float) -> SceneResult:
    s = settings
    return _scene(
        ha_per_class={
            s.CLASS_NODATA: 0.0,
            s.CLASS_WATER: 100.0,
            s.CLASS_DENSE_HYACINTH: 50.0,
            s.CLASS_INTERFACE: 10.0,
            s.CLASS_BARE_SOIL: soil_ha,
        }
    )


def test_baseline_is_good_clean_publishable() -> None:
    report = assess_quality(_scene())
    assert report.light == "GOOD"
    assert report.degrading_flags == []
    assert report.info_flags == []
    assert report.publishable_pct is True
    assert report.regime == ""
    assert report.valid_fraction == 0.99
    assert report.cloud_fraction == 0.0
    assert report.gmm1_sep == 5.0
    assert report.gmm2_sep == 3.0


@pytest.mark.parametrize(
    ("valid", "flagged"),
    [(0.85, False), (0.8499, True), (0.80, True), (1.0, False)],
)
def test_partial_clouds_boundary(valid: float, flagged: bool) -> None:
    report = assess_quality(_scene(valid_fraction=valid))
    assert ("PARTIAL_CLOUDS" in report.degrading_flags) is flagged
    assert report.light == ("DOUBTFUL" if flagged else "GOOD")


@pytest.mark.parametrize(
    ("cloud", "flagged"),
    [(0.10, False), (0.1001, True), (0.0, False), (0.5, True)],
)
def test_high_cloud_boundary(cloud: float, flagged: bool) -> None:
    report = assess_quality(_scene(cloud_fraction=cloud))
    assert ("HIGH_CLOUD" in report.degrading_flags) is flagged
    assert report.light == ("DOUBTFUL" if flagged else "GOOD")


def test_cloud_nan_does_not_fire() -> None:
    report = assess_quality(_scene(cloud_fraction=math.nan))
    assert report.degrading_flags == []
    assert report.light == "GOOD"


@pytest.mark.parametrize(
    ("sep", "flagged"),
    [(1.5, False), (1.4999, True), (5.0, False), (math.inf, False)],
)
def test_gmm_water_overlap_boundary(sep: float, flagged: bool) -> None:
    report = assess_quality(_scene(gmm1_sep=sep))
    assert ("GMM_WATER_OVERLAP" in report.degrading_flags) is flagged
    assert report.light == ("DOUBTFUL" if flagged else "GOOD")


def test_gmm1_sep_nan_fires_overlap() -> None:
    report = assess_quality(_scene(gmm1_sep=math.nan))
    assert report.degrading_flags == ["GMM_WATER_OVERLAP"]
    assert report.light == "DOUBTFUL"


def test_poor_below_min_valid_fraction() -> None:
    report = assess_quality(_scene(valid_fraction=0.79))
    assert report.light == "POOR"
    assert report.degrading_flags == ["PARTIAL_CLOUDS"]
    assert report.publishable_pct is False


def test_poor_lists_all_degrading_flags() -> None:
    report = assess_quality(
        _scene(valid_fraction=0.79, cloud_fraction=0.5, gmm1_sep=0.5)
    )
    assert report.light == "POOR"
    assert report.degrading_flags == [
        "PARTIAL_CLOUDS",
        "HIGH_CLOUD",
        "GMM_WATER_OVERLAP",
    ]


def test_min_valid_fraction_boundary_is_doubtful_not_poor() -> None:
    report = assess_quality(_scene(valid_fraction=0.80))
    assert report.light == "DOUBTFUL"
    assert report.degrading_flags == ["PARTIAL_CLOUDS"]


@pytest.mark.parametrize("sep", [math.nan, math.inf, -math.inf])
def test_no_mat_when_gmm2_not_finite(sep: float) -> None:
    report = assess_quality(_scene(gmm2_sep=sep))
    assert report.info_flags == ["NO_MAT"]
    assert report.light == "GOOD"


def test_no_mat_absent_when_gmm2_finite() -> None:
    report = assess_quality(_scene(gmm2_sep=0.1))
    assert "NO_MAT" not in report.info_flags


def test_adaptive_nir_flag() -> None:
    assert assess_quality(_scene(nir_thr_used=0.25)).info_flags == ["ADAPTIVE_NIR"]
    assert assess_quality(_scene(nir_thr_used=None)).info_flags == []


@pytest.mark.parametrize(
    ("frac", "flagged"),
    [(0.70, False), (0.7001, True), (0.3, False), (math.nan, False)],
)
def test_high_turbidity_boundary(frac: float, flagged: bool) -> None:
    report = assess_quality(_scene(nir_water_frac=frac))
    assert ("HIGH_TURBIDITY" in report.info_flags) is flagged
    assert report.light == "GOOD"


def test_interface_dominant_flag() -> None:
    s = settings
    dominant = _scene(
        ha_per_class={
            s.CLASS_NODATA: 0.0,
            s.CLASS_WATER: 100.0,
            s.CLASS_DENSE_HYACINTH: 10.0,
            s.CLASS_INTERFACE: 50.0,
            s.CLASS_BARE_SOIL: 0.0,
        }
    )
    assert assess_quality(dominant).info_flags == ["INTERFACE_DOMINANT"]
    tied = _scene(
        ha_per_class={
            s.CLASS_NODATA: 0.0,
            s.CLASS_WATER: 100.0,
            s.CLASS_DENSE_HYACINTH: 30.0,
            s.CLASS_INTERFACE: 30.0,
            s.CLASS_BARE_SOIL: 0.0,
        }
    )
    assert "INTERFACE_DOMINANT" not in assess_quality(tied).info_flags


def test_info_flags_never_change_the_light() -> None:
    s = settings
    result = _scene(
        ha_per_class={
            s.CLASS_NODATA: 0.0,
            s.CLASS_WATER: 100.0,
            s.CLASS_DENSE_HYACINTH: 10.0,
            s.CLASS_INTERFACE: 50.0,
            s.CLASS_BARE_SOIL: 60.0,
        },
        gmm2_sep=math.nan,
        nir_thr_used=0.25,
        nir_water_frac=0.9,
    )
    report = assess_quality(result)
    assert report.info_flags == [
        "NO_MAT",
        "ADAPTIVE_NIR",
        "HIGH_TURBIDITY",
        "INTERFACE_DOMINANT",
    ]
    assert report.light == "GOOD"
    assert report.regime == "EXPOSED_BED"


@pytest.mark.parametrize(
    ("valid", "publishable"),
    [(0.95, True), (0.9499, False), (0.99, True), (0.90, False)],
)
def test_publishable_pct_boundary(valid: float, publishable: bool) -> None:
    report = assess_quality(_scene(valid_fraction=valid))
    assert report.publishable_pct is publishable


def test_publishability_is_independent_of_the_light() -> None:
    # FRAC_PUBLISHABLE (0.95) sits above FRAC_DOUBTFUL (0.85): a scene with
    # valid in [0.85, 0.95) is GOOD yet its percent must not be published.
    report = assess_quality(_scene(valid_fraction=0.90))
    assert report.publishable_pct is False
    assert report.light == "GOOD"
    assert report.degrading_flags == []


@pytest.mark.parametrize(
    ("soil_ha", "nir_frac", "expected"),
    [
        (60.0, 0.9, "EXPOSED_BED"),  # soil wins on priority
        (60.0, 0.1, "EXPOSED_BED"),
        (50.0, 0.1, ""),  # soil uses strict >
        (50.0001, 0.1, "EXPOSED_BED"),
        (0.0, 0.60, "TURBIDITY"),  # turbidity uses >=
        (0.0, 0.5999, ""),
        (0.0, 0.9, "TURBIDITY"),
        (None, None, ""),
        (float("nan"), float("nan"), ""),
        (None, 0.9, "TURBIDITY"),
        (60.0, None, "EXPOSED_BED"),
    ],
)
def test_regime(soil_ha: float | None, nir_frac: float | None, expected: str) -> None:
    assert regime(soil_ha, nir_frac) == expected


def test_regime_uses_soil_hectares_from_scene() -> None:
    assert assess_quality(_soil_scene(60.0)).regime == "EXPOSED_BED"
    assert assess_quality(_soil_scene(50.0)).regime == ""
    assert assess_quality(_scene(nir_water_frac=0.6)).regime == "TURBIDITY"


def test_sep_doubtful_lives_in_config_not_gmm() -> None:
    assert settings.SEP_DOUBTFUL == 1.5
    assert not hasattr(gmm_module, "SEP_ADAPT_MAX")
