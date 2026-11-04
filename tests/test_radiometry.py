"""Tests for the DN-to-reflectance funnel (pure, no I/O)."""

import logging

import numpy as np
import pytest

from smol_frontier.sources.radiometry import (
    AnomalousProduct,
    boa_offset,
    check_radiometry,
    dn_to_reflectance,
    guard_boa_offset,
    to_reflectance,
)


def test_offset_baseline_below_04_never_applies() -> None:
    assert boa_offset("02.04", False) == 0.0
    assert boa_offset("03.01", False) == 0.0


def test_offset_baseline_at_or_above_04_with_flag_false() -> None:
    assert boa_offset("04.00", False) == pytest.approx(-0.1)
    assert boa_offset("05.11", False) == pytest.approx(-0.1)


def test_offset_flag_true_never_applies() -> None:
    assert boa_offset("05.11", True) == 0.0
    assert boa_offset("03.01", True) == 0.0


def test_offset_unknown_baseline_is_conservative() -> None:
    assert boa_offset(None, False) == 0.0
    assert boa_offset("not-a-baseline", False) == 0.0


def test_guard_keeps_offset_for_uncorrected_dn() -> None:
    b2 = np.array([1500, 2000, 3000, 1200])  # stays positive under -0.1
    assert guard_boa_offset(-0.1, b2) == pytest.approx(-0.1)


def test_guard_distrusts_bad_flag(caplog: pytest.LogCaptureFixture) -> None:
    b2 = np.array([500, 300, 800, 200])  # already corrected: -0.1 negatives all
    with caplog.at_level(logging.WARNING, logger="smol_frontier.sources.radiometry"):
        assert guard_boa_offset(-0.1, b2) == 0.0
    assert "boa_offset_applied looks wrong" in caplog.text


def test_guard_boundary_is_more_than_half() -> None:
    exactly_half = np.array([500, 500, 1500, 1500])  # 2/4 negative
    assert guard_boa_offset(-0.1, exactly_half) == pytest.approx(-0.1)
    over_half = np.array([500, 500, 500, 1500])  # 3/4 negative
    assert guard_boa_offset(-0.1, over_half) == 0.0


def test_guard_ignores_nodata_and_zero_offset() -> None:
    assert guard_boa_offset(0.0, np.array([100, 200])) == 0.0
    assert guard_boa_offset(-0.1, np.zeros(4, dtype=int)) == pytest.approx(-0.1)


def test_dn_to_reflectance_values() -> None:
    dn = np.array([1500, 2000])
    assert dn_to_reflectance(dn, -0.1) == pytest.approx([0.05, 0.10])
    assert dn_to_reflectance(dn, 0.0) == pytest.approx([0.15, 0.20])


def test_nodata_stays_nodata() -> None:
    dn = np.array([0, 1500, 0])
    result = dn_to_reflectance(dn, -0.1)
    assert np.isnan(result[0])
    assert result[1] == pytest.approx(0.05)
    assert np.isnan(result[2])


def test_funnel_end_to_end() -> None:
    dn = np.array([0, 1500])
    result = to_reflectance(dn, "05.11", False)
    assert np.isnan(result[0])
    assert result[1] == pytest.approx(0.05)
    already_ok = to_reflectance(dn, "05.11", True)
    assert already_ok[1] == pytest.approx(0.15)
    # Other bands are guarded by the B2 array, not their own DN.
    other = to_reflectance(
        np.array([1500]), "05.11", False, b2_dn=np.array([100, 100, 100])
    )
    assert other[0] == pytest.approx(0.15)


SPECTRAL = ("B02", "B03", "B04", "B05", "B08", "B8A", "B11")


def _dn_bands(rng, low: int, high: int, shape=(40, 50)):
    return {
        band: rng.integers(low, high, size=shape).astype(np.uint16) for band in SPECTRAL
    }


def test_check_radiometry_synthetic_8bit_raises() -> None:
    rng = np.random.default_rng(27)
    bands = _dn_bands(rng, 1, 256)
    bands["B08"][0, 0] = 5000  # outside the reservoir: must not save the scene
    inside = np.full((40, 50), True)
    inside[0, 0] = False
    bands["B02"][:, 0] = 0  # nodata column inside: excluded via DN > 0
    with pytest.raises(AnomalousProduct, match="S2C_TEST_8BIT") as excinfo:
        check_radiometry(bands, inside, "S2C_TEST_8BIT", "planetary-computer")
    message = str(excinfo.value)
    assert "planetary-computer" in message
    for band in SPECTRAL:
        assert f"{band}=" in message
    assert "earth-search" in message  # retry with the other STAC source


def test_check_radiometry_normal_product_passes() -> None:
    rng = np.random.default_rng(11)
    bands = _dn_bands(rng, 800, 4000)
    p999 = check_radiometry(
        bands, np.full((40, 50), True), "S2A_NORMAL", "earth-search"
    )
    assert sorted(p999) == sorted(SPECTRAL)
    assert all(value >= 800 for value in p999.values())


def test_bad_flag_case_reaches_guard() -> None:
    """Normal DN with a wrong boa_offset_applied flag: no raise, guard runs."""
    rng = np.random.default_rng(5)
    bands = _dn_bands(rng, 200, 800)  # already corrected: -0.1 negatives most
    check_radiometry(bands, np.full((40, 50), True), "S2X_BADFLAG", "earth-search")
    assert guard_boa_offset(-0.1, bands["B02"]) == 0.0
