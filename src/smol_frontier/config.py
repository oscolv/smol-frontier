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

    # WHY: GMM posterior cutoff above which a pixel counts as water at all;
    #   separates the reservoir surface from land before hyacinth mapping.
    # HOW: calibrated on the author's prior research (Oct 2026, 465
    #   Sentinel-2 scenes 2017–2026): varying TAU_WATER over 0.50–0.99 moves
    #   total hyacinth area by at most 7 ha, so 0.80 is a stable choice.
    TAU_WATER: float = 0.80

    # WHY: GMM posterior cutoff above which water counts as dense hyacinth
    #   rather than open water / interface; splits the water mask by cover.
    # HOW: same calibration as TAU_WATER (Oct 2026, 465 scenes): moving
    #   TAU_HYACINTH only reallocates pixels between dense and interface
    #   classes without changing the water mask, so 0.55 is a stable split.
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
    #   with exposed bed, Sentinel-2 underestimates hyacinth versus same-day
    #   PlanetScope (P(|PS − S2| > 100 ha) = 0.62 with exposed bed vs 0.22
    #   without; 125 pairs ±1 day), and no separability/confidence metric
    #   predicted that gap. So the regime is reported in its own field and
    #   does NOT degrade quality.
    # HOW: fixed cutoff from the author's prior research.
    REGIME_SOIL_MAX_HA: float = 50.0

    # WHY: nir_water_frac at or above which the scene is in the TURBIDITY
    #   regime — with turbidity, Sentinel-2 underestimates versus same-day
    #   PlanetScope (P(|PS − S2| > 100 ha) = 0.91 with turbidity vs 0.25
    #   without; same 125 pairs). Reported in its own field, never degrades
    #   quality.
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

    def traffic_light(self, pct: float) -> str:
        """Classify reservoir-area percent into green/yellow/red bands."""
        if pct < self.GREEN_MAX_PCT:
            return "green"
        if pct < self.RED_MIN_PCT:
            return "yellow"
        return "red"


settings = Settings()
