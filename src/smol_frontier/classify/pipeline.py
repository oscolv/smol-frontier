"""Full scene classification pipeline: masks → indices → GMM → post-process.

Runs the prior pipeline exactly, in order: validity mask → spectral indices →
Stage 0/0b pre-masks and pre-screen → Stage 1 (water vs. mat) → Stage 2 (dense
vs. interface) → water rescue → NSD substrate test → haze rescue (a frozen
second pass over haze-over-vegetation pixels inside the day's water mirror).
An identity gate compares this pipeline against the reference series, so the
order and cutoffs are spec-literal. No I/O except the reservoir area from
aoi.py; callers load the mirror.
"""

from dataclasses import dataclass

import numpy as np

from smol_frontier.aoi import reservoir_area_ha
from smol_frontier.classify import indices
from smol_frontier.classify.gmm import (
    MAT_PENDING,
    border_strip,
    haze_pass,
    rescue_water,
    stage1,
    stage2,
    substrate_test,
)
from smol_frontier.classify.masks import (
    cloud_fraction,
    haze_vegetation,
    valid_fraction,
    valid_mask,
)
from smol_frontier.config import settings
from smol_frontier.ingest import IngestedScene

__all__ = ["PIXEL_AREA_M2", "SceneResult", "classify"]

# WHY: hectare conversion for the 10 m ingest grid — every classified pixel
#   covers 10 m x 10 m, so pixel count x 100 m² / 10 000 = hectares.
# HOW: fixed by the ingest grid (B04 reference); the reservoir area itself is
#   never a constant — it comes from aoi.py.
PIXEL_AREA_M2 = 100.0


@dataclass(frozen=True, eq=False)
class SceneResult:
    """One classified scene: map, hectares, percents, and diagnostics."""

    scene_id: str
    class_map: np.ndarray
    ha_per_class: dict[int, float]
    hyacinth_total_ha: float
    pct_reservoir: float
    reservoir_area_ha: float
    valid_fraction: float
    cloud_fraction: float
    gmm1_sep: float
    gmm2_sep: float
    nir_thr_used: float | None
    nir_water_frac: float
    n_rescued: int
    n_substrate: int
    hyacinth_under_haze_ha: float = 0.0


def classify(scene: IngestedScene, mirror: np.ndarray | None = None) -> SceneResult:
    """Classify one ingested scene; return the map and its summary.

    ``mirror`` is the day's water mirror on the scene grid (bool); None (no
    level, flood map or curve, or a grid mismatch) rescues nothing. Haze over
    vegetation inside the mirror gets a frozen second pass: every other pixel
    and every diagnostic stays as the first pass left it.
    """
    s = settings
    scl = scene.bands["SCL"]
    inside = np.asanyarray(scene.inside, dtype=bool)
    b02 = scene.reflectance("B02")
    b03 = scene.reflectance("B03")
    b04 = scene.reflectance("B04")
    b05 = scene.reflectance("B05")
    b08 = scene.reflectance("B08")
    b8a = scene.reflectance("B8A")
    b11 = scene.reflectance("B11")

    bands = {
        "B02": b02,
        "B03": b03,
        "B04": b04,
        "B05": b05,
        "B08": b08,
        "B8A": b8a,
        "B11": b11,
    }
    valid = valid_mask(scl, bands=bands, inside=inside)
    # Indices see non-negative copies: slightly negative dark-water
    # reflectance (Sen2Cor over-correction) would otherwise push ratios
    # outside [-1, 1]. np.maximum returns new arrays, so the originals (and
    # the mask above, which must see the true values) are untouched; NaN
    # stays NaN and positive pixels are unchanged.
    clipped = {name: np.maximum(arr, 0) for name, arr in bands.items()}
    ndvi = indices.ndvi(clipped["B08"], clipped["B04"])
    ndre = indices.ndre(clipped["B8A"], clipped["B05"])
    mndwi = indices.mndwi(clipped["B03"], clipped["B11"])
    bsi = indices.bsi(clipped["B11"], clipped["B04"], clipped["B08"], clipped["B02"])
    nsd = indices.nsd(clipped["B08"], clipped["B11"])
    fai = indices.fai(clipped["B08"], clipped["B04"], clipped["B11"])
    border = border_strip(inside)

    first = stage1(fai, ndvi, ndre, mndwi, bsi, nsd, b08, valid, border)
    second = stage2(ndvi, ndre, first.classes, first.confidence)
    rescued = rescue_water(second.classes, second.confidence, valid, ndvi, b08)
    tested = substrate_test(rescued.classes, rescued.confidence, valid, nsd, ndvi)
    classes = tested.classes
    assert (classes != MAT_PENDING).all(), "MAT_PENDING pixels survived Stage 2"

    under_haze = np.zeros(classes.shape, dtype=bool)
    if mirror is not None:
        rescue = haze_vegetation(scl, bands, inside) & np.asanyarray(mirror, bool)
        # Disjoint from valid by construction; the guard mirrors the reference.
        rescue &= ~valid
        if rescue.any():
            haze_classes = haze_pass(
                rescue,
                classes,
                valid,
                border,
                fai=fai,
                ndvi=ndvi,
                ndre=ndre,
                mndwi=mndwi,
                bsi=bsi,
                nsd=nsd,
                b08=b08,
                first=first,
                second=second,
            )
            classes = np.where(rescue, haze_classes, classes)
            under_haze = rescue & np.isin(
                classes, (s.CLASS_DENSE_HYACINTH, s.CLASS_INTERFACE)
            )
    class_map = classes.astype(np.uint8)

    codes = (
        s.CLASS_NODATA,
        s.CLASS_WATER,
        s.CLASS_DENSE_HYACINTH,
        s.CLASS_INTERFACE,
        s.CLASS_BARE_SOIL,
    )
    ha_per_class = {
        code: float((class_map == code).sum()) * PIXEL_AREA_M2 / 10_000.0
        for code in codes
    }
    hyacinth_total_ha = (
        ha_per_class[s.CLASS_DENSE_HYACINTH] + ha_per_class[s.CLASS_INTERFACE]
    )
    area_ha = reservoir_area_ha()
    return SceneResult(
        scene_id=scene.scene.id,
        class_map=class_map,
        ha_per_class=ha_per_class,
        hyacinth_total_ha=hyacinth_total_ha,
        pct_reservoir=hyacinth_total_ha / area_ha * 100.0,
        reservoir_area_ha=area_ha,
        valid_fraction=valid_fraction(scl, bands=bands, inside=inside),
        cloud_fraction=cloud_fraction(scl, b02, b11, b04, inside),
        gmm1_sep=first.gmm1_sep,
        gmm2_sep=second.gmm2_sep,
        nir_thr_used=first.nir_thr_used,
        nir_water_frac=first.nir_water_frac,
        n_rescued=rescued.n_changed,
        n_substrate=tested.n_changed,
        hyacinth_under_haze_ha=float(under_haze.sum()) * PIXEL_AREA_M2 / 10_000.0,
    )
