"""Per-scene quality traffic light and regime.

Pure functions over :class:`SceneResult`: the light and its flags follow the
author's prior methodology exactly (an identity gate compares them against the
reference series), so every comparison below is spec-literal. The regime is
reported in its own field and never degrades quality — no
separability/confidence metric predicted the PlanetScope gap in those regimes.
No I/O.
"""

import math
from dataclasses import dataclass
from typing import Literal

from smol_frontier.classify.pipeline import SceneResult
from smol_frontier.config import settings

__all__ = ["QualityReport", "assess_quality", "regime"]

Light = Literal["GOOD", "DOUBTFUL", "POOR"]


@dataclass(frozen=True)
class QualityReport:
    """Quality verdict for one scene, with the inputs used for traceability."""

    light: Light
    degrading_flags: list[str]
    info_flags: list[str]
    publishable_pct: bool
    regime: str
    valid_fraction: float
    cloud_fraction: float
    gmm1_sep: float
    gmm2_sep: float


def regime(soil_ha: float | None, nir_water_frac: float | None) -> str:
    """Scene regime: "EXPOSED_BED", "TURBIDITY", or "" (clean).

    EXPOSED_BED has priority. None or NaN never exceed a threshold. Note the
    deliberate asymmetry: soil uses >, turbidity uses >=.
    """
    s = settings
    if (
        soil_ha is not None
        and not math.isnan(soil_ha)
        and soil_ha > s.REGIME_SOIL_MAX_HA
    ):
        return "EXPOSED_BED"
    if (
        nir_water_frac is not None
        and not math.isnan(nir_water_frac)
        and nir_water_frac >= s.REGIME_NIR_WATER_MIN
    ):
        return "TURBIDITY"
    return ""


def assess_quality(result: SceneResult) -> QualityReport:
    """Decide the traffic light, flags, regime, and percent publishability."""
    s = settings

    degrading: list[str] = []
    if result.valid_fraction < s.FRAC_DOUBTFUL:
        degrading.append("PARTIAL_CLOUDS")
    # NaN cloud fraction does not fire (NaN > cutoff is False).
    if result.cloud_fraction > s.CLOUD_FRAC_DOUBTFUL:
        degrading.append("HIGH_CLOUD")
    # gmm1_sep = +inf (the no-mat path) is perfect separation by convention
    # and does not fire; NaN (GMM failure) does.
    if math.isnan(result.gmm1_sep) or result.gmm1_sep < s.SEP_DOUBTFUL:
        degrading.append("GMM_WATER_OVERLAP")

    # Only the valid fraction can make a scene POOR; a POOR scene still
    # lists its degrading flags.
    if result.valid_fraction < s.MIN_VALID_FRACTION:
        light: Light = "POOR"
    elif degrading:
        light = "DOUBTFUL"
    else:
        light = "GOOD"

    ha = result.ha_per_class
    info: list[str] = []
    if not math.isfinite(result.gmm2_sep):
        info.append("NO_MAT")
    if result.nir_thr_used is not None:
        info.append("ADAPTIVE_NIR")
    if (
        math.isfinite(result.nir_water_frac)
        and result.nir_water_frac > s.TURBIDITY_HIGH_FRAC
    ):
        info.append("HIGH_TURBIDITY")
    if ha[s.CLASS_INTERFACE] > ha[s.CLASS_DENSE_HYACINTH]:
        info.append("INTERFACE_DOMINANT")

    return QualityReport(
        light=light,
        degrading_flags=degrading,
        info_flags=info,
        publishable_pct=result.valid_fraction >= s.FRAC_PUBLISHABLE,
        regime=regime(ha[s.CLASS_BARE_SOIL], result.nir_water_frac),
        valid_fraction=result.valid_fraction,
        cloud_fraction=result.cloud_fraction,
        gmm1_sep=result.gmm1_sep,
        gmm2_sep=result.gmm2_sep,
    )
