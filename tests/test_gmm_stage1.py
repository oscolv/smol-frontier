"""Tests for Stage 1 (synthetic water/mat scenes, pure numpy)."""

import math

import numpy as np
import pytest

import smol_frontier.classify.gmm as gmm_module
from smol_frontier.classify.gmm import (
    MAT_PENDING,
    _is_no_mat,
    border_strip,
    nir_valley,
    stage1,
)
from smol_frontier.config import settings


def _water(rng, n, *, b08=(0.095, 0.015), nsd=(0.08, 0.06)):
    return {
        "fai": rng.normal(0.0, 0.008, n),
        "ndvi": rng.normal(0.02, 0.05, n),
        "ndre": rng.normal(0.0, 0.05, n),
        "mndwi": rng.normal(0.3, 0.1, n),
        "bsi": rng.normal(-0.05, 0.03, n),
        "nsd": rng.normal(*nsd, n),
        "b08": rng.normal(*b08, n),
    }


def _mat(rng, n):
    return {
        "fai": rng.normal(0.09, 0.015, n),
        "ndvi": rng.normal(0.6, 0.08, n),
        "ndre": rng.normal(0.4, 0.1, n),
        "mndwi": rng.normal(-0.3, 0.1, n),
        "bsi": rng.normal(-0.1, 0.05, n),
        "nsd": rng.normal(0.2, 0.05, n),
        "b08": rng.normal(0.28, 0.03, n),
    }


def _scene(water, mat, shape, split):
    scene = {}
    for key in water:
        rows = np.concatenate([water[key], mat[key]])
        scene[key] = rows.reshape(shape)
    scene["mat_rows"] = np.zeros(shape, dtype=bool)
    scene["mat_rows"][split:, :] = True
    return scene


