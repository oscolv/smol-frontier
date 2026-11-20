"""Tests for the haze rescue wired into classify(scene, mirror=...)."""

from datetime import datetime

import numpy as np
import pytest
import rasterio

from smol_frontier.aoi import reservoir_area_ha
from smol_frontier.classify import pipeline
from smol_frontier.classify.pipeline import classify
from smol_frontier.config import settings
from smol_frontier.ingest import IngestedScene
from smol_frontier.sources.stac import SceneMeta

# DN per band (offset 0, so reflectance = DN / 10 000).
_WATER = {
    "B02": 300,
    "B03": 450,
    "B04": 500,
    "B05": 350,
    "B08": 350,
    "B8A": 340,
    "B11": 200,
}
_DENSE = {
    "B02": 300,
    "B03": 600,
    "B04": 300,
    "B05": 900,
    "B08": 3800,
    "B8A": 3900,
    "B11": 1500,
}
_INTERFACE = {
    "B02": 400,
    "B03": 600,
    "B04": 500,
    "B05": 900,
    "B08": 1800,
    "B8A": 1900,
    "B11": 900,
}
# Thin haze over hyacinth: spectral cloud (bright, white-ish B02, B11 > 0.10)
# but NDVI ~0.65 > CLOUD_NDVI_MAX.
_HAZE = {
    "B02": 1000,
    "B03": 1100,
    "B04": 900,
    "B05": 1400,
    "B08": 4200,
    "B8A": 4300,
    "B11": 1900,
}
# Grey cloud: spectral cloud with moderate NDVI (~0.2), never rescued.
_GREY = {
    "B02": 2200,
    "B03": 2200,
    "B04": 2000,
    "B05": 2300,
    "B08": 3000,
    "B8A": 3000,
    "B11": 2200,
}

_SHAPE = (40, 40)
_HAZE_BLOCK = (slice(0, 4), slice(30, 34))
_GREY_BLOCK = (slice(5, 8), slice(30, 34))
_SCL_BLOCK = (slice(10, 13), slice(30, 34))
_OUTSIDE_BLOCK = (slice(15, 18), slice(30, 34))


def _region(block) -> np.ndarray:
    mask = np.zeros(_SHAPE, dtype=bool)
    mask[block] = True
    return mask


def _scene(*, mat: bool = True) -> IngestedScene:
    """Water with (optionally) a dense + interface mat and three haze patches.

    The haze patches: one inside the mirror (rescued), one with an invalid
    SCL (cloud shadow, 3), one outside the mirror; plus a grey-cloud patch.
    """
    rng = np.random.default_rng(7)
    bands = {}
    for band, water in _WATER.items():
        arr = np.full(_SHAPE, water, dtype=np.float64)
        if mat:
            arr[:, 10:20] = _DENSE[band]
            arr[:, 20:25] = _INTERFACE[band]
        for block in (_HAZE_BLOCK, _SCL_BLOCK, _OUTSIDE_BLOCK):
            arr[block] = _HAZE[band]
        arr[_GREY_BLOCK] = _GREY[band]
        arr += rng.normal(0.0, 20.0, _SHAPE)
        bands[band] = np.clip(arr, 1, None).astype(np.uint16)
    scl = np.full(_SHAPE, 6, dtype=np.uint16)
    scl[_SCL_BLOCK] = 3
    bands["SCL"] = scl
    meta = SceneMeta(
        id="TEST_HAZE",
        datetime=datetime(2026, 10, 12),
        source="test",
        processing_baseline="05.11",
        boa_offset_applied=True,
        assets={},
    )
    return IngestedScene(
        scene=meta,
        bands=bands,
        inside=np.full(_SHAPE, True),
        transform=rasterio.Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2200000.0),
        crs="EPSG:32614",
        offset=0.0,
    )


def _mirror() -> np.ndarray:
    """Day mirror: everything except the 'outside' patch."""
    return ~_region(_OUTSIDE_BLOCK)


