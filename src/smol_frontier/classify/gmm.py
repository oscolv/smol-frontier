"""GMM classification, parts 1–2: pre-masks, Stage 1, Stage 2, post-process.

Methodology reproduced from the author's prior research: soil and fixed-NIR
water pre-masks, a turbidity pre-screen, then a 2-D GMM on FAI x NDVI (Stage 1:
water vs. mat), a 2-D GMM on NDVI x NDRE over the mat (Stage 2: dense vs.
interface), a water → interface rescue, and the NSD substrate test. An
identity gate compares this pipeline against the reference series, so every
cutoff below is spec-literal. Pure numpy + sklearn + scipy; no I/O.
"""

import math

import numpy as np
from scipy.ndimage import binary_dilation, distance_transform_edt
from sklearn.mixture import GaussianMixture

from smol_frontier.config import settings

__all__ = [
    "MAT_PENDING",
    "RefinementResult",
    "Stage1Result",
    "Stage2Result",
    "border_strip",
    "nir_valley",
    "rescue_water",
    "stage1",
    "stage2",
    "substrate_test",
]

# WHY: provisional code for mat pixels leaving Stage 1 — negative so it can
#   never be mistaken for a final class code; Stage 2 resolves it.
MAT_PENDING = -1

# WHY: NIR valley detector geometry — 80 bins over the B08 range where the
#   water/mat valley lives, 5-bin smoothing against histogram noise, search
#   window strictly inside the populated range.
VALLEY_BINS = 80
VALLEY_LO = 0.08
VALLEY_HI = 0.55
VALLEY_SMOOTH = 5
VALLEY_SEARCH_LO = 0.17
VALLEY_SEARCH_HI = 0.38

# WHY: bimodality proof — the water-side peak must clearly dominate the
#   valley, else the "valley" is noise and the fallback applies.
VALLEY_RATIO = 3.0
VALLEY_EPS = 1e-6
VALLEY_MIN_PIXELS = 200
VALLEY_MIN_BINS = 5

# WHY: GMM budget — below 20 pixels there is nothing to split ("no mat");
#   above 50 000 the fit subsamples for speed with a fixed seed.
GMM_MIN_PIXELS = 20
GMM_MAX_SAMPLE = 50_000
GMM_RANDOM_STATE = 42
GMM_MAX_ITER = 200
GMM_TOL = 1e-3
GMM_N_INIT = 3

# WHY: degenerate-fit guard — pooled std below this reports infinite
#   separation instead of dividing by ~zero. (The 1.5 adaptive-NIR cutoff
#   lives in config.py as SEP_DOUBTFUL, shared with the quality flag so the
#   two can never drift apart.)
SEP_EPS = 1e-9

# WHY: no-mat rules — under 2 % high-FAI pixels (≈25 ha) there is no mat to
#   find, and a "mat" component darker than 0.005 mean FAI means the GMM
#   split water in two.
MAT_FAI_FRAC_MIN = 0.02
MAT_MEAN_FAI_MIN = 0.005

# WHY: valley margins — the pre-screen widens only past NIR_WATER_MAX plus a
#   0.02 margin; the adaptive attempt only past 0.14.
VALLEY_MARGIN = 0.02
ADAPT_VALLEY_MIN = 0.14

# WHY: Stage 2 floor — below 4 mat pixels there is nothing to split, so all
#   of them resolve to interface (the "not clearly dense" bucket).
STAGE2_MIN_PIXELS = 4


class Stage1Result:
    """Stage 1 outputs: classes, mat mask, diagnostics, confidence, rescue count."""

    def __init__(
        self,
        classes: np.ndarray,
        mat_mask: np.ndarray,
        gmm1_sep: float,
        nir_thr_used: float | None,
        nir_water_frac: float,
        no_mat: bool,
        confidence: np.ndarray,
        n_fai_rescued: int,
    ) -> None:
        self.classes = classes
        self.mat_mask = mat_mask
        self.gmm1_sep = gmm1_sep
        self.nir_thr_used = nir_thr_used
        self.nir_water_frac = nir_water_frac
        self.no_mat = no_mat
        self.confidence = confidence
        # Pixels flipped to mat by the FAI rescue (the author's FAI_RESCATE
        # flag fires when this is > 0).
        self.n_fai_rescued = n_fai_rescued


