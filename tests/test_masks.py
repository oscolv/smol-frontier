"""Tests for validity masks (synthetic archetype pixels, pure numpy)."""

import numpy as np
import pytest

from smol_frontier.classify import indices
from smol_frontier.classify.masks import (
    SPECTRAL_BANDS,
    cloud_fraction,
    haze_vegetation,
    scl_invalid,
    spectral_cloud,
    valid_fraction,
    valid_mask,
)
from smol_frontier.config import settings

CLEAN_WATER = {
    "b02": 0.03,
    "b03": 0.03,
    "b04": 0.03,
    "b05": 0.02,
    "b08": 0.02,
    "b8a": 0.02,
    "b11": 0.01,
    "scl": 6,
}
TURBID_WATER = {  # B11 guard saves it
    "b02": 0.08,
    "b03": 0.085,
    "b04": 0.09,
    "b05": 0.06,
    "b08": 0.10,
    "b8a": 0.09,
    "b11": 0.02,
    "scl": 6,
}
DENSE_HYACINTH = {  # B02 guard saves it
    "b02": 0.02,
    "b03": 0.04,
    "b04": 0.03,
    "b05": 0.15,
    "b08": 0.35,
    "b8a": 0.34,
    "b11": 0.15,
    "scl": 4,
}
HAZE_AS_WATER = {  # SCL missed it
    "b02": 0.075,
    "b03": 0.075,
    "b04": 0.075,
    "b05": 0.08,
    "b08": 0.08,
    "b8a": 0.08,
    "b11": 0.12,
    "scl": 6,
}
CLOUD = {
    "b02": 0.15,
    "b03": 0.15,
    "b04": 0.15,
    "b05": 0.16,
    "b08": 0.16,
    "b8a": 0.16,
    "b11": 0.20,
    "scl": 9,
}
SEDIMENT = {  # whiteness guard saves it
    "b02": 0.09,
    "b03": 0.10,
    "b04": 0.12,
    "b05": 0.14,
    "b08": 0.16,
    "b8a": 0.16,
    "b11": 0.15,
    "scl": 5,
}
DARK_WATER_NEGATIVE = {  # Sen2Cor over-correction: real water, not nodata
    "b02": 0.03,
    "b03": 0.025,
    "b04": 0.02,
    "b05": 0.01,
    "b08": -0.004,
    "b8a": -0.017,
    "b11": 0.002,
    "scl": 6,
}


def _pixel(archetype):
    return {
        "scl": np.array([archetype["scl"]]),
        "bands": {band: np.array([archetype[band.lower()]]) for band in SPECTRAL_BANDS},
        "inside": np.array([True]),
    }


@pytest.mark.parametrize(
    ("archetype", "valid", "cloudy"),
    [
        (CLEAN_WATER, True, False),
        (TURBID_WATER, True, False),
        (DENSE_HYACINTH, True, False),
        (HAZE_AS_WATER, False, True),
        (CLOUD, False, True),
        (SEDIMENT, True, False),
        (DARK_WATER_NEGATIVE, True, False),
    ],
)
def test_archetypes_land_where_expected(archetype, valid, cloudy) -> None:
    px = _pixel(archetype)
    assert bool(valid_mask(**px)) is valid
    assert (
        bool(spectral_cloud(px["bands"]["B02"], px["bands"]["B11"], px["bands"]["B04"]))
        is cloudy
    )


@pytest.mark.parametrize("scl", [0, 1, 3, 8, 9, 10])
def test_scl_invalid_classes(scl: int) -> None:
    px = _pixel(CLEAN_WATER) | {"scl": np.array([scl])}
    assert bool(scl_invalid(px["scl"])) is True
    assert bool(valid_mask(**px)) is False


@pytest.mark.parametrize("scl", [2, 4, 5, 6, 7, 11])
def test_scl_accepted_classes(scl: int) -> None:
    px = _pixel(CLEAN_WATER) | {"scl": np.array([scl])}
    assert bool(scl_invalid(px["scl"])) is False
    assert bool(valid_mask(**px)) is True