_HYACINTH = (settings.CLASS_DENSE_HYACINTH, settings.CLASS_INTERFACE)
_DIAGNOSTICS = (
    "valid_fraction",
    "cloud_fraction",
    "gmm1_sep",
    "gmm2_sep",
    "nir_thr_used",
    "nir_water_frac",
    "n_rescued",
    "n_substrate",
    "reservoir_area_ha",
    "scene_id",
)


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and np.isnan(a):
        return bool(np.isnan(b))
    return a == b


def test_haze_over_hyacinth_inside_mirror_rescued() -> None:
    result = classify(_scene(), mirror=_mirror())
    haze = result.class_map[_HAZE_BLOCK]
    assert np.isin(haze, _HYACINTH).all()


def test_grey_cloud_invalid_scl_and_outside_mirror_not_rescued() -> None:
    result = classify(_scene(), mirror=_mirror())
    for block in (_GREY_BLOCK, _SCL_BLOCK, _OUTSIDE_BLOCK):
        assert (result.class_map[block] == settings.CLASS_NODATA).all()


def test_mirror_none_and_all_false_match_rescue_off() -> None:
    scene = _scene()
    base = classify(scene)
    for mirror in (None, np.zeros(_SHAPE, dtype=bool)):
        result = classify(scene, mirror=mirror)
        assert (result.class_map == base.class_map).all()
        assert result.ha_per_class == base.ha_per_class
        assert result.hyacinth_total_ha == base.hyacinth_total_ha
        assert result.pct_reservoir == base.pct_reservoir
        assert result.hyacinth_under_haze_ha == 0.0
        for name in _DIAGNOSTICS:
            assert _same(getattr(result, name), getattr(base, name)), name
    # Without the rescue the haze patch is nodata (excluded as cloud).
    assert (base.class_map[_HAZE_BLOCK] == settings.CLASS_NODATA).all()


def test_rescue_leaves_other_pixels_and_diagnostics_unchanged() -> None:
    scene = _scene()
    base = classify(scene)
    result = classify(scene, mirror=_mirror())
    outside = ~_region(_HAZE_BLOCK)
    assert (result.class_map[outside] == base.class_map[outside]).all()
    for name in _DIAGNOSTICS:
        assert _same(getattr(result, name), getattr(base, name)), name


def test_hyacinth_under_haze_ha_counted_in_totals() -> None:
    scene = _scene()
    base = classify(scene)
    result = classify(scene, mirror=_mirror())
    rescued = _region(_HAZE_BLOCK) & np.isin(result.class_map, _HYACINTH)
    expected = float(rescued.sum()) * 0.01
    assert expected > 0
    assert result.hyacinth_under_haze_ha == pytest.approx(expected)
    assert result.hyacinth_total_ha == pytest.approx(base.hyacinth_total_ha + expected)
    assert result.hyacinth_total_ha == pytest.approx(
        result.ha_per_class[settings.CLASS_DENSE_HYACINTH]
        + result.ha_per_class[settings.CLASS_INTERFACE]
    )
    assert result.ha_per_class[settings.CLASS_NODATA] == pytest.approx(
        base.ha_per_class[settings.CLASS_NODATA]
        - float(_region(_HAZE_BLOCK).sum()) * 0.01
    )
    assert result.pct_reservoir == pytest.approx(
        result.hyacinth_total_ha / reservoir_area_ha() * 100.0
    )


def test_early_exit_no_mat_rescues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    real = pipeline.stage1

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        seen.append(out)
        return out

    monkeypatch.setattr(pipeline, "stage1", spy)
    result = classify(_scene(mat=False), mirror=_mirror())
    assert seen and seen[0].no_mat
    assert (result.class_map[_HAZE_BLOCK] == settings.CLASS_NODATA).all()
    assert result.hyacinth_under_haze_ha == 0.0