def nir_valley(b08: np.ndarray) -> float:
    """NIR valley threshold from the B08 histogram (pure function).

    Falls back to NIR_WATER_MAX when the data are too few, the search
    window is degenerate, or no clear valley exists.
    """
    values = np.asanyarray(b08, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    fallback = settings.NIR_WATER_MAX
    if values.size < VALLEY_MIN_PIXELS:
        return fallback
    hist, edges = np.histogram(values, bins=VALLEY_BINS, range=(VALLEY_LO, VALLEY_HI))
    centers = (edges[:-1] + edges[1:]) / 2.0
    smooth = np.convolve(hist, np.ones(VALLEY_SMOOTH) / VALLEY_SMOOTH, mode="same")
    search = (centers > VALLEY_SEARCH_LO) & (centers < VALLEY_SEARCH_HI)
    if int(search.sum()) < VALLEY_MIN_BINS:
        return fallback
    pick = int(np.argmin(smooth[search]))
    valley = float(centers[search][pick])
    height = float(smooth[search][pick])
    left = (centers > VALLEY_LO) & (centers < valley)
    if not left.any():
        return fallback
    if float(smooth[left].max()) < VALLEY_RATIO * (height + VALLEY_EPS):
        return fallback
    return valley


def border_strip(aoi_mask: np.ndarray, pixel_size_m: float = 10.0) -> np.ndarray:
    """Strip within BORDER_ZONE_M of the reservoir edge (pure function)."""
    aoi = np.asanyarray(aoi_mask, dtype=bool)
    width_px = settings.BORDER_ZONE_M / pixel_size_m
    dist = distance_transform_edt(aoi)
    return aoi & ~(dist > width_px)


def _separability(means: np.ndarray, covariances: np.ndarray) -> float:
    """||mu1 - mu2|| over the pooled per-dimension std (inf if degenerate)."""
    pooled_var = (np.trace(covariances[0]) / 2 + np.trace(covariances[1]) / 2) / 2
    pooled_std = math.sqrt(max(pooled_var, 0.0))
    if pooled_std < SEP_EPS:
        return math.inf
    return float(np.linalg.norm(means[0] - means[1]) / pooled_std)


def _is_no_mat(fai_values: np.ndarray, mat_mean_fai: float) -> bool:
    """No-mat rule: too few high-FAI pixels or the mat comp is dark."""
    frac = float((np.asanyarray(fai_values) > settings.FAI_FLOAT_MIN).mean())
    return frac < MAT_FAI_FRAC_MIN or float(mat_mean_fai) < MAT_MEAN_FAI_MIN


def _fit_water_posterior(
    features: np.ndarray,
) -> tuple[np.ndarray, int, np.ndarray, np.ndarray]:
    """Fit the 2-component GMM; return P(water), water comp, means, covs."""
    model = GaussianMixture(
        n_components=2,
        covariance_type="full",
        random_state=GMM_RANDOM_STATE,
        max_iter=GMM_MAX_ITER,
        tol=GMM_TOL,
        n_init=GMM_N_INIT,
    )
    rng = np.random.default_rng(GMM_RANDOM_STATE)
    if len(features) > GMM_MAX_SAMPLE:
        sample = rng.choice(len(features), size=GMM_MAX_SAMPLE, replace=False)
        model.fit(features[sample])
    else:
        model.fit(features)
    water_comp = int(np.argmin(model.means_[:, 0]))
    return (
        model.predict_proba(features)[:, water_comp],
        water_comp,
        model.means_,
        model.covariances_,
    )


def stage1(
    fai: np.ndarray,
    ndvi: np.ndarray,
    ndre: np.ndarray,
    mndwi: np.ndarray,
    bsi: np.ndarray,
    nsd: np.ndarray,
    b08: np.ndarray,
    valid: np.ndarray,
    border: np.ndarray,
) -> Stage1Result:
    """Run pre-masks + Stage 1 GMM; return classes so far and diagnostics."""
    s = settings
    shapes = {
        np.shape(a) for a in (fai, ndvi, ndre, mndwi, bsi, nsd, b08, valid, border)
    }
    if len(shapes) != 1:
        msg = f"all stage-1 inputs must share one shape, got {sorted(shapes)}"
        raise ValueError(msg)
    shape = shapes.pop()
    fai = np.asanyarray(fai, dtype=np.float64)
    ndvi = np.asanyarray(ndvi, dtype=np.float64)
    ndre = np.asanyarray(ndre, dtype=np.float64)
    mndwi = np.asanyarray(mndwi, dtype=np.float64)
    bsi = np.asanyarray(bsi, dtype=np.float64)
    nsd = np.asanyarray(nsd, dtype=np.float64)
    b08 = np.asanyarray(b08, dtype=np.float64)
    valid = np.asanyarray(valid, dtype=bool)
    border = np.asanyarray(border, dtype=bool)

    classes = np.zeros(shape, dtype=np.int16)
    confidence = np.full(shape, np.nan, dtype=np.float32)
    flat_classes = classes.ravel()
    flat_conf = confidence.ravel()

    # Stage 0 — soil, only inside the border strip.
    soil = (
        valid
        & border
        & (bsi > s.SOIL_BSI_MIN)
        & (mndwi < s.SOIL_MNDWI_MAX)
        & (ndvi < s.SOIL_NDVI_MAX)
        & (ndre < s.SOIL_NDRE_MAX)
        & (nsd < s.SOIL_NSD_MAX)
    )
    classes[soil] = s.CLASS_BARE_SOIL
    confidence[soil] = 1.0

    # Stage 0b — water by fixed NIR, whole scene.
    water0b = valid & ~soil & (b08 < s.NIR_WATER_MAX) & (nsd < s.NSD_VEG_MIN)
    classes[water0b] = s.CLASS_WATER
    confidence[water0b] = 1.0

    # GMM-entering pixels need finite FAI/NDVI (features) and B08 (valley).
    # Valid-but-nonfinite leftovers stay nodata (class 0, NaN confidence).
    gmm = (
        valid
        & ~soil
        & ~water0b
        & np.isfinite(fai)
        & np.isfinite(ndvi)
        & np.isfinite(b08)
    )

    # Turbidity pre-screen: a GMM set with no dark mode gets valley-widened,
    # but the widening only counts when it moves at least one pixel to water.
    nir_thr_used: float | None = None
    pre_water = np.zeros(shape, dtype=bool)
    if int(gmm.sum()) > 0 and float(np.percentile(b08[gmm], 5)) > s.NIR_WATER_MAX:
        valley = nir_valley(b08[gmm])
        if valley > s.NIR_WATER_MAX + VALLEY_MARGIN:
            candidate = (
                gmm & (b08 < valley) & (fai < s.FAI_FLOAT_MIN) & (nsd < s.NSD_VEG_MIN)
            )
            if int(candidate.sum()) > 0:
                pre_water = candidate
                classes[pre_water] = s.CLASS_WATER
                confidence[pre_water] = 1.0
                gmm = gmm & ~pre_water
                nir_thr_used = valley

    denom = int((valid & ~soil).sum())
    nir_water_frac = float((water0b | pre_water).sum()) / denom if denom else 0.0

    def _no_mat_waterless(left: np.ndarray, gmm1_sep: float) -> Stage1Result:
        classes[left] = s.CLASS_WATER
        confidence[left] = 1.0
        return Stage1Result(
            classes=classes,
            mat_mask=np.zeros(shape, dtype=bool),
            gmm1_sep=gmm1_sep,
            nir_thr_used=nir_thr_used,
            nir_water_frac=nir_water_frac,
            no_mat=True,
            confidence=confidence,
            n_fai_rescued=0,
        )

    if int(gmm.sum()) < GMM_MIN_PIXELS:
        return _no_mat_waterless(gmm, math.inf)

    # Stage 1 — GMM on FAI x NDVI, up to two attempts. The no-mat check runs
    # on every attempt; no-mat results carry sep +inf (clean and confident,
    # never NaN — NaN stays reserved for GMM failure).
    cur = gmm
    sep = float("nan")
    n_fai_rescued = 0
    for attempt in range(2):
        features = np.column_stack([fai[cur], ndvi[cur]])
        p_water, water_comp, means, covs = _fit_water_posterior(features)
        sep = _separability(means, covs)
        mat_mean_fai = float(means[1 - water_comp, 0])
        if _is_no_mat(features[:, 0], mat_mean_fai):
            return _no_mat_waterless(cur, math.inf)
        if attempt == 0 and sep < s.SEP_DOUBTFUL and nir_thr_used is None:
            valley = nir_valley(b08[cur])
            if valley > ADAPT_VALLEY_MIN:
                adapt_water = cur & (b08 < valley) & (fai < s.FAI_FLOAT_MIN)
                # The widening only counts when it moves at least one pixel;
                # otherwise attempt 0 proceeds as is.
                if int(adapt_water.sum()) > 0:
                    classes[adapt_water] = s.CLASS_WATER
                    confidence[adapt_water] = 1.0
                    cur = cur & ~adapt_water
                    nir_thr_used = valley
                    if int(cur.sum()) < GMM_MIN_PIXELS:
                        return _no_mat_waterless(cur, math.inf)
                    continue
        pos = np.flatnonzero(cur)
        is_water = p_water >= s.TAU_WATER
        flat_classes[pos[is_water]] = s.CLASS_WATER
        flat_classes[pos[~is_water]] = MAT_PENDING
        flat_conf[pos[is_water]] = p_water[is_water]
        flat_conf[pos[~is_water]] = 1.0 - p_water[~is_water]
        # FAI rescue: runs whenever the NIR water threshold was widened
        # at all (nir_thr_used > NIR_WATER_MAX), by the turbidity pre-screen
        # or the Stage 1 adaptive attempt — pixels the widened threshold
        # caught that still look vegetated are mat edge, not water.
        if nir_thr_used is not None and nir_thr_used > s.NIR_WATER_MAX:
            rescue = (
                cur
                & (b08 >= s.NIR_WATER_MAX)
                & (b08 < nir_thr_used)
                & (fai > s.FAI_FLOAT_MIN)
            )
            classes[rescue] = MAT_PENDING
            confidence[rescue] = 1.0
            n_fai_rescued = int(rescue.sum())
        break

    return Stage1Result(
        classes=classes,
        mat_mask=classes == MAT_PENDING,
        gmm1_sep=sep,
        nir_thr_used=nir_thr_used,
        nir_water_frac=nir_water_frac,
        no_mat=False,
        confidence=confidence,
        n_fai_rescued=n_fai_rescued,
    )


class Stage2Result:
    """Stage 2 outputs: resolved classes, separability, updated confidence."""

    def __init__(
        self,
        classes: np.ndarray,
        gmm2_sep: float,
        confidence: np.ndarray,
    ) -> None:
        self.classes = classes
        self.gmm2_sep = gmm2_sep
        self.confidence = confidence


class RefinementResult:
    """One post-process pass: classes, confidence, and pixels changed."""

    def __init__(
        self,
        classes: np.ndarray,
        confidence: np.ndarray,
        n_changed: int,
    ) -> None:
        self.classes = classes
        self.confidence = confidence
        self.n_changed = n_changed


def _one_shape(*arrays: np.ndarray) -> tuple[int, ...]:
    """Shared shape of all inputs, raising when they disagree."""
    shapes = {np.shape(a) for a in arrays}
    if len(shapes) != 1:
        msg = f"all inputs must share one shape, got {sorted(shapes)}"
        raise ValueError(msg)
    return shapes.pop()


def _fit_dense_posterior(
    features: np.ndarray,
) -> tuple[np.ndarray, int, np.ndarray, np.ndarray]:
    """Fit the 2-component GMM; return P(dense), dense comp, means, covs."""
    model = GaussianMixture(
        n_components=2,
        covariance_type="full",
        random_state=GMM_RANDOM_STATE,
        max_iter=GMM_MAX_ITER,
        tol=GMM_TOL,
        n_init=GMM_N_INIT,
    )
    rng = np.random.default_rng(GMM_RANDOM_STATE)
    if len(features) > GMM_MAX_SAMPLE:
        sample = rng.choice(len(features), size=GMM_MAX_SAMPLE, replace=False)
        model.fit(features[sample])
    else:
        model.fit(features)
    dense_comp = int(np.argmax(model.means_[:, 0]))
    return (
        model.predict_proba(features)[:, dense_comp],
        dense_comp,
        model.means_,
        model.covariances_,
    )


def stage2(
    ndvi: np.ndarray,
    ndre: np.ndarray,
    classes: np.ndarray,
    confidence: np.ndarray,
) -> Stage2Result:
    """Split MAT_PENDING pixels into dense vs. interface (GMM on NDVI x NDRE).

    Dense is the higher-mean-NDVI component; dense iff P(dense) >= TAU_HYACINTH.
    Below 4 mat pixels (or none) everything resolves to interface and the
    separability is NaN (diagnostic only). Mat pixels with non-finite features
    cannot enter the GMM and resolve to interface. No 3-D fallback — it was
    retired in the prior research (tied or worsened against PlanetScope at
    3 m). Inputs are not mutated.
    """
    s = settings
    _one_shape(ndvi, ndre, classes, confidence)
    ndvi = np.asanyarray(ndvi, dtype=np.float64)
    ndre = np.asanyarray(ndre, dtype=np.float64)
    out_classes = np.asanyarray(classes).copy()
    out_conf = np.asanyarray(confidence, dtype=np.float32).copy()

    mat = out_classes == MAT_PENDING
    n_mat = int(mat.sum())
    if n_mat == 0:
        return Stage2Result(out_classes, math.nan, out_conf)
    if n_mat < STAGE2_MIN_PIXELS:
        out_classes[mat] = s.CLASS_INTERFACE
        out_conf[mat] = 1.0
        return Stage2Result(out_classes, math.nan, out_conf)

    finite = mat & np.isfinite(ndvi) & np.isfinite(ndre)
    out_classes[mat & ~finite] = s.CLASS_INTERFACE
    out_conf[mat & ~finite] = 1.0
    if int(finite.sum()) < 2:
        # Degenerate guard: two components cannot fit on one pixel.
        out_classes[finite] = s.CLASS_INTERFACE
        out_conf[finite] = 1.0
        return Stage2Result(out_classes, math.nan, out_conf)

    features = np.column_stack([ndvi[finite], ndre[finite]])
    p_dense, _, means, covs = _fit_dense_posterior(features)
    sep = _separability(means, covs)
    pos = np.flatnonzero(finite)
    is_dense = p_dense >= s.TAU_HYACINTH
    flat_classes = out_classes.ravel()
    flat_conf = out_conf.ravel()
    flat_classes[pos[is_dense]] = s.CLASS_DENSE_HYACINTH
    flat_classes[pos[~is_dense]] = s.CLASS_INTERFACE
    flat_conf[pos[is_dense]] = p_dense[is_dense]
    flat_conf[pos[~is_dense]] = 1.0 - p_dense[~is_dense]
    return Stage2Result(out_classes, sep, out_conf)


def rescue_water(
    classes: np.ndarray,
    confidence: np.ndarray,
    valid: np.ndarray,
    ndvi: np.ndarray,
    b08: np.ndarray,
) -> RefinementResult:
    """Reclassify vegetated water pixels as interface (post-process 1).

    Any WATER pixel (whatever its origin, including NIR pre-masked water) that
    is valid with high NDVI and high NIR is a mat edge the Stage 1 GMM put
    into water — water, however turbid, cannot produce both. Inputs are not
    mutated.
    """
    s = settings
    _one_shape(classes, confidence, valid, ndvi, b08)
    out_classes = np.asanyarray(classes).copy()
    out_conf = np.asanyarray(confidence, dtype=np.float32).copy()
    rescued = (
        (out_classes == s.CLASS_WATER)
        & np.asanyarray(valid, dtype=bool)
        & (np.asanyarray(ndvi) > s.NDVI_RESCUE_WATER)
        & (np.asanyarray(b08) > s.NIR_RESCUE_WATER)
    )
    out_classes[rescued] = s.CLASS_INTERFACE
    out_conf[rescued] = 1.0
    return RefinementResult(out_classes, out_conf, int(rescued.sum()))


def substrate_test(
    classes: np.ndarray,
    confidence: np.ndarray,
    valid: np.ndarray,
    nsd: np.ndarray,
    ndvi: np.ndarray,
) -> RefinementResult:
    """Reclassify near-shore over-sediment vegetation as soil (post-process 2).

    Runs last and has the last word: dense/interface candidates with low NSD
    near bare exposed sediment (soil anchor with low NDVI) are dry-season
    shoreline vegetation, not floating hyacinth. With no anchor (full
    reservoir: no shoreline context) nothing changes. Inputs are not mutated.
    """
    s = settings
    _one_shape(classes, confidence, valid, nsd, ndvi)
    out_classes = np.asanyarray(classes).copy()
    out_conf = np.asanyarray(confidence, dtype=np.float32).copy()
    valid = np.asanyarray(valid, dtype=bool)
    candidates = (
        ((out_classes == s.CLASS_DENSE_HYACINTH) | (out_classes == s.CLASS_INTERFACE))
        & valid
        & (np.asanyarray(nsd) < s.NSD_SUBSTRATE_SOIL)
    )
    anchor = (
        (out_classes == s.CLASS_BARE_SOIL)
        & valid
        & (np.asanyarray(ndvi) < s.NDVI_BARE_SEDIMENT)
    )
    if not anchor.any():
        return RefinementResult(out_classes, out_conf, 0)
    # Default structuring element (4-connectivity cross), NOT a Euclidean
    # buffer: this matches the prior pipeline (5 px ≈ 50 m).
    near = binary_dilation(anchor, iterations=s.NSD_SEDIMENT_DIST_PX)
    hit = candidates & near
    out_classes[hit] = s.CLASS_BARE_SOIL
    out_conf[hit] = 1.0
    return RefinementResult(out_classes, out_conf, int(hit.sum()))