def test_outside_is_never_valid() -> None:
    px = _pixel(CLEAN_WATER) | {"inside": np.array([False])}
    assert bool(valid_mask(**px)) is False


def test_shadow_counts_as_cloud() -> None:
    """Shadow alone (spectrally clear) pushes the cloud fraction over cutoff."""
    n = 100
    px = {
        "scl": np.array([3] * 12 + [6] * 88),
        "bands": {band: np.full(n, 0.03) for band in SPECTRAL_BANDS},
        "inside": np.ones(n, dtype=bool),
    }
    px["bands"]["B11"] = np.full(n, 0.01)
    flat = {
        "scl": px["scl"],
        "b02": px["bands"]["B02"],
        "b11": px["bands"]["B11"],
        "b04": px["bands"]["B04"],
        "inside": px["inside"],
    }
    assert cloud_fraction(**flat) == pytest.approx(0.12)
    assert cloud_fraction(**flat) > settings.CLOUD_FRAC_DOUBTFUL
    assert valid_fraction(**px) == pytest.approx(0.88)  # shadow still invalid


def test_fractions() -> None:
    archetypes = [CLEAN_WATER, HAZE_AS_WATER, CLOUD, SEDIMENT]
    px = {
        "scl": np.array([a["scl"] for a in archetypes]),
        "bands": {
            band: np.array([a[band.lower()] for a in archetypes])
            for band in SPECTRAL_BANDS
        },
        "inside": np.array([True, True, True, True]),
    }
    assert valid_fraction(**px) == 0.5  # water + sediment
    flat = {
        "scl": px["scl"],
        "b02": px["bands"]["B02"],
        "b11": px["bands"]["B11"],
        "b04": px["bands"]["B04"],
        "inside": px["inside"],
    }
    assert cloud_fraction(**flat) == 0.5  # haze (spectral) + SCL 9
    empty = dict(px, inside=np.zeros(4, dtype=bool))
    assert valid_fraction(**empty) == 0.0
    assert cloud_fraction(**{**flat, "inside": np.zeros(4, dtype=bool)}) == 0.0


def test_dark_water_slightly_negative_is_valid() -> None:
    """B08=-0.004, B8A=-0.017, B11=0.002, SCL=6: water, not nodata."""
    assert bool(valid_mask(**_pixel(DARK_WATER_NEGATIVE))) is True


@pytest.mark.parametrize("band", SPECTRAL_BANDS)
def test_subfloor_band_invalidates(band: str) -> None:
    """A row with any band at -0.1 (nodata-with-offset level) is invalid."""
    px = _pixel(CLEAN_WATER)
    px["bands"][band] = np.array([-0.1])
    assert bool(valid_mask(**px)) is False


def test_floor_boundary_is_invalid() -> None:
    """The floor is strict: bands must be *above* REFLECTANCE_MIN_VALID."""
    px = _pixel(CLEAN_WATER)
    px["bands"]["B08"] = np.array([settings.REFLECTANCE_MIN_VALID])
    assert bool(valid_mask(**px)) is False
    px["bands"]["B08"] = np.array([settings.REFLECTANCE_MIN_VALID + 1e-9])
    assert bool(valid_mask(**px)) is True


@pytest.mark.parametrize("band", SPECTRAL_BANDS)
def test_nan_in_any_band_invalidates(band: str) -> None:
    """NaN (DN == 0) in any band inside the reservoir is invalid."""
    px = _pixel(CLEAN_WATER)
    px["bands"][band] = np.array([np.nan])
    assert bool(valid_mask(**px)) is False


