"""Validity masks: SCL screening plus an SCL-independent spectral cloud test.

Pure numpy on reflectance arrays. No I/O.
"""

from collections.abc import Mapping
from typing import Any

import numpy as np

from smol_frontier.config import settings

__all__ = [
    "CLOUD_B02_MIN",
    "CLOUD_B11_MIN",
    "CLOUD_WHITENESS",
    "SCL_CLOUD",
    "SCL_INVALID",
    "SPECTRAL_BANDS",
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

# WHY: SCL classes counted as cloud for the cloud fraction — medium/high
#   probability and thin cirrus plus cloud shadow (3). The author's method
#   counts shadow as cloud for the high-cloud doubtful rule, so the identity
#   gate stays spec-literal. Shadows are still invalid (see SCL_INVALID);
#   they are just also cloudy.
SCL_CLOUD = frozenset({3, 8, 9, 10})

# WHY: spectral cloud test, independent of SCL: since Processing Baseline
#   04.00, Sen2Cor's SCL misses low clouds and haze, labelling them "not
#   vegetated"/water/vegetation (documented ESA limitation). Real case:
#   Endho scene 2025-07-29, 43.44 % of reservoir pixels flagged that SCL
#   did not flag; without the test the scene passes as valid (0.9806) with
#   998.79 ha of false hyacinth (see experiments/sensitivity.md).
#   cloud = (B02 > 0.07) AND (B11 > 0.10) AND (B02 > 0.85 · B04). The B11
#   guard protects turbid water (absorbs SWIR); the B02 guard protects
#   vegetation; the whiteness guard protects reddish sediment (clouds are
#   spectrally neutral, B02/B04 ≈ 0.99, while sediment is ≈ 0.73).
# HOW: Re-verified in this repo (see experiments/sensitivity.md): the
#   2025-07-29 haze scene is POOR via 43.44 % spectral-only pixels while
#   the 2025-07-24 turbid control stays GOOD/TURBIDITY with 0.0000 flagged.
#   Values from the author's prior research (see PRIOR_WORK.md): clean
#   water/vegetation B02 p99 = 0.045; clouds B02 0.05–0.19, B11 0.13–0.29;
#   0.07 (not 0.10) catches thin haze (B02 ≈ 0.06–0.09), 0.06 starts
#   flagging bright hyacinth.
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


# WHY: the spectral bands the validity mask range-checks — every band the
#   classifier consumes (see pipeline.classify), so a NaN or sub-floor value
#   anywhere disqualifies the pixel instead of poisoning an index.
SPECTRAL_BANDS = ("B02", "B03", "B04", "B05", "B08", "B8A", "B11")


def _fold_dict_style(
    b02: Any, b11: Any, inside: Any, bands: Mapping[str, Any] | None
) -> tuple[Any, Any, Any, Mapping[str, Any] | None]:
    """Fold valid_*(scl, bands, inside) into the flat argument slots."""
    if isinstance(b02, Mapping):
        if bands is None:
            bands = b02
        if inside is None:
            inside = b11
        b02, b11 = None, None
    return b02, b11, inside, bands


def _collect_bands(
    b02: Any = None,
    b11: Any = None,
    b04: Any = None,
    b03: Any = None,
    b05: Any = None,
    b08: Any = None,
    b8a: Any = None,
    bands: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge flat band args with a bands dict (dict keys win, any case)."""
    merged = {
        "B02": b02,
        "B03": b03,
        "B04": b04,
        "B05": b05,
        "B08": b08,
        "B8A": b8a,
        "B11": b11,
    }
    out = {key: value for key, value in merged.items() if value is not None}
    if bands is not None:
        for key, value in bands.items():
            out[str(key).upper()] = value
    return out


def valid_mask(
    scl: np.ndarray,
    b02: Any = None,
    b11: Any = None,
    b04: Any = None,
    inside: Any = None,
    b03: Any = None,
    b05: Any = None,
    b08: Any = None,
    b8a: Any = None,
    *,
    bands: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Inside AND not SCL-invalid AND not spectral cloud AND in range.

    Every provided spectral band must be finite and above
    REFLECTANCE_MIN_VALID; slightly negative dark water (Sen2Cor
    over-correction) stays valid, NaN and sub-floor values do not. Bands
    arrive flat (``valid_mask(scl, b02, b11, b04, inside, b03=..., ...)``)
    or as a dict (``valid_mask(scl, bands, inside)``); only provided bands
    are checked, so pass all seven (see SPECTRAL_BANDS).
    """
    b02, b11, inside, bands = _fold_dict_style(b02, b11, inside, bands)
    collected = _collect_bands(b02, b11, b04, b03, b05, b08, b8a, bands)
    mask = np.asanyarray(inside, dtype=bool) & ~scl_invalid(scl)
    c02, c11, c04 = (collected.get(key) for key in ("B02", "B11", "B04"))
    if c02 is not None and c11 is not None and c04 is not None:
        mask = mask & ~spectral_cloud(c02, c11, c04)
    floor = settings.REFLECTANCE_MIN_VALID
    for key in SPECTRAL_BANDS:
        arr = collected.get(key)
        if arr is None:
            continue
        arr = np.asanyarray(arr, dtype=np.float64)
        mask = mask & np.isfinite(arr) & (arr > floor)
    return mask


def _inside_count(inside: np.ndarray) -> int:
    return int(np.asanyarray(inside, dtype=bool).sum())


def valid_fraction(
    scl: np.ndarray,
    b02: Any = None,
    b11: Any = None,
    b04: Any = None,
    inside: Any = None,
    b03: Any = None,
    b05: Any = None,
    b08: Any = None,
    b8a: Any = None,
    *,
    bands: Mapping[str, Any] | None = None,
) -> float:
    """Valid fraction of the reservoir (0.0 when the reservoir is empty).

    Takes bands flat or as a dict, exactly like valid_mask.
    """
    b02, b11, inside, bands = _fold_dict_style(b02, b11, inside, bands)
    total = _inside_count(inside)
    if total == 0:
        return 0.0
    mask = valid_mask(scl, b02, b11, b04, inside, b03, b05, b08, b8a, bands=bands)
    return float(mask.sum()) / total


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
