"""Tests for spectral indices (hand-computed values, pure numpy)."""

import numpy as np

from smol_frontier.classify.indices import bsi, fai, mndwi, ndre, ndvi, nsd


def test_ndvi() -> None:
    assert ndvi(0.4, 0.1) == float(np.float32(0.6))
    assert ndvi(0.05, 0.15) == float(np.float32(-0.5))  # water-like, negative
    assert np.isnan(ndvi(0.0, 0.0))
    assert np.isnan(ndvi(0.2, -0.2))  # zero denominator, never inf
    assert ndvi(np.array([0.4]), np.array([0.1])).dtype == np.float32


def test_mndwi() -> None:
    assert mndwi(0.06, 0.02) == float(np.float32(0.5))
    assert np.isnan(mndwi(0.0, 0.0))
    assert mndwi(np.array([0.06]), np.array([0.02])).dtype == np.float32


def test_ndre() -> None:
    assert ndre(0.5, 0.3) == float(np.float32(0.25))
    assert np.isnan(ndre(0.0, 0.0))
    assert ndre(np.array([0.5]), np.array([0.3])).dtype == np.float32


def test_bsi() -> None:
    assert bsi(0.3, 0.2, 0.2, 0.1) == float(np.float32(0.25))
    assert bsi(0.2, 0.15, 0.25, 0.1) == float(np.float32(0.0))
    assert np.isnan(bsi(0.0, 0.0, 0.0, 0.0))
    out = bsi(np.array([0.3]), np.array([0.2]), np.array([0.2]), np.array([0.1]))
    assert out.dtype == np.float32


def test_nsd() -> None:
    assert nsd(0.4, 0.1) == float(np.float32(0.3))
    assert nsd(np.array([0.4]), np.array([0.1])).dtype == np.float32


def test_fai_b08_default() -> None:
    expected = 0.30 - (0.05 + (0.03 - 0.05) * (168 / 945))
    assert fai(0.30, 0.05, 0.03) == float(np.float32(expected))
    out = fai(np.array([0.30]), np.array([0.05]), np.array([0.03]))
    assert out.dtype == np.float32


def test_fai_b8a_option() -> None:
    expected = 0.32 - (0.05 + (0.03 - 0.05) * (200 / 945))
    assert fai(0.32, 0.05, 0.03, nir_wavelength_nm=865.0) == float(np.float32(expected))


def test_nan_propagates() -> None:
    assert np.isnan(ndvi(np.nan, 0.1))
    assert np.isnan(mndwi(0.06, np.nan))
    assert np.isnan(nsd(np.nan, np.nan))
    assert np.isnan(fai(0.30, np.nan, 0.03))
