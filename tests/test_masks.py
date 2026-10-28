"""Tests for validity masks (synthetic archetype pixels, pure numpy)."""

import numpy as np
import pytest

from smol_frontier.classify.masks import (
    cloud_fraction,
    scl_invalid,
    spectral_cloud,
    valid_fraction,
    valid_mask,
)

CLEAN_WATER = {"b02": 0.03, "b04": 0.03, "b11": 0.01, "scl": 6}
TURBID_WATER = {"b02": 0.08, "b04": 0.09, "b11": 0.02, "scl": 6}  # B11 guard saves it
DENSE_HYACINTH = {"b02": 0.02, "b04": 0.03, "b11": 0.15, "scl": 4}  # B02 guard saves it
HAZE_AS_WATER = {"b02": 0.075, "b04": 0.075, "b11": 0.12, "scl": 6}  # SCL missed it
CLOUD = {"b02": 0.15, "b04": 0.15, "b11": 0.20, "scl": 9}
SEDIMENT = {"b02": 0.09, "b04": 0.12, "b11": 0.15, "scl": 5}  # whiteness guard saves it


def _pixel(archetype):
    one = np.array([archetype["b02"]]), np.array([archetype["b11"]])
    return {
        "scl": np.array([archetype["scl"]]),
        "b02": one[0],
        "b11": one[1],
        "b04": np.array([archetype["b04"]]),
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
    ],
)
def test_archetypes_land_where_expected(archetype, valid, cloudy) -> None:
    px = _pixel(archetype)
    assert bool(valid_mask(**px)) is valid
    assert bool(spectral_cloud(px["b02"], px["b11"], px["b04"])) is cloudy


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


def test_fractions() -> None:
    archetypes = [CLEAN_WATER, HAZE_AS_WATER, CLOUD, SEDIMENT]
    px = {
        key: np.array([a[key] for a in archetypes])
        for key in ("scl", "b02", "b11", "b04")
    } | {"inside": np.array([True, True, True, True])}
    assert valid_fraction(**px) == 0.5  # water + sediment
    assert cloud_fraction(**px) == 0.5  # haze (spectral) + SCL 9
    empty = dict(px, inside=np.zeros(4, dtype=bool))
    assert valid_fraction(**empty) == 0.0
    assert cloud_fraction(**empty) == 0.0