def test_flat_and_dict_styles_agree() -> None:
    """Flat kwargs, dict-first positional, and legacy 3-band calls agree."""
    px = _pixel(DARK_WATER_NEGATIVE)
    by_dict_kw = valid_mask(**px)
    by_dict_pos = valid_mask(px["scl"], px["bands"], px["inside"])
    flat = {key.lower(): value for key, value in px["bands"].items()}
    by_flat = valid_mask(px["scl"], inside=px["inside"], **flat)
    assert bool(by_dict_kw[0]) is True
    assert (by_dict_pos == by_dict_kw).all()
    assert (by_flat == by_dict_kw).all()
    legacy = valid_mask(px["scl"], flat["b02"], flat["b11"], flat["b04"], px["inside"])
    assert bool(legacy[0]) is True
    assert valid_fraction(px["scl"], px["bands"], px["inside"]) == pytest.approx(1.0)


def test_ndvi_from_clipped_bands_stays_bounded() -> None:
    """Clipped NDVI with B08=-0.005, B04=0.02 stays in [-1, 1]."""
    b08 = np.array([-0.005])
    b04 = np.array([0.02])
    unclipped = indices.ndvi(b08, b04)
    assert float(unclipped[0]) < -1.0  # the case is meaningful unclipped
    clipped = indices.ndvi(np.maximum(b08, 0), np.maximum(b04, 0))
    assert -1.0 <= float(clipped[0]) <= 1.0
    assert float(b08[0]) < 0.0  # the clip is a copy; the original is untouched


HAZE_BANDS = {
    "B02": 0.075,
    "B03": 0.08,
    "B04": 0.07,
    "B05": 0.09,
    "B08": 0.40,
    "B8A": 0.38,
    "B11": 0.15,
}


def _haze(scl: int = 4, inside: bool = True, **overrides: float):
    bands = {k: np.array([v]) for k, v in HAZE_BANDS.items()}
    for key, value in overrides.items():
        bands[key] = np.array([value])
    return haze_vegetation(np.array([scl]), bands, np.array([inside]))


def test_haze_over_hyacinth_is_rescued() -> None:
    assert bool(_haze()[0]) is True


def test_moderate_ndvi_cloud_is_not_rescued() -> None:
    # 2025-07-29-like cloud (NDVI ~ 0.375)
    assert bool(_haze(B08=0.22, B04=0.10, B02=0.12)[0]) is False


def test_ndvi_exactly_at_threshold_is_not_rescued() -> None:
    # (0.75 - 0.25) / (0.75 + 0.25) == 0.5 exactly in binary floating point
    assert settings.CLOUD_NDVI_MAX == 0.5
    assert bool(_haze(B08=0.75, B04=0.25, B02=0.25)[0]) is False


@pytest.mark.parametrize("scl", [0, 1, 3, 8, 9, 10])
def test_haze_invalid_scl_is_not_rescued(scl: int) -> None:
    assert bool(_haze(scl=scl)[0]) is False


def test_clear_vegetation_is_not_rescued() -> None:
    assert bool(_haze(B02=0.03)[0]) is False


@pytest.mark.parametrize("band", SPECTRAL_BANDS)
def test_haze_nan_or_subfloor_band_is_not_rescued(band: str) -> None:
    assert bool(_haze(**{band: np.nan})[0]) is False
    assert bool(_haze(**{band: settings.REFLECTANCE_MIN_VALID})[0]) is False


def test_haze_outside_aoi_is_not_rescued() -> None:
    assert bool(_haze(inside=False)[0]) is False


def test_haze_is_disjoint_from_valid_mask() -> None:
    cols = [HAZE_BANDS, {**HAZE_BANDS, "B02": 0.03}, {**HAZE_BANDS, "B08": 0.15}]
    bands = {k: np.array([c[k] for c in cols]) for k in HAZE_BANDS}
    scl = np.array([4, 4, 4])
    inside = np.ones(3, dtype=bool)
    haze = haze_vegetation(scl, bands, inside)
    valid = valid_mask(scl, bands, inside)
    assert haze.tolist() == [True, False, False]
    assert not (haze & valid).any()
