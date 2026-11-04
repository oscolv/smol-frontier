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
from collections.abc import Mapping

import numpy as np

from smol_frontier.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "SPECTRAL_BANDS",
    "AnomalousProduct",
    "boa_offset",
    "check_radiometry",
    "dn_to_reflectance",
    "guard_boa_offset",
    "to_reflectance",
]

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

# WHY: the spectral bands every radiometric check covers — the seven
#   reflectance bands the classifier consumes (SCL is a class map, not DN).
SPECTRAL_BANDS = ("B02", "B03", "B04", "B05", "B08", "B8A", "B11")

# WHY: high percentile for the anomalous-product test — the max would let a
#   single hot pixel hide an 8-bit product, while p99.9 still sees the
#   ceiling over ~126k reservoir pixels.
P999 = 99.9

# WHY: names the retry target in the AnomalousProduct message without
#   importing sources.stac (which would drag pystac into this light
#   module); the canonical constants live there. Unknown sources fall
#   back to the generic "the other STAC source".
_OTHER_SOURCE = {
    "planetary-computer": "earth-search",
    "earth-search": "planetary-computer",
}


class AnomalousProduct(ValueError):
    """A STAC product whose DN range is unphysical (e.g. 8-bit data)."""


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


def check_radiometry(
    dn_bands: Mapping[str, np.ndarray],
    inside: np.ndarray,
    scene_id: str,
    source: str,
) -> dict[str, float]:
    """Flag 8-bit-like products on raw DN; return per-band DN p99.9.

    Runs BEFORE the offset and guard_boa_offset: a product is anomalous
    when EVERY spectral band's p99.9 over valid (DN > 0) inside pixels
    sits below DN_MAX_ANOMALOUS. A band with no valid inside pixel counts
    as below the ceiling — an empty band is itself broken. Raises
    AnomalousProduct naming the scene, source, and measured p99.9.
    """
    mask = np.asanyarray(inside, dtype=bool)
    ceiling = settings.DN_MAX_ANOMALOUS
    p999: dict[str, float] = {}
    for band in SPECTRAL_BANDS:
        valid = np.asanyarray(dn_bands[band])[mask]
        valid = valid[valid > 0]
        p999[band] = float(np.percentile(valid, P999)) if valid.size else 0.0
    if all(value < ceiling for value in p999.values()):
        measured = " ".join(f"{band}={p999[band]:.1f}" for band in SPECTRAL_BANDS)
        other = _OTHER_SOURCE.get(source, "the other STAC source")
        msg = (
            f"anomalous product {scene_id} ({source}): DN p99.9 over valid "
            f"inside pixels {measured} — every band below {ceiling}; "
            f"retry with {other}"
        )
        raise AnomalousProduct(msg)
    return p999


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
