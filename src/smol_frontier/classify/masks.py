"""Validity masks: SCL screening plus an SCL-independent spectral cloud test.

Pure numpy on reflectance arrays. No I/O.
"""

import numpy as np

__all__ = [
    "CLOUD_B02_MIN",
    "CLOUD_B11_MIN",
    "CLOUD_WHITENESS",
    "SCL_CLOUD",
    "SCL_INVALID",
    "cloud_fraction",
    "scl_invalid",
    "spectral_cloud",
    "valid_fraction",
    "valid_mask",
]

# WHY: SCL classes that disqualify a pixel (nodata, saturated, cloud shadow,
#   medium/high-probability cloud, thin cirrus).
# HOW: fixed per project spec. Class 11 (snow) is deliberately NOT excluded
#   (0 pixels in 628 cached Endho scenes 2017–2026); class 7 (unclassified)
#   is accepted because the spectral cloud test covers that gap.
SCL_INVALID = frozenset({0, 1, 3, 8, 9, 10})

# WHY: SCL classes counted as cloud for the cloud fraction (medium/high
#   probability and thin cirrus; shadows are invalid but not cloud).
SCL_CLOUD = frozenset({8, 9, 10})

# WHY: spectral cloud test, independent of SCL: since Processing Baseline
#   04.00, Sen2Cor's SCL misses low clouds and haze, labelling them "not
#   vegetated"/water/vegetation (documented ESA limitation). Real case:
#   Endho scene 2025-07-29, ~32 % cloud that SCL missed → 1025 ha of false
#   hyacinth. cloud = (B02 > 0.07) AND (B11 > 0.10) AND (B02 > 0.85 · B04).
# HOW: calibrated on the author's prior research (see PRIOR_WORK.md):
#   clean water/vegetation B02 p99 = 0.045; clouds B02 0.05–0.19 and
#   B11 0.13–0.29. The B11 guard protects turbid water (absorbs SWIR); the
#   B02 guard protects vegetation. 0.07 (not 0.10) catches thin haze
#   (B02 ≈ 0.06–0.09); 0.06 starts flagging bright hyacinth. Whiteness
#   guard: clouds are spectrally neutral (B02/B04 ≈ 0.99) while exposed
#   sediment is reddish (B02/B04 ≈ 0.73), so 0.85 separates them.
CLOUD_B02_MIN = 0.07
CLOUD_B11_MIN = 0.10
CLOUD_WHITENESS = 0.85


def scl_invalid(scl: np.ndarray) -> np.ndarray:
    """Pixels whose SCL class disqualifies them."""
    return np.isin(scl, sorted(SCL_INVALID))


def spectral_cloud(b02: np.ndarray, b11: np.ndarray, b04: np.ndarray) -> np.ndarray:
    """Pixels that look like cloud/haze spectrally, whatever SCL says."""
    b02 = np.asanyarray(b02)
    return (b02 > CLOUD_B02_MIN) & (b11 > CLOUD_B11_MIN) & (b02 > CLOUD_WHITENESS * b04)


def valid_mask(
    scl: np.ndarray,
    b02: np.ndarray,
    b11: np.ndarray,
    b04: np.ndarray,
    inside: np.ndarray,
) -> np.ndarray:
    """Inside the reservoir AND not SCL-invalid AND not spectral cloud."""
    return (
        np.asanyarray(inside, dtype=bool)
        & ~scl_invalid(scl)
        & ~spectral_cloud(b02, b11, b04)
    )


def _inside_count(inside: np.ndarray) -> int:
    return int(np.asanyarray(inside, dtype=bool).sum())


def valid_fraction(
    scl: np.ndarray,
    b02: np.ndarray,
    b11: np.ndarray,
    b04: np.ndarray,
    inside: np.ndarray,
) -> float:
    """Valid fraction of the reservoir (0.0 when the reservoir is empty)."""
    total = _inside_count(inside)
    if total == 0:
        return 0.0
    return float(valid_mask(scl, b02, b11, b04, inside).sum()) / total


def cloud_fraction(
    scl: np.ndarray,
    b02: np.ndarray,
    b11: np.ndarray,
    b04: np.ndarray,
    inside: np.ndarray,
) -> float:
    """SCL-cloud plus spectral-cloud fraction of the reservoir."""
    total = _inside_count(inside)
    if total == 0:
        return 0.0
    inside = np.asanyarray(inside, dtype=bool)
    cloudy = inside & (np.isin(scl, sorted(SCL_CLOUD)) | spectral_cloud(b02, b11, b04))
    return float(cloudy.sum()) / total
