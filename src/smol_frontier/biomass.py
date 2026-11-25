"""Wet-weight hyacinth biomass from corrected hectares (issue #102).

One deterministic conversion: hectares x 10 000 m2/ha x density kg/m2 /
1 000 kg/t, for the central, low and high field density in ``config.py``.
The caller passes the exposed-bed-corrected hectares, never the raw ones.
"""

import math
from dataclasses import dataclass

from smol_frontier.config import settings

__all__ = ["WetBiomass", "wet_biomass"]


@dataclass(frozen=True)
class WetBiomass:
    """Tonnes of wet weight at the central, low and high density."""

    t: int
    t_low: int
    t_high: int


def _tonnes(hectares: float, kg_m2: float) -> int:
    # WHY half up (floor + 0.5): the same rounding as the GEE viewer's
    #   Math.round, so the figures match smol-gee to the tonne.
    return math.floor(hectares * 10 * kg_m2 + 0.5)


def wet_biomass(hectares: float | None) -> WetBiomass | None:
    """Wet biomass of ``hectares`` of hyacinth; None when there is no area."""
    if hectares is None:
        return None
    if hectares < 0:
        msg = f"negative hyacinth area: {hectares} ha"
        raise ValueError(msg)
    return WetBiomass(
        _tonnes(hectares, settings.BIOMASS_WET_KG_M2),
        _tonnes(hectares, settings.BIOMASS_WET_KG_M2_LOW),
        _tonnes(hectares, settings.BIOMASS_WET_KG_M2_HIGH),
    )
