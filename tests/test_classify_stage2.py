"""Tests for Stage 2, the rescue, the substrate test, and the pipeline."""

import math
from datetime import datetime

import numpy as np
import pytest
import rasterio

from smol_frontier.aoi import reservoir_area_ha
from smol_frontier.classify.gmm import (
    MAT_PENDING,
    rescue_water,
    stage2,
    substrate_test,
)
from smol_frontier.classify.pipeline import classify
from smol_frontier.config import settings
from smol_frontier.ingest import IngestedScene
from smol_frontier.sources.stac import SceneMeta


def _pending_scene(rng, shape=(80, 50)):
    """Mat scene: dense-like top half, interface-like bottom half (all pending)."""
    split = shape[0] // 2
    n_top, n_bot = split * shape[1], (shape[0] - split) * shape[1]
    ndvi = np.concatenate(
        [rng.normal(0.72, 0.05, n_top), rng.normal(0.38, 0.05, n_bot)]
    ).reshape(shape)
    ndre = np.concatenate(
        [rng.normal(0.55, 0.05, n_top), rng.normal(0.22, 0.05, n_bot)]
    ).reshape(shape)
    classes = np.full(shape, MAT_PENDING, dtype=np.int16)
    confidence = np.full(shape, np.nan, dtype=np.float32)
    return ndvi, ndre, classes, confidence, split


def test_stage2_clean_dense_interface_split() -> None:
    rng = np.random.default_rng(11)
    ndvi, ndre, classes, confidence, split = _pending_scene(rng)
    classes[0, 0] = settings.CLASS_WATER
    confidence[0, 0] = 0.9
    result = stage2(ndvi, ndre, classes, confidence)
    assert (result.classes != MAT_PENDING).all()
    assert math.isfinite(result.gmm2_sep) and result.gmm2_sep > 0
    dense = result.classes[:split, :] == settings.CLASS_DENSE_HYACINTH
    assert dense.mean() > 0.95
    interface = result.classes[split:, :] == settings.CLASS_INTERFACE
    assert interface.mean() > 0.95
    # Non-mat pixels are untouched.
    assert result.classes[0, 0] == settings.CLASS_WATER
    assert result.confidence[0, 0] == pytest.approx(0.9)
    mat_conf = result.confidence[1:, :]
    assert np.isfinite(mat_conf).all()
    assert ((mat_conf >= 0.0) & (mat_conf <= 1.0)).all()


def test_stage2_few_mat_pixels_become_interface() -> None:
    shape = (4, 4)
    ndvi = np.full(shape, 0.7)
    ndre = np.full(shape, 0.5)
    classes = np.full(shape, settings.CLASS_WATER, dtype=np.int16)
    classes[0, 0:3] = MAT_PENDING
    confidence = np.full(shape, 1.0, dtype=np.float32)
    result = stage2(ndvi, ndre, classes, confidence)
    assert (result.classes != MAT_PENDING).all()
    assert (result.classes[0, 0:3] == settings.CLASS_INTERFACE).all()
    assert math.isnan(result.gmm2_sep)
    assert (result.confidence[0, 0:3] == 1.0).all()


def test_stage2_no_mat_skips() -> None:
    shape = (4, 4)
    classes = np.full(shape, settings.CLASS_WATER, dtype=np.int16)
    confidence = np.full(shape, 1.0, dtype=np.float32)
    result = stage2(np.full(shape, 0.7), np.full(shape, 0.5), classes, confidence)
    assert (result.classes == settings.CLASS_WATER).all()
    assert math.isnan(result.gmm2_sep)


def test_stage2_nonfinite_mat_resolves_interface() -> None:
    rng = np.random.default_rng(11)
    ndvi, ndre, classes, confidence, _ = _pending_scene(rng)
    ndre[0, 1:4] = np.nan
    result = stage2(ndvi, ndre, classes, confidence)
    assert (result.classes != MAT_PENDING).all()
    assert (result.classes[0, 1:4] == settings.CLASS_INTERFACE).all()
    assert math.isfinite(result.gmm2_sep)


def test_rescue_water_fires() -> None:
    w, s, i = (
        settings.CLASS_WATER,
        settings.CLASS_BARE_SOIL,
        settings.CLASS_INTERFACE,
    )
    classes = np.array([[w, w, w, w, s, i]], dtype=np.int16)
    confidence = np.full((1, 6), 0.9, dtype=np.float32)
    valid = np.array([[True, True, True, False, True, True]])
    ndvi = np.array([[0.40, 0.40, 0.20, 0.40, 0.50, 0.50]])
    b08 = np.array([[0.20, 0.10, 0.20, 0.20, 0.20, 0.20]])
    result = rescue_water(classes, confidence, valid, ndvi, b08)
    assert result.n_changed == 1
    assert result.classes[0, 0] == i
    assert result.confidence[0, 0] == pytest.approx(1.0)
    # Low NIR, low NDVI, invalid, soil, and interface pixels are untouched.
    assert result.classes[0, 1] == w
    assert result.classes[0, 2] == w
    assert result.classes[0, 3] == w
    assert result.classes[0, 4] == s
    assert result.classes[0, 5] == i