def test_clean_bimodal_water_mat() -> None:
    rng = np.random.default_rng(7)
    shape, split = (80, 50), 50
    scene = _scene(
        _water(rng, split * shape[1]),
        _mat(rng, (shape[0] - split) * shape[1]),
        shape,
        split,
    )
    result = stage1(
        *[scene[k] for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert result.no_mat is False
    assert result.nir_thr_used is None
    assert result.gmm1_sep > 1.5
    mat_rows = scene["mat_rows"]
    assert result.mat_mask[mat_rows].mean() > 0.95
    assert (result.classes[~mat_rows] == settings.CLASS_WATER).mean() > 0.95
    assert set(np.unique(result.classes)) <= {0, 1, MAT_PENDING}


def test_turbid_prescreen_widens_water() -> None:
    rng = np.random.default_rng(11)
    shape = (80, 50)
    water = _water(rng, 40 * shape[1], b08=(0.16, 0.015), nsd=(0.05, 0.02))
    interface = {
        "fai": rng.normal(0.03, 0.015, 15 * shape[1]),
        "ndvi": rng.normal(0.25, 0.1, 15 * shape[1]),
        "ndre": rng.normal(0.2, 0.1, 15 * shape[1]),
        "mndwi": rng.normal(0.0, 0.1, 15 * shape[1]),
        "bsi": rng.normal(-0.05, 0.03, 15 * shape[1]),
        "nsd": rng.normal(0.2, 0.05, 15 * shape[1]),
        "b08": rng.normal(0.20, 0.03, 15 * shape[1]),
    }
    mat = _mat(rng, 25 * shape[1])
    mat["b08"] = rng.normal(0.30, 0.03, 25 * shape[1])
    scene = {}
    for key in water:
        scene[key] = np.concatenate([water[key], interface[key], mat[key]]).reshape(
            shape
        )
    result = stage1(
        *[scene[k] for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert result.nir_thr_used is not None and result.nir_thr_used > 0.14
    assert result.no_mat is False
    assert result.mat_mask[55:, :].mean() > 0.9
    assert (result.classes[:40, :] == settings.CLASS_WATER).mean() > 0.9


def test_no_mat_all_water() -> None:
    rng = np.random.default_rng(13)
    shape = (100, 100)
    water = _water(rng, shape[0] * shape[1], b08=(0.09, 0.02), nsd=(0.05, 0.02))
    arrays = [
        water[k].reshape(shape)
        for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")
    ]
    result = stage1(*arrays, np.ones(shape, dtype=bool), np.zeros(shape, dtype=bool))
    assert result.no_mat is True
    assert result.gmm1_sep == math.inf
    assert result.mat_mask.sum() == 0
    assert (result.classes == settings.CLASS_WATER).all()


def test_soil_only_in_border() -> None:
    rng = np.random.default_rng(17)
    shape = (30, 40)
    water = _water(rng, shape[0] * shape[1], b08=(0.09, 0.02), nsd=(0.05, 0.02))
    arrays = {k: water[k].reshape(shape) for k in water}
    soil_spectra = {
        "bsi": 0.10,
        "mndwi": -0.20,
        "ndvi": 0.10,
        "ndre": 0.10,
        "nsd": 0.05,
        "b08": 0.15,
        "fai": 0.02,
    }
    for key, value in soil_spectra.items():
        arrays[key][0:3, 10:20] = value  # inside the border strip
        arrays[key][15:18, 10:20] = value  # interior, same spectra
    border = np.zeros(shape, dtype=bool)
    border[0:5, :] = True
    result = stage1(
        *[arrays[k] for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")],
        np.ones(shape, dtype=bool),
        border,
    )
    assert (result.classes[0:3, 10:20] == settings.CLASS_BARE_SOIL).all()
    assert (result.classes[15:18, 10:20] != settings.CLASS_BARE_SOIL).all()


def test_border_strip_geometry() -> None:
    aoi = np.zeros((70, 70), dtype=bool)
    aoi[15:55, 15:55] = True
    border = border_strip(aoi)
    assert border.sum() == 40 * 40 - 10 * 10
    assert not border[30:40, 30:40].any()
    assert border[15, 15] and border[15, 54]
    assert not border[0, 0]  # outside the AOI is never border
    narrow = np.zeros((60, 70), dtype=bool)
    narrow[20:40, 20:50] = True
    assert border_strip(narrow)[20:40, 20:50].all()


def test_valley_detector() -> None:
    rng = np.random.default_rng(0)
    uniform = rng.uniform(0.1, 0.5, 5000)
    assert nir_valley(uniform) == settings.NIR_WATER_MAX
    assert nir_valley(np.array([0.2, 0.3])) == settings.NIR_WATER_MAX
    bimodal = np.concatenate(
        [rng.normal(0.14, 0.01, 3000), rng.normal(0.30, 0.015, 3000)]
    )
    valley = nir_valley(bimodal)
    assert 0.17 < valley < 0.38


def test_nan_pixels_stay_nodata() -> None:
    rng = np.random.default_rng(7)
    shape, split = (80, 50), 50
    scene = _scene(
        _water(rng, split * shape[1]),
        _mat(rng, (shape[0] - split) * shape[1]),
        shape,
        split,
    )
    scene["b08"][0, 0] = np.nan
    scene["fai"][60, 0] = np.nan
    result = stage1(
        *[scene[k] for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert result.classes[0, 0] == settings.CLASS_NODATA
    assert result.classes[60, 0] == settings.CLASS_NODATA
    assert np.isnan(result.confidence[0, 0])
    assert result.no_mat is False


def _adaptive_scene():
    """Scene that reaches attempt 1 (low attempt-0 sep, clear NIR valley)."""
    rng = np.random.default_rng(23)
    shape = (110, 100)
    n_water, n_edge, n_mat = 6000, 1000, 4000
    water = _water(rng, n_water, b08=(0.11, 0.015), nsd=(0.10, 0.07))
    water["fai"] = rng.normal(0.005, 0.006, n_water)
    water["ndvi"] = rng.normal(0.10, 0.10, n_water)
    edge = {
        "fai": rng.normal(0.03, 0.015, n_edge),
        "ndvi": rng.normal(0.10, 0.10, n_edge),
        "ndre": rng.normal(0.2, 0.1, n_edge),
        "mndwi": rng.normal(0.0, 0.1, n_edge),
        "bsi": rng.normal(-0.05, 0.03, n_edge),
        "nsd": rng.normal(0.2, 0.05, n_edge),
        "b08": rng.normal(0.19, 0.02, n_edge),
    }
    mat = _mat(rng, n_mat)
    mat["fai"] = rng.normal(0.05, 0.04, n_mat)
    mat["ndvi"] = rng.normal(0.10, 0.10, n_mat)
    mat["b08"] = rng.normal(0.30, 0.04, n_mat)
    scene = {}
    for key in water:
        scene[key] = np.concatenate([water[key], edge[key], mat[key]]).reshape(shape)
    return scene, shape


def _run_stage1(scene, shape):
    return stage1(
        *[scene[k] for k in ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )


def test_adaptive_nir_second_attempt() -> None:
    scene, shape = _adaptive_scene()
    result = _run_stage1(scene, shape)
    assert result.nir_thr_used is not None
    assert result.no_mat is False
    assert result.mat_mask.sum() > 0


def test_is_no_mat() -> None:
    assert _is_no_mat(np.zeros(100), 0.05) is True  # frac 0 < 0.02
    assert _is_no_mat(np.full(100, 0.06), 0.06) is False
    assert _is_no_mat(np.full(100, 0.06), 0.004) is True  # mat comp dark
    boundary_frac = np.concatenate([np.full(2, 0.06), np.zeros(98)])
    assert _is_no_mat(boundary_frac, 0.06) is False  # frac exactly 0.02
    assert _is_no_mat(np.full(100, 0.06), 0.005) is False  # mean exactly 0.005


def test_attempt1_check_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    scene, shape = _adaptive_scene()
    calls = []

    def fire_on_attempt1(fai_values, mat_mean_fai):
        calls.append(True)
        return len(calls) == 2

    monkeypatch.setattr(gmm_module, "_is_no_mat", fire_on_attempt1)
    result = _run_stage1(scene, shape)
    assert len(calls) == 2  # the check ran on both attempts
    assert result.nir_thr_used is not None  # adaptive really fired
    assert result.no_mat is True
    assert result.gmm1_sep == math.inf
    assert result.mat_mask.sum() == 0


def test_no_mat_check_fires_attempt0() -> None:
    rng = np.random.default_rng(41)
    shape = (100, 100)
    dark = _water(rng, 3000, b08=(0.09, 0.02), nsd=(0.2, 0.05))
    bright = _water(rng, 7000, b08=(0.14, 0.02), nsd=(0.05, 0.02))
    scene = {}
    for key in dark:
        scene[key] = np.concatenate([dark[key], bright[key]]).reshape(shape)
    result = _run_stage1(scene, shape)
    assert result.nir_thr_used is None
    assert result.no_mat is True
    assert result.gmm1_sep == math.inf
    assert result.mat_mask.sum() == 0


def test_fai_rescue_after_prescreen_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FAI rescue fires when only the pre-screen widened the threshold.

    The GMM is mocked to call everything water, so every MAT_PENDING pixel
    must come from the rescue; the valley detector is counted to prove the
    adaptive attempt never ran.
    """
    rng = np.random.default_rng(11)
    shape = (80, 50)
    water = _water(rng, 40 * shape[1], b08=(0.16, 0.015), nsd=(0.05, 0.02))
    edge = {
        "fai": rng.normal(0.07, 0.005, 10 * shape[1]),
        "ndvi": rng.normal(0.25, 0.05, 10 * shape[1]),
        "ndre": rng.normal(0.2, 0.05, 10 * shape[1]),
        "mndwi": rng.normal(0.0, 0.1, 10 * shape[1]),
        "bsi": rng.normal(-0.05, 0.03, 10 * shape[1]),
        "nsd": rng.normal(0.20, 0.02, 10 * shape[1]),
        "b08": rng.normal(0.165, 0.005, 10 * shape[1]),
    }
    mat = _mat(rng, 30 * shape[1])
    mat["b08"] = rng.normal(0.30, 0.03, 30 * shape[1])
    scene = {}
    for key in water:
        scene[key] = np.concatenate([water[key], edge[key], mat[key]]).reshape(shape)

    def _all_water(features):
        n = len(features)
        means = np.array([[0.0, 0.0], [0.06, 0.10]])
        covs = np.array([np.eye(2) * 0.01, np.eye(2) * 0.01])
        return np.ones(n), 0, means, covs

    monkeypatch.setattr(gmm_module, "_fit_water_posterior", _all_water)
    real_valley = gmm_module.nir_valley
    calls = []

    def _counted_valley(b08):
        calls.append(True)
        return real_valley(b08)

    monkeypatch.setattr(gmm_module, "nir_valley", _counted_valley)
    result = _run_stage1(scene, shape)

    assert result.nir_thr_used is not None
    assert result.nir_thr_used > settings.NIR_WATER_MAX
    assert len(calls) == 1  # pre-screen only; the adaptive attempt never ran
    assert result.no_mat is False
    assert result.n_fai_rescued > 0
    assert (result.classes[40:50, :] == MAT_PENDING).all()  # the edge rows
    eligible = (
        (scene["b08"] >= settings.NIR_WATER_MAX)
        & (scene["b08"] < result.nir_thr_used)
        & (scene["fai"] > settings.FAI_FLOAT_MIN)
    )
    assert ((result.classes == MAT_PENDING) <= eligible).all()
    assert result.n_fai_rescued == int(result.mat_mask.sum())


def test_no_mat_under_20_after_adaptive() -> None:
    rng = np.random.default_rng(31)
    n_water, n_high = 600, 18
    water = _water(rng, n_water, b08=(0.11, 0.015), nsd=(0.10, 0.07))
    water["fai"] = rng.normal(0.002, 0.003, n_water)
    water["ndvi"] = rng.normal(0.1, 0.1, n_water)
    high = {
        "fai": rng.normal(0.06, 0.01, n_high),
        "ndvi": rng.normal(0.1, 0.1, n_high),
        "ndre": rng.normal(0.4, 0.1, n_high),
        "mndwi": rng.normal(-0.3, 0.1, n_high),
        "bsi": rng.normal(-0.1, 0.05, n_high),
        "nsd": rng.normal(0.2, 0.05, n_high),
        "b08": rng.normal(0.25, 0.02, n_high),
    }
    shape = (6, 103)
    scene = {}
    for key in water:
        scene[key] = np.concatenate([water[key], high[key]]).reshape(shape)
    result = _run_stage1(scene, shape)
    assert result.nir_thr_used is not None  # adaptive fired, then < 20 left
    assert result.no_mat is True
    assert result.gmm1_sep == math.inf
    assert result.mat_mask.sum() == 0
