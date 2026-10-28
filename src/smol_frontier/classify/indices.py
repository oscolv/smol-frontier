"""Spectral indices on surface reflectance (pure numpy, no I/O).

All ratios use safe division: a zero denominator yields NaN (0/0 → NaN, never
inf). Outputs are float32; NaN inputs (nodata) propagate as NaN.
"""

import numpy as np

__all__ = ["bsi", "fai", "mndwi", "ndre", "ndvi", "nsd"]

# WHY: nominal Sentinel-2 central wavelengths (nm) for the FAI baseline slope.
LAMBDA_B04_NM = 665.0
LAMBDA_B08_NM = 833.0
LAMBDA_B8A_NM = 865.0
LAMBDA_B11_NM = 1610.0


def _ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """Element-wise ratio; zero denominator → NaN, never inf (float32)."""
    num = np.asanyarray(numerator, dtype=np.float64)
    den = np.asanyarray(denominator, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / den
    return np.where(den == 0.0, np.nan, out).astype(np.float32)


def ndvi(b08: np.ndarray, b04: np.ndarray) -> np.ndarray:
    """Normalized difference vegetation index (B08 − B04) / (B08 + B04)."""
    return _ratio(b08 - b04, b08 + b04)


def mndwi(b03: np.ndarray, b11: np.ndarray) -> np.ndarray:
    """Modified normalized difference water index (B03 − B11) / (B03 + B11)."""
    return _ratio(b03 - b11, b03 + b11)


def ndre(b8a: np.ndarray, b05: np.ndarray) -> np.ndarray:
    """Red-edge NDVI (B8A − B05) / (B8A + B05)."""
    return _ratio(b8a - b05, b8a + b05)


def bsi(
    b11: np.ndarray, b04: np.ndarray, b08: np.ndarray, b02: np.ndarray
) -> np.ndarray:
    """Bare soil index ((B11 + B04) − (B08 + B02)) / ((B11 + B04) + (B08 + B02))."""
    return _ratio((b11 + b04) - (b08 + b02), (b11 + b04) + (b08 + b02))


def nsd(b08: np.ndarray, b11: np.ndarray) -> np.ndarray:
    """NIR–SWIR difference B08 − B11.

    Water under a canopy annihilates SWIR → high NSD; exposed sediment
    reflects SWIR → low NSD.
    """
    return np.asanyarray(b08 - b11, dtype=np.float32)


def fai(
    nir: np.ndarray,
    red: np.ndarray,
    swir1: np.ndarray,
    nir_wavelength_nm: float = LAMBDA_B08_NM,
) -> np.ndarray:
    """Floating algae index (Hu, 2009, RSE).

    FAI = NIR − [RED + (SWIR − RED) · (λNIR − 665) / (1610 − 665)].

    B08 (833 nm) is the default NIR band — the historical baseline of the
    series. Pass B8A with ``nir_wavelength_nm=865.0`` for the narrow-NIR
    variant.

    NOTE: the project brief quotes a B08 slope of 0.1759, but the stated
    formula and wavelengths give (833 − 665) / (1610 − 665) = 168/945 ≈
    0.17778. The formula is implemented as written; if 0.1759 is the
    intended constant, this is a one-line change.
    """
    slope = (nir_wavelength_nm - LAMBDA_B04_NM) / (LAMBDA_B11_NM - LAMBDA_B04_NM)
    baseline = np.asanyarray(red) + (np.asanyarray(swir1) - np.asanyarray(red)) * slope
    return np.asanyarray(np.asanyarray(nir) - baseline, dtype=np.float32)
