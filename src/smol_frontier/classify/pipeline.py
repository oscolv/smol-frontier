"""Full scene classification pipeline: masks → indices → GMM → post-process.

Runs the prior pipeline exactly, in order: validity mask → spectral indices →
Stage 0/0b pre-masks and pre-screen → Stage 1 (water vs. mat) → Stage 2 (dense
vs. interface) → water rescue → NSD substrate test. An identity gate compares
this pipeline against the reference series, so the order and cutoffs are
spec-literal. No I/O except the reservoir area from aoi.py.
"""

from dataclasses import dataclass

import numpy as np

from smol_frontier.aoi import reservoir_area_ha
from smol_frontier.classify import indices
from smol_frontier.classify.gmm import (
    MAT_PENDING,
    border_strip,
    rescue_water,
    stage1,
    stage2,
    substrate_test,
)
from smol_frontier.classify.masks import cloud_fraction, valid_fraction, valid_mask
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


def classify(scene: IngestedScene) -> SceneResult:
    """Classify one ingested scene; return the map and its summary."""
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

    valid = valid_mask(scl, b02, b11, b04, inside)
    ndvi = indices.ndvi(b08, b04)
    ndre = indices.ndre(b8a, b05)
    mndwi = indices.mndwi(b03, b11)
    bsi = indices.bsi(b11, b04, b08, b02)
    nsd = indices.nsd(b08, b11)
    fai = indices.fai(b08, b04, b11)
    border = border_strip(inside)

    first = stage1(fai, ndvi, ndre, mndwi, bsi, nsd, b08, valid, border)
    second = stage2(ndvi, ndre, first.classes, first.confidence)
    rescued = rescue_water(second.classes, second.confidence, valid, ndvi, b08)
    tested = substrate_test(rescued.classes, rescued.confidence, valid, nsd, ndvi)
    classes = tested.classes
    assert (classes != MAT_PENDING).all(), "MAT_PENDING pixels survived Stage 2"
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
        valid_fraction=valid_fraction(scl, b02, b11, b04, inside),
        cloud_fraction=cloud_fraction(scl, b02, b11, b04, inside),
        gmm1_sep=first.gmm1_sep,
        gmm2_sep=second.gmm2_sep,
        nir_thr_used=first.nir_thr_used,
        nir_water_frac=first.nir_water_frac,
        n_rescued=rescued.n_changed,
        n_substrate=tested.n_changed,
    )
