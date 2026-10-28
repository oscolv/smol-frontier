"""Sentinel-2 L2A DN to surface reflectance.

This module is the ONLY place that converts digital numbers to reflectance:

    reflectance = DN / 10000 + offset

The offset corrects for ESA processing baseline >= 04.00 (Jan 2022), which
added +0.1 (1000 DN) to the bands. Whether a given product still carries that
+0.1 is decided by rule, never by trusting metadata offsets:

- Do NOT read ``raster:bands[].offset``: Planetary Computer returns None
  there, and trusting it silently skips the correction. The author's prior
  research found a spurious step in the series from Jan 2022 because of this.
- Gate on the processing baseline too: scenes before 04.00 never had the
  offset and also arrive with the flag False.

Data-driven guard: Earth Search sometimes reports ``boa_offset_applied=False``
on scenes whose DN is already corrected (seen in S2C and some S2B scenes of
early 2025). If applying the offset would make more than 50 % of valid
(DN > 0) B2 pixels negative, the flag is distrusted, offset 0 is used, and a
warning is logged.

Pure functions on primitives and numpy arrays. No I/O.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["boa_offset", "dn_to_reflectance", "guard_boa_offset", "to_reflectance"]

# WHY: fixed L2A quantification — reflectance units are DN / 10000 by product
#   definition (https://docs.sentinel-hub.com/api/latest/data/s2l2a/).
DN_SCALE = 10_000.0

# WHY: fixed magnitude of the baseline >= 04.00 radiometric offset (+1000 DN);
#   the correction subtracts it back.
BOA_OFFSET_VALUE = -0.1

# WHY: first ESA processing baseline that added the +0.1 offset (Jan 2022);
#   earlier baselines never had it, so the gate is numeric on this floor.
BASELINE_WITH_OFFSET = (4, 0)

# WHY: fraction of valid B2 pixels allowed to go negative under the offset
#   before the flag is judged wrong — a genuinely uncorrected scene keeps
#   almost all water/land pixels positive after -0.1.
NEGATIVE_FRAC_LIMIT = 0.5


def _parse_baseline(baseline: str | None) -> tuple[int, ...] | None:
    """Parse "05.11" into (5, 11); None when missing or unparseable."""
    if baseline is None:
        return None
    try:
        return tuple(int(part) for part in str(baseline).split("."))
    except ValueError:
        return None


def boa_offset(baseline: str | None, boa_offset_applied: bool) -> float:
    """Rule-based BOA offset: -0.1 only for baseline >= 04.00 needing it."""
    if boa_offset_applied:
        return 0.0
    parsed = _parse_baseline(baseline)
    # Never apply a correction that cannot be attributed: unknown baselines
    # (and pre-04.00 scenes, which never had the offset) get 0.0.
    if parsed is None or parsed < BASELINE_WITH_OFFSET:
        return 0.0
    return BOA_OFFSET_VALUE


def guard_boa_offset(offset: float, b2_dn: np.ndarray) -> float:
    """Distrust a bad flag: if the offset negatives most valid B2, use 0."""
    if offset >= 0.0:
        return offset
    valid = b2_dn > 0
    if not valid.any():
        return offset
    negative_frac = ((b2_dn[valid] / DN_SCALE + offset) < 0.0).mean()
    if negative_frac > NEGATIVE_FRAC_LIMIT:
        logger.warning(
            "boa_offset_applied looks wrong (%.1f %% of valid B2 negative"
            " under the offset); using 0.0",
            100.0 * negative_frac,
        )
        return 0.0
    return offset


def dn_to_reflectance(dn: np.ndarray, offset: float) -> np.ndarray:
    """Convert DN to reflectance; nodata (DN <= 0) stays nodata (NaN)."""
    scaled = dn.astype(np.float64) / DN_SCALE + offset
    return np.where(dn > 0, scaled, np.nan)


def to_reflectance(
    dn: np.ndarray,
    baseline: str | None,
    boa_offset_applied: bool,
    b2_dn: np.ndarray | None = None,
) -> np.ndarray:
    """Full funnel: rule offset, data-driven guard, then conversion.

    Pass the B2 array explicitly via ``b2_dn`` when converting other bands;
    it defaults to ``dn`` (correct when converting B2 itself).
    """
    offset = boa_offset(baseline, boa_offset_applied)
    offset = guard_boa_offset(offset, dn if b2_dn is None else b2_dn)
    return dn_to_reflectance(dn, offset)
