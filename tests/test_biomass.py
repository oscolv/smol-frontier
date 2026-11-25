"""Wet biomass from corrected hectares, cross-checked against smol and smol-gee.

The reference tonnes come from the two implementations the viewer figure
replaces, which use the same density (issue #102, maintainer comment):

- smol, sentinel/pipeline/runner.py:
  ``round(ha * 10_000 * BIOMASA_KG_POR_M2 / 1_000, 1)`` (central only);
- smol-gee, endho.js / clasificar_fecha.py: ``round(ha * 10 * k)`` for the
  central, low and high density.
"""

import pytest

from smol_frontier.biomass import WetBiomass, wet_biomass
from smol_frontier.config import settings

# (hectares, smol central t, smol-gee (central, low, high) t)
REFERENCE = [
    (824.63, 151072.2, (151072, 96234, 196592)),  # 2026-10-12, corrected
    (15.70, 2876.2, (2876, 1832, 3743)),  # 2024-06-09, corrected
    (434.21, 79547.3, (79547, 50672, 103516)),  # 2024-06-09, raw
]


def test_density_is_the_field_measurement() -> None:
    assert (
        settings.BIOMASS_WET_KG_M2,
        settings.BIOMASS_WET_KG_M2_LOW,
        settings.BIOMASS_WET_KG_M2_HIGH,
    ) == (18.32, 11.67, 23.84)


@pytest.mark.parametrize(("hectares", "smol_t", "gee_t"), REFERENCE)
def test_matches_smol_gee_to_the_tonne(
    hectares: float, smol_t: float, gee_t: tuple[int, int, int]
) -> None:
    result = wet_biomass(hectares)
    assert (result.t, result.t_low, result.t_high) == gee_t


@pytest.mark.parametrize(("hectares", "smol_t", "gee_t"), REFERENCE)
def test_matches_smol_to_the_tonne(
    hectares: float, smol_t: float, gee_t: tuple[int, int, int]
) -> None:
    assert wet_biomass(hectares).t == round(smol_t)


def test_corrected_and_raw_differ_on_the_dry_season_control() -> None:
    # WHY: smol-gee multiplies the raw area; the viewer must never do that.
    assert wet_biomass(15.70).t == 2876
    assert wet_biomass(434.21).t == 79547


def test_zero_and_none() -> None:
    assert wet_biomass(0.0) == WetBiomass(0, 0, 0)
    assert wet_biomass(None) is None


def test_negative_hectares_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        wet_biomass(-1.0)
