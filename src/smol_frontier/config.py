"""Single source of truth for every smol-frontier parameter.

All paths, thresholds, bands, and class codes live in one frozen
:class:`Settings` object so no value is ever hard-coded at a call site.
"""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

__all__ = ["PROJECT_ROOT", "Settings", "settings"]

# WHY: anchor for every repo-relative default path, so `smolf` works no matter
#   which directory it is invoked from (a bare "data/..." path resolves against
#   the caller's cwd and breaks outside the repo root).
# HOW: derived from this file's location — src/smol_frontier/config.py, so the
#   repo root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_aoi_path() -> Path:
    """Resolve the AOI path, honoring the SMOLF_AOI env override."""
    if "SMOLF_AOI" in os.environ:
        return Path(os.environ["SMOLF_AOI"])
    return PROJECT_ROOT / "data/aoi/endho_vaso.shp"


@dataclass(frozen=True)
class Settings:
    """Frozen project settings. Construct with no args; env overrides apply."""

    # WHY: path to the Endho reservoir (vaso) polygon; every area computation
    #   starts from this geometry.
    # HOW: default follows the repo layout anchored at PROJECT_ROOT (absolute,
    #   so the caller's cwd cannot break it); overridable at runtime via
    #   SMOLF_AOI so runs can point at another copy without code changes.
    AOI_PATH: Path = field(default_factory=_default_aoi_path)

    # WHY: projected CRS in metres for the reservoir, so area means hectares.
    # HOW: fixed by geography — Endho sits in UTM zone 14N.
    UTM_CRS: str = "EPSG:32614"

    # NOTE: the classifier thresholds as a set are verified jointly by the
    #   identity gate (465 scenes); docs/method.md section 10 reports the
    #   gate figures.
    # WHY: GMM posterior cutoff above which a pixel counts as water at all;
    #   separates the reservoir surface from land before hyacinth mapping.
    # HOW: set in the author's prior research (Oct 2026, 465 Sentinel-2
    #   scenes 2017–2026). Re-verification in this repo (see
    #   experiments/sensitivity.md) does NOT reproduce the prior "at most
    #   7 ha" bound: max |Δ total| vs 0.80 is 22.68 ha at 0.99 (2023-05-01),
    #   13.28 ha at 0.95 and 7.92 ha at 0.50 (2019-04-17); median 0.00 ha at
    #   every grid point, p95 at most 5.62 ha, no light/regime change.
    TAU_WATER: float = 0.80

    # WHY: GMM posterior cutoff above which water counts as dense hyacinth
    #   rather than open water / interface; splits the water mask by cover.
    # HOW: Re-verified in this repo (see experiments/sensitivity.md): at
    #   0.45 and 0.65 the total is bit-identical to 0.55 in all 465 scenes
    #   (max |Δ total| 0.0000 ha) while dense/interface move up to 124.54 ha
    #   in opposite directions, so 0.55 is a stable split. Same origin as
    #   TAU_WATER (Oct 2026, 465 scenes).
    TAU_HYACINTH: float = 0.55

    # WHY: minimum valid (cloud-free, observed) fraction of the reservoir for
    #   a scene's percentage to be publishable — clouded area implicitly
    #   counts as "no hyacinth", so a cloudy scene's percentage understates
    #   coverage. Below this, hectares are still reported as a lower bound
    #   but the percentage is withheld.
    # HOW: fixed policy value per project spec; 0.95 keeps the understatement
    #   from missing data negligible relative to the reported percent.
    FRAC_PUBLISHABLE: float = 0.95

    # WHY: traffic-light management bands on percent of reservoir area covered
    #   by hyacinth: GREEN below GREEN_MAX_PCT, YELLOW up to RED_MIN_PCT,
    #   RED at or above RED_MIN_PCT.
    # HOW: fixed policy bands per project spec (GREEN < 30, YELLOW 30–50,
    #   RED >= 50).
    GREEN_MAX_PCT: float = 30.0
    RED_MIN_PCT: float = 50.0

    # WHY: minimum valid fraction of the reservoir for a scene to be
    #   classified at all — below it too little of the reservoir is observed
    #   to trust any coverage number.
    # HOW: fixed policy value per project spec.
    MIN_VALID_FRACTION: float = 0.80

    # WHY: reflectance floor for the validity mask — since processing baseline
    #   04.00, Sen2Cor over-corrects dark water in the NIR/SWIR, so real Endho
    #   water lands slightly below zero in offset scenes; that is water, not
    #   nodata (nodata would sit at exactly -0.10 had DN == 0 not been mapped
    #   to NaN first). Any band at or below this floor, or non-finite, fails
    #   the mask.
    # HOW: measured on this repo's 473 cached scenes (59.7M inside pixels, Nov
    #   2026): per-band minima over finite inside reflectance are B02 -0.0065,
    #   B03 -0.0020, B04 -0.0017, B05 -0.0174, B08 -0.0414, B8A -0.0162 and
    #   B11 -0.0006 (every dark-water SCL-6 minimum >= -0.02); zero pixels sit
    #   below -0.05 while 25 315 sit in [-0.05, 0), so the floor keeps every
    #   real measurement and only rejects sub-floor garbage.
    REFLECTANCE_MIN_VALID: float = -0.05

    # WHY: DN ceiling for the anomalous-product guard — a genuine Sentinel-2
    #   L2A product always has bright pixels (cloud, soil, mat) in at least
    #   one band, while the corrupt 2026-10-27 MPC product is 8-bit data (DN
    #   0-255) in every band. check_radiometry flags a product only when ALL
    #   seven bands sit below this ceiling, so one dark band can never
    #   false-positive.
    # HOW: joint p99.9 separation over this repo's 474 cached scenes (Nov
    #   2026): the bad product peaks at 255.0 (B02 255.0, B03 255.0, B04
    #   255.0, B05 251.0, B08 255.0, B8A 248.0, B11 251.0) while the most
    #   8-bit-like good scene still reaches 1958.0 (B08,
    #   S2B_14QMH_20211008_1_L2A). Per-band minima over the other 473 scenes
    #   are B02 462.0, B03 632.0, B04 497.8, B05 921.8, B08 1958.0, B8A
    #   1641.7, B11 974.1 — B02/B04 alone sit below 500, which is why the
    #   rule needs every band. 500 clears the bad max by 245 DN and the
    #   nearest good scene by 1458 DN. p99.9, not the max, so one hot pixel
    #   cannot hide a bad product.
    DN_MAX_ANOMALOUS: int = 500

    # WHY: catalog cloud-cover ceiling when extending the series with new
    #   scenes — generous on purpose, because the per-pixel SCL mask and the
    #   spectral cloud test filter clouds later anyway.
    # HOW: author's catalog value, see PRIOR_WORK.md.
    MAX_CLOUD_COVER_CATALOG: float = 50.0

    # WHY: cloud fraction above which a scene's coverage is doubtful even when
    #   classified — clouds hide hyacinth, biasing coverage downward.
    # HOW: fixed policy value per project spec, anchored on case 2026-06-14
    #   (~12 % cloud, doubtful).
    CLOUD_FRAC_DOUBTFUL: float = 0.10

    # WHY: valid fraction below which a classified scene is doubtful
    #   (PARTIAL_CLOUDS) — between here and MIN_VALID_FRACTION the scene is
    #   still classified but too much of the reservoir is unobserved to trust
    #   the coverage number fully.
    # HOW: fixed cutoff from the author's prior research (per-scene quality
    #   traffic light).
    FRAC_DOUBTFUL: float = 0.85

    # WHY: double-use separability cutoff — below it Stage 1 retries with the
    #   adaptive NIR threshold AND the scene is flagged doubtful
    #   (GMM_WATER_OVERLAP). One constant, so the retry rule and the quality
    #   flag can never drift apart.
    # HOW: moved from gmm.py (was SEP_ADAPT_MAX); fixed cutoff from the
    #   author's prior research.
    SEP_DOUBTFUL: float = 1.5

    # WHY: soil hectares above which the scene is in the EXPOSED_BED regime —
    #   the prior research found larger disagreement with a higher-resolution
    #   sensor in this regime (Sentinel-2 differs from same-day reference
    #   imagery on hyacinth), and no separability/confidence metric predicted
    #   that gap. So the regime is reported in its own field and does NOT
    #   degrade quality.
    # HOW: fixed cutoff from the author's prior research.
    REGIME_SOIL_MAX_HA: float = 50.0

    # WHY: nir_water_frac at or above which the scene is in the TURBIDITY
    #   regime — the prior research found larger disagreement with a
    #   higher-resolution sensor in this regime (Sentinel-2 differs from
    #   same-day reference imagery on hyacinth). Reported in its own field,
    #   never degrades quality.
    # HOW: fixed cutoff from the author's prior research.
    REGIME_NIR_WATER_MIN: float = 0.60

    # WHY: nir_water_frac above which the HIGH_TURBIDITY informational flag
    #   fires — notes a strongly turbid scene without changing the light.
    # HOW: fixed cutoff from the author's prior research.
    TURBIDITY_HIGH_FRAC: float = 0.70

    # WHY: level-change tolerance when refreshing the SIH record — the SIH
    #   rounds to cm, and between two copies a month apart the 26 645 common
    #   days had identical levels, so a change over 5 cm is a different
    #   series, not a correction; refreshes that exceed it are rejected
    #   unless --force.
    # HOW: fixed cutoff from the author's prior research (hydrology inputs).
    LEVEL_REVISION_TOL_M: float = 0.05

    # WHY: freezes the stage–area curve to the SIH copy that defined it —
    #   pairs past this date are ignored, so the exposed-bed correction only
    #   moves when the classifier moves.
    # HOW: fixed cutoff from the author's prior research (stage–area curve).
    CURVE_DATA_END: date = date(2026, 9, 20)

    # WHY: 0.10 m bins average out the SIH's cm rounding noise while keeping
    #   the V(h) shape; bins with fewer than 5 days are too noisy to keep.
    # HOW: fixed binning from the author's prior research (stage–area curve).
    CURVE_BIN_M: float = 0.10
    CURVE_BIN_MIN_N: int = 5

    # WHY: bins stop here — above it, past the free spillway crest at
    #   2018.0 m, the reported volume follows another rule.
    # HOW: fixed cutoff from the author's prior research (stage–area curve).
    CURVE_FIT_H_MAX: float = 2017.5

    # WHY: official (level m, volume hm³) points from the dam's design
    #   report — they anchor the fit where SIH bins are absent or suspect.
    # HOW: design-report values, fixed per project spec.
    CURVE_ANCHORS: tuple[tuple[float, float], ...] = (
        (2003.16, 52.1),
        (2018.0, 182.0),
        (2020.0, 208.0),
    )

    # WHY: weight of each official anchor — ≈ one bin of 2 500 days
    #   (sqrt(2500) = 50), so each anchor outvotes any single bin.
    # HOW: fixed weight from the author's prior research (stage–area curve).
    CURVE_W_ANCHOR: float = 50.0

    # WHY: bootstrap size and seed for the area uncertainty bands — fixed
    #   seed so the bands are reproducible run to run.
    # HOW: fixed budget from the author's prior research (stage–area curve).
    CURVE_BOOTSTRAP_B: int = 300
    CURVE_BOOTSTRAP_SEED: int = 42

    # WHY: top of the output grid — above it would be spline extrapolation.
    # HOW: fixed grid from the author's prior research (stage–area curve).
    CURVE_GRID_TOP: float = 2018.5

    # WHY: model-error leg of the exposed-bed cushion — V(h) sensitivity to
    #   the anchor weight (10 vs 200), interpolated by stage, so hyacinth
    #   hugging the shore is not stripped by curve error.
    # HOW: author's values, fixed per project spec: 0.29/0.41/0.25/0.66 km²
    #   at 2005/2010/2015/2018 (8b2 reproduced them), stored in ha.
    BED_CUSHION_MODEL_HA: tuple[tuple[float, float], ...] = (
        (2005.0, 29.0),
        (2010.0, 41.0),
        (2015.0, 25.0),
        (2018.0, 66.0),
    )

    # WHY: width of the strip near the reservoir edge where exposed bed
    #   appears at low water — the soil pre-mask applies ONLY there, never in
    #   open water.
    # HOW: prior research value; was 500 m, reduced to 150 m so soil does not
    #   invade the narrow arms in the south of Endho.
    BORDER_ZONE_M: float = 150.0

    # WHY: joint cutoffs identifying exposed sediment in the border strip:
    #   bare soil is bright (BSI), not open water (MNDWI), not vegetated
    #   (NDVI/NDRE), and shows no canopy-over-water SWIR absorption (NSD).
    # HOW: fixed cutoffs from the author's prior research (Stage 0 pre-mask).
    SOIL_BSI_MIN: float = 0.02
    SOIL_MNDWI_MAX: float = -0.12
    SOIL_NDVI_MAX: float = 0.25
    SOIL_NDRE_MAX: float = 0.25
    SOIL_NSD_MAX: float = 0.15

    # WHY: fixed-NIR water rule — pixels this dark in B08 are open water
    #   without needing the GMM. Endho's clear water has B08 ≈ 0.06–0.14
    #   (permanent sediment load), so the cutoff sits at the top of that.
    # HOW: fixed cutoff from the author's prior research (Stage 0b).
    NIR_WATER_MAX: float = 0.12

    # WHY: NSD guard on the NIR water rules — interface pixels with low B08
    #   but high NSD are vegetation over water, not open water.
    # HOW: fixed cutoff from the author's prior research.
    NSD_VEG_MIN: float = 0.15

    # WHY: FAI floor for floating vegetation; separates the water mode from
    #   the mat mode in the turbidity pre-screen, the no-mat check, and the
    #   FAI rescue.
    # HOW: fixed cutoff from the author's prior research.
    FAI_FLOAT_MIN: float = 0.04

    # WHY: rescue water → interface — at high coverage the Stage 1 GMM puts
    #   mat-edge pixels into water, but water, however turbid, cannot produce
    #   high NDVI and high NIR together; rescued pixels recover 12–35 % of
    #   hyacinth at high coverage and 0 ha in clean low-coverage water.
    # HOW: fixed cutoffs from the author's prior research (see PRIOR_WORK.md).
    NDVI_RESCUE_WATER: float = 0.30
    NIR_RESCUE_WATER: float = 0.15

    # WHY: NSD substrate test — in the dry season, vegetation over exposed
    #   sediment is not floating hyacinth; SWIR sees the substrate (low NSD).
    #   Gated by sediment proximity (last word, runs after the rescue) so real
    #   hyacinth over turbid water in a full reservoir (Jul–Dec) is untouched.
    # HOW: fixed cutoffs from the author's prior research (see PRIOR_WORK.md):
    #   NSD below 0.10 near (5 px ≈ 50 m) bare sediment (soil with NDVI < 0.20).
    NSD_SUBSTRATE_SOIL: float = 0.10
    NDVI_BARE_SEDIMENT: float = 0.20
    NSD_SEDIMENT_DIST_PX: int = 5

    # WHY: integer raster codes for the classification map; fixed once so
    #   outputs stay comparable across runs and steps.
    # HOW: fixed enumeration per project spec (0 nodata, 1 water,
    #   2 dense hyacinth, 3 interface, 4 bare soil).
    CLASS_NODATA: int = 0
    CLASS_WATER: int = 1
    CLASS_DENSE_HYACINTH: int = 2
    CLASS_INTERFACE: int = 3
    CLASS_BARE_SOIL: int = 4
    # WHY: code for hyacinth removed by the exposed-bed correction, so the
    #   published map agrees with the corrected figures. Exists ONLY in the
    #   map `smolf publish` writes; the classifier never emits it and its own
    #   class map and GeoTIFF stay 0-4.
    # HOW: next free code after the classifier's fixed enumeration.
    CLASS_BED_REMOVED: int = 5

    # WHY: display palette for the classified map — single source of truth
    #   for the GeoTIFF color table, the demo UI, and charts, so every
    #   surface shows the same class colors. Hex RGB (CSS-style), except
    #   nodata, which carries an alpha channel (fully transparent).
    # HOW: fixed palette per project spec.
    COLOR_NODATA: str = "#00000000"
    COLOR_WATER: str = "#1d2e42"
    COLOR_DENSE_HYACINTH: str = "#15c912"
    COLOR_INTERFACE: str = "#8ed89d"
    COLOR_BARE_SOIL: str = "#a0783c"
    # WHY: published-map colour for class 5 (removed by exposed-bed
    #   correction): hyacinth-coloured pixels that do NOT count must not read
    #   as hyacinth, water or soil.
    # HOW: picked by eye as a muted red-orange, far in hue from the greens,
    #   the dark blue and the soil brown above.
    COLOR_BED_REMOVED: str = "#d0573a"

    # WHY: the published Sentinel-2 true-colour images share one fixed
    #   window, reflectance TRUE_COLOUR_MIN..TRUE_COLOUR_MAX to 0..1, so
    #   images of different dates and years are visually comparable (a
    #   per-image stretch would make a clear and a hazy scene look alike).
    # HOW: 0.30 sits above the red/green/blue reflectance of the reservoir's
    #   water, hyacinth, crops and bare soil, so only clouds and bright
    #   roofs saturate; 0 keeps dark water dark instead of lifting it grey.
    TRUE_COLOUR_MIN: float = 0.0
    TRUE_COLOUR_MAX: float = 0.30
    # WHY: the reservoir's reflectance is low (0.015-0.045 median), so the
    #   linear window alone renders it at 6-9 % brightness: water near black,
    #   hyacinth dark green. x ** (1/gamma) after the window lifts it.
    #   True colour only; never applied to a NIR/SWIR composite.
    # HOW: 1, 1/1.4, 1/1.6 and 1/2.2 compared on the four featured November
    #   photos (experiments/truecolour_gamma.md): 1.6 shows the mat and its
    #   edges at 18-21 % while water stays dark; 2.2 (smol's value) turns
    #   clean-water years milky grey.
    TRUE_COLOUR_GAMMA: float = 1.6
    # WHY: a photo pixel is no data, not dark water, when every band is at or
    #   below this reflectance; it is published transparent, not black.
    # HOW: smol's export_rgb alpha rule (issue #60); valid reflectance after
    #   the BOA offset sits above -0.1, so real pixels never reach it.
    TRUE_COLOUR_VALID_MIN: float = -0.15

    def traffic_light(self, pct: float) -> str:
        """Classify reservoir-area percent into green/yellow/red bands."""
        if pct < self.GREEN_MAX_PCT:
            return "green"
        if pct < self.RED_MIN_PCT:
            return "yellow"
        return "red"


settings = Settings()