def test_substrate_fires_near_anchor() -> None:
    shape = (20, 20)
    classes = np.full(shape, settings.CLASS_WATER, dtype=np.int16)
    confidence = np.full(shape, 1.0, dtype=np.float32)
    valid = np.full(shape, True)
    nsd = np.full(shape, 0.20)
    ndvi = np.full(shape, 0.05)
    classes[10, 2] = settings.CLASS_BARE_SOIL
    ndvi[10, 2] = 0.10
    classes[10, 5] = settings.CLASS_DENSE_HYACINTH  # 3 px away: hit
    nsd[10, 5] = 0.05
    ndvi[10, 5] = 0.60
    classes[10, 12] = settings.CLASS_INTERFACE  # 10 px away: too far
    nsd[10, 12] = 0.05
    ndvi[10, 12] = 0.40
    classes[10, 4] = settings.CLASS_DENSE_HYACINTH  # near but high NSD
    nsd[10, 4] = 0.20
    ndvi[10, 4] = 0.60
    result = substrate_test(classes, confidence, valid, nsd, ndvi)
    assert result.n_changed == 1
    assert result.classes[10, 5] == settings.CLASS_BARE_SOIL
    assert result.confidence[10, 5] == pytest.approx(1.0)
    assert result.classes[10, 12] == settings.CLASS_INTERFACE
    assert result.classes[10, 4] == settings.CLASS_DENSE_HYACINTH


def test_substrate_no_anchor_changes_nothing() -> None:
    shape = (20, 20)
    classes = np.full(shape, settings.CLASS_DENSE_HYACINTH, dtype=np.int16)
    confidence = np.full(shape, 0.9, dtype=np.float32)
    valid = np.full(shape, True)
    nsd = np.full(shape, 0.05)
    ndvi = np.full(shape, 0.60)
    result = substrate_test(classes, confidence, valid, nsd, ndvi)
    assert result.n_changed == 0
    assert (result.classes == settings.CLASS_DENSE_HYACINTH).all()


def test_substrate_vegetated_soil_is_no_anchor() -> None:
    shape = (20, 20)
    classes = np.full(shape, settings.CLASS_WATER, dtype=np.int16)
    confidence = np.full(shape, 1.0, dtype=np.float32)
    valid = np.full(shape, True)
    nsd = np.full(shape, 0.20)
    ndvi = np.full(shape, 0.05)
    classes[10, 5] = settings.CLASS_DENSE_HYACINTH
    nsd[10, 5] = 0.05
    ndvi[10, 5] = 0.60
    classes[10, 6] = settings.CLASS_BARE_SOIL  # adjacent but vegetated
    ndvi[10, 6] = 0.50
    result = substrate_test(classes, confidence, valid, nsd, ndvi)
    assert result.n_changed == 0
    assert result.classes[10, 5] == settings.CLASS_DENSE_HYACINTH


def _water_scene(shape=(20, 20)) -> IngestedScene:
    """All-open-water synthetic scene (constant DN, offset 0)."""
    dn = {
        "B02": 300,
        "B03": 450,
        "B04": 500,
        "B05": 350,
        "B08": 350,
        "B8A": 340,
        "B11": 200,
    }
    bands = {band: np.full(shape, value, dtype=np.uint16) for band, value in dn.items()}
    bands["SCL"] = np.full(shape, 6, dtype=np.uint16)
    scene = SceneMeta(
        id="TEST_WATER",
        datetime=datetime(2026, 10, 12),
        source="test",
        processing_baseline="05.11",
        boa_offset_applied=True,
        assets={},
    )
    return IngestedScene(
        scene=scene,
        bands=bands,
        inside=np.full(shape, True),
        transform=rasterio.Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2200000.0),
        crs="EPSG:32614",
        offset=0.0,
    )


def test_pipeline_all_water() -> None:
    result = classify(_water_scene())
    assert result.scene_id == "TEST_WATER"
    assert result.class_map.dtype == np.uint8
    assert (result.class_map == settings.CLASS_WATER).all()
    assert result.ha_per_class[settings.CLASS_WATER] == pytest.approx(4.0)
    assert result.hyacinth_total_ha == pytest.approx(0.0)
    assert result.pct_reservoir == pytest.approx(0.0)
    assert result.reservoir_area_ha == pytest.approx(reservoir_area_ha())
    assert result.valid_fraction == pytest.approx(1.0)
    assert result.cloud_fraction == pytest.approx(0.0)
    assert math.isinf(result.gmm1_sep)
    assert math.isnan(result.gmm2_sep)
    assert result.nir_thr_used is None
    assert result.nir_water_frac == pytest.approx(1.0)
    assert result.n_rescued == 0
    assert result.n_substrate == 0
    # Identities: total = dense + interface; percent uses the AOI area.
    assert result.hyacinth_total_ha == pytest.approx(
        result.ha_per_class[settings.CLASS_DENSE_HYACINTH]
        + result.ha_per_class[settings.CLASS_INTERFACE]
    )
    assert result.pct_reservoir == pytest.approx(
        result.hyacinth_total_ha / reservoir_area_ha() * 100.0
    )
