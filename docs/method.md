# Method

How smol-frontier turns Sentinel-2 scenes into an auditable water-hyacinth
time series for Endhó Reservoir. One section per pipeline stage, in order;
each names the thresholds it uses by config name and value, and why.
Every number below comes from `config.py`, a test, or a gate/series output
in this repo (cited in each section). Prior research is referenced through
`PRIOR_WORK.md` only. The water-level source is the SIH public record.

## 1. STAC search (`sources/stac.py`)

One interface, two catalogs: each search tries Planetary Computer first and
falls back to Earth Search on timeout or HTTP error, logging which catalog
served the result. Reference scenes are re-ingested by exact Earth Search
item id; the series `--extend` mode searches Earth Search for dates after
the last reference date. Same-date duplicates keep the highest
`s2:processing_baseline` per date and tile (`deduplicate()`); every
reference scene is tile 14QMH, so this is one scene per date with the
latest processing.

When every catalog fails, `smolf classify` and `smolf fetch` print one
`error: …` line on stderr and exit 2 (retry later, nothing written), the
same contract as `series build --extend` (§9).

- `MAX_CLOUD_COVER_CATALOG = 50.0` (config.py): catalog cloud-cover ceiling
  for newly searched scenes. Generous on purpose — the per-pixel SCL mask
  and the spectral cloud test filter clouds later anyway.
- Whole-tile `eo:cloud_cover` is recorded per scene (`tile_cloud_pct`) but
  never gates a scene: the reservoir's own cloud fraction decides quality.

Sources: config.py; `tests/test_stac.py`; `tests/reference/`.

## 2. Ingest and cache (`ingest.py`, `smolf fetch`)

Reads only the reservoir window from each band COG (never the full tile),
resamples the 20 m bands onto the 10 m B04 grid (bilinear for reflectance,
nearest neighbour for SCL), and zeroes pixels outside the reservoir
polygon. The cache stores raw DN plus SCL and metadata tags; the
radiometric offset is re-derived at read time, so a radiometry fix never
requires re-downloading. One offset per scene, applied to every band.

No thresholds. Cache hit and miss are asserted equivalent in
`tests/test_ingest.py`.

## 3. Radiometry (`sources/radiometry.py`)

The only place DN becomes reflectance: `reflectance = DN / 10000 + offset`.
The offset undoes the +0.1 step that processing baseline 04.00 (Jan 2022)
added to the bands.

- `DN_SCALE = 10000.0`: fixed L2A quantification.
- `BOA_OFFSET_VALUE = -0.1`, applied only when the baseline is at least
  `BASELINE_WITH_OFFSET = (4, 0)` and the scene flag says the offset is
  still present. The rule never trusts embedded raster offsets (one catalog
  returns none there) and never corrects unknown or pre-04.00 baselines.
- `NEGATIVE_FRAC_LIMIT = 0.5`: data-driven guard — if the offset would push
  more than half of valid B02 pixels negative, the flag is judged wrong and
  0.0 is used with a warning.
- Nodata (DN ≤ 0) stays nodata (NaN).

Sources: `sources/radiometry.py` constants; `tests/test_radiometry.py`.

## 4. Validity masks and indices (`classify/masks.py`, `classify/indices.py`)

A pixel is valid when it is inside the reservoir, its SCL class is not
disqualifying, and it is not spectral cloud:

- `SCL_INVALID = {0, 1, 3, 8, 9, 10}` (nodata, saturated, cloud shadow,
  medium/high-probability cloud, thin cirrus). Snow (11) is kept — never
  observed over Endhó — and unclassified (7) is kept because the spectral
  test covers that gap.
- Spectral cloud test, independent of SCL: B02 above `CLOUD_B02_MIN = 0.07`
  and B11 above `CLOUD_B11_MIN = 0.10` and B02 above
  `CLOUD_WHITENESS = 0.85` times B04. Why: since baseline 04.00 the SCL
  misses low clouds and haze; the B11 guard protects turbid water (which
  absorbs SWIR) and the whiteness guard protects reddish sediment.
  Regression scene: 2025-07-29, where missed haze once passed as valid
  with a fully false hyacinth cover (prior case documented in
  `classify/masks.py`) — now light POOR (measured: valid 0.5462, cloud
  0.4538, `tests/test_regression_scenes.py`).
- The cloud fraction counts SCL classes 3/8/9/10 (shadow counts as cloud)
  plus spectral cloud.

Indices (safe division, NaN-propagating): NDVI, NDRE, MNDWI, BSI,
NSD = B08 − B11 (canopy-over-water annihilates SWIR, so high NSD means
water under vegetation), and FAI after Hu (2009) with B08 (833 nm) as the
NIR band.

Sources: `classify/masks.py` constants; `tests/test_masks.py`;
`tests/test_regression_scenes.py` (2025-07-29).

## 5. Pre-masks, two-stage GMM, rescue, substrate (`classify/gmm.py`)

- Stage 0, soil — only inside the `BORDER_ZONE_M = 150.0` m shore strip, so
  soil never invades open water: BSI above `SOIL_BSI_MIN = 0.02`, MNDWI
  below `SOIL_MNDWI_MAX = -0.12`, NDVI below `SOIL_NDVI_MAX = 0.25`, NDRE
  below `SOIL_NDRE_MAX = 0.25`, NSD below `SOIL_NSD_MAX = 0.15`.
- Stage 0b, water — whole scene, no GMM needed: B08 below
  `NIR_WATER_MAX = 0.12` with NSD below `NSD_VEG_MIN = 0.15` (the NSD guard
  keeps low-NIR vegetation out of water).
- Turbidity pre-screen: when the GMM set has no dark mode, the NIR water
  threshold widens to the B08 histogram valley (and past it, only when that
  moves at least one pixel to water), recording `nir_water_frac`.
- Stage 1: two-component full-covariance GMM on FAI × NDVI (seed 42, up to
  50 000 sampled pixels). Water is the lower-mean-FAI component; a pixel is
  water at posterior `TAU_WATER = 0.80` or above, else mat-pending. Scenes
  with no mat short-circuit to all water. On poor separation the stage
  retries once with an adaptive NIR threshold — the same `SEP_DOUBTFUL`
  cutoff that flags the scene doubtful (§7), so the retry rule and the
  quality flag cannot drift apart.
- Stage 2: two-component GMM on NDVI × NDRE over the mat pixels; dense is
  the higher-mean-NDVI component at posterior `TAU_HYACINTH = 0.55` or
  above, else interface (mat edge / sparse cover).
- Rescue (water → interface): water pixels with NDVI above
  `NDVI_RESCUE_WATER = 0.30` and B08 above `NIR_RESCUE_WATER = 0.15` are mat
  edge the GMM misplaced — turbid water cannot produce both. This recovers
  12–35 % of hyacinth at high coverage and 0 ha in clean water.
- Substrate test, last word: hyacinth pixels with NSD below
  `NSD_SUBSTRATE_SOIL = 0.10` within `NSD_SEDIMENT_DIST_PX = 5` px (~50 m)
  of bare sediment (soil with NDVI below `NDVI_BARE_SEDIMENT = 0.20`) are
  dry-season shoreline vegetation on exposed sediment, not floating
  hyacinth, and become soil. With no sediment anchor (full reservoir)
  nothing changes.
- Class codes `CLASS_NODATA/WATER/DENSE_HYACINTH/INTERFACE/BARE_SOIL` =
  0/1/2/3/4 (config.py).

Sources: config.py; `classify/gmm.py` budget constants (seed 42, 50 000
pixel cap); `tests/test_gmm_stage1.py`; `tests/test_classify_stage2.py`.

## 6. `classify()` (`classify/pipeline.py`, `smolf classify`)

Runs §4–§5 in fixed order and converts pixels to hectares at
`PIXEL_AREA_M2 = 100.0` (the 10 m grid). The public percentage divides by
the reservoir area computed from the polygon in `aoi.py` — 1261.75 ha,
checked by `tests/test_aoi.py` and `tests/test_regression_scenes.py` — and
is the only percentage with that denominator. A classified GeoTIFF is
written with the fixed `COLOR_*` palette (config.py).

## 7. Quality light and regime (`quality.py`)

Three degrading criteria make a scene DOUBTFUL: valid fraction below
`FRAC_DOUBTFUL = 0.85` (PARTIAL_CLOUDS), cloud fraction above
`CLOUD_FRAC_DOUBTFUL = 0.10` (HIGH_CLOUD), Stage-1 separability below
`SEP_DOUBTFUL = 1.5` or NaN (GMM_WATER_OVERLAP; +inf means cleanly
mat-free). Only the valid fraction can make a scene POOR: below
`MIN_VALID_FRACTION = 0.80`. The percentage is publishable only at valid
fraction `FRAC_PUBLISHABLE = 0.95` or above. Informational flags (never
degrading): NO_MAT, ADAPTIVE_NIR, HIGH_TURBIDITY (above
`TURBIDITY_HIGH_FRAC = 0.70`), INTERFACE_DOMINANT.

The regime is reported in its own field and never degrades quality:
EXPOSED_BED when soil exceeds `REGIME_SOIL_MAX_HA = 50.0` ha (strictly
greater), else TURBIDITY when `nir_water_frac` reaches
`REGIME_NIR_WATER_MIN = 0.60`, else clean. Management bands on the public
percentage: green below `GREEN_MAX_PCT = 30.0`, yellow to
`RED_MIN_PCT = 50.0`, red at or above.

Sources: config.py; `tests/test_quality.py`.

## 8. Exposed-bed correction (`hydro/`, `smolf levels`, `smolf hydro`)

In the dry season, vegetation on the exposed bed classifies as hyacinth.
The correction crosses the class map with a per-pixel flood-stage map and
the day's SIH level: hyacinth above the level plus an uncertainty margin is
removed; hyacinth in the mirror or the margin band keeps counting. The
class map itself is never changed — only the reported figures.

- Levels: the committed SIH copy (`data/hydro/ENDHD.csv`), or the refreshed
  copy under `SMOLF_DATA_DIR` once it exists, exact-day lookup, never
  interpolated. Refreshes rejecting level revisions above
  `LEVEL_REVISION_TOL_M = 0.05` m unless forced; the first refresh into a
  data folder is validated against the committed copy.
- Stage–area curve (`data/hydro/stage_area_curve.csv`): SIH pairs up to
  `CURVE_DATA_END = 2026-09-20`, `CURVE_BIN_M = 0.10` m bins with at least
  `CURVE_BIN_MIN_N = 5` days, fit up to `CURVE_FIT_H_MAX = 2017.5` m,
  official anchors `CURVE_ANCHORS = (2003.16, 52.1), (2018.0, 182.0),
  (2020.0, 208.0)` weighted `CURVE_W_ANCHOR = 50.0`, bootstrap
  `CURVE_BOOTSTRAP_B = 300` at seed `CURVE_BOOTSTRAP_SEED = 42`, grid top
  `CURVE_GRID_TOP = 2018.5` m.
- Flood-stage map (`data/hydro/flood_stage.tif`): JRC surface-water
  occurrence matched to the curve on the 2026-10-12 classifier grid
  (`PIXEL_HA = 0.01`).
- Cushion: the margin adds the larger of the model-error leg
  (`BED_CUSHION_MODEL_HA`: 29/41/25/66 ha at 2005/2010/2015/2018 m) and the
  bootstrap half-width, so shoreline hyacinth survives curve error. Missing
  level, missing files, or a grid mismatch leave the raw figures standing
  with a recorded reason.

Sources: config.py; `hydro/flood_map.py` (`PIXEL_HA = 0.01`);
`tests/test_levels.py`, `tests/test_curve.py`, `tests/test_flood_map.py`,
`tests/test_bed.py`; `tests/test_regression_scenes.py` (2024-06-09: raw
432.25 ha → 13.74 ha).

## 9. Series builder and manifest (`series.py`, `smolf series build`)

Runs §6–§8 per scene over the 465 reference rows by exact item id
(Planetary Computer fallback per scene, shared with the gate), or
additionally past the last reference date with `--extend`. POOR scenes are
recorded as skipped and never enter `series.csv`; failed scenes are
recorded as errors and never stop the run. Each run writes
`outputs/series/<run_id>/` with one JSON per finished scene (written at
once), `series.csv` (sorted, fixed formatting), and `manifest.json`: code
fingerprint (git commit, dirty flag, config hash), package and Python
versions, `uv.lock` hash, input-file hashes, per-scene entries, counts, and
the `series.csv` sha256 — a series is identified by that hash, not by
date. `--resume` reuses finished scenes only when the fingerprint matches,
else it refuses with exit 1 (not retryable: start a new run).

The config hash is the sha256 of the canonical settings dump (every
threshold and parameter in `config.py`), excluding input-file locations
(`AOI_PATH`). Input contents are pinned separately by sha256 in the
manifest's `inputs`, so moving the checkout does not block `--resume`.

`series build` exit codes:

- 0: the run completed.
- 2: precondition refusal, nothing written, retry later. One `error: …` line
  on stderr. Today: stale SIH level record (a target day is newer than the
  last level day) and new-scene catalog unavailable (`--extend` and Earth
  Search is unreachable). Its own argument checks (`--workers < 1`,
  `--extend` with `--resume`) and Click/Typer usage errors also exit 2.
- 1: anything else, including a resume fingerprint mismatch (the code,
  config or dirty flag changed since the run started, so a retry cannot
  succeed) and an unknown or existing run id.
- 130: interrupted. Once the run directory exists, stderr names the
  `--resume` command; before that, nothing was written.

Errors and refusals go to stderr; stdout carries only results. The run id
(`run: <id> (new|resume)`) is printed once its directory exists, so a
refusal never names a run.

Measured: run `20261028T221314` — 465 requested, 465 ok, 0 skipped,
0 errors, wall 1:35, `series.csv` sha256
`87514b60…` (full hash in the run manifest).

Sources: `tests/test_series.py`; `outputs/series/20261028T221314/`.

## 10. Identity gate (`gate.py`, `smolf gate identity`)

Re-ingests the reference scenes and compares raw classifier output (no bed
correction) against the author's prior series. Pass needs median |Δ total|
at most `PASS_MEDIAN_HA = 10.0` ha and p95 at most `PASS_P95_HA = 25.0` ha
on same-item scenes, with zero failures.

Measured (`outputs/identity_gate/gate_20261028T212616.md`, `.csv`): 465
scenes, median |Δ| 0.01 ha, p95 0.11 ha, max 0.43 ha (2023-10-28
`S2B_14QMH_20231028_0_L2A`), light and regime agreement 465/465, 0
fallback, 0 errors — verdict PASS. 206 scenes sit at Δ 0.00 ha. The series
builder reproduces the gate's raw figures exactly (max |Δ| 0.00 ha, light
and regime 465/465 against that gate CSV).

## 11. Publish contract (`publish/`, `smolf publish`)

Derives a versioned data contract from one series run, named with `--run
<id>` or chosen with `--latest` (the newest run, by run id, whose manifest
is readable and whose status is complete; unreadable manifests are skipped;
neither or both exits 2) into `outputs/publish/` (or
`$SMOLF_DATA_DIR/outputs/publish/`; `--out` overrides): `latest.json` (the
last `ok` scene plus run context, constants, the reservoir outline as an
RFC 7946 MultiPolygon and the current SIH level),
`series.json` (every `ok` and POOR scene; errors excluded),
`map/<date>_<item_id>.tif` and `.png` for the
latest scene, and the integrity index `publish.json`. Field by field:
`docs/contract.md`; schemas: `src/smol_frontier/schemas/`
(`SCHEMA_VERSION = "3.0.0"`, written exactly; each schema accepts any
`3.x.y`, and only a new optional field is a minor change — see the
versioning rules in `docs/contract.md`). Scene records are sorted by date,
then requested item id. Figures are copied from the run rows and rounded
like `series.csv` (2 decimals; fractions 4); nothing is filtered. Each
scene also carries `display_status` (`publish.aggregates.display_status()`
on the rounded entry): `HIDDEN` when not GOOD or without a corrected
figure, `LOWER_BOUND` when `valid_frac` < `FRAC_PUBLISHABLE`, else `FULL`.
`cover_band` is `settings.traffic_light()` (`GREEN_MAX_PCT = 30.0`,
`RED_MIN_PCT = 50.0`) on the 2-decimal `pct_reservoir_corrected`, and
`null` with every corrected figure when the bed correction was not
applied.

Runs do not persist class maps, so the latest scene is re-classified from
the scene cache after its sha256 matches the record's `cache_sha256` (a
missing scene is re-ingested by exact item id), and its per-class hectares
must equal the run row to the hundredth. The published map adds class
`CLASS_BED_REMOVED = 5` (`COLOR_BED_REMOVED`) through the pure
`exposed_bed_mask()` in `hydro/bed.py`, using the row's `level_m` (no fresh
level lookup) and the margin **recomputed** from that level with the
committed stage–area curve — the run's own code path (`bed_margin()`).
The row's `margin_m` is rounded to 2 decimals and cannot reproduce the
run's removal: pixels whose stage lies between the rounded and the
full-precision margin would flip. Publish therefore refuses (exit 2) when
the curve's or the flood-stage map's sha256 differs from the run
manifest's inputs (verified only when the correction was applied to the
latest scene: without it no class 5 exists and neither input touches the
published figures), when the recomputed margin does not round to the row's
`margin_m`, or when class-5 pixels × `PIXEL_HA = 0.01` differ from
`removed_ha` at the hundredth. Other refusals (exit 2): incomplete or dirty
run, unreadable manifest of an explicit `--run` (`--latest` selection skips
it), no `ok` scene, cache hash mismatch, hectare mismatch, and an `--out`
directory that is non-empty but not a previous publish (refused without
walking its tree), a broken symlink in the `--out` path (unless it is the
final component and a lone `.<target>.previous-*` can be restored), or several
leftover `.previous-*` directories. A schema or raster-guard failure of
smolf's own output is a bug (exit 1). Rasters go only through the guarded
writers (uint8, values 0–5), files only through the allowlist, and the
directory is swapped in by rename after validation. `--out` is resolved
to its real path first; at the start of a publish stale
`.<name>.staging-*` directories holding only allowlisted files are
removed, a lone `.<name>.previous-*` is restored when `--out` is
missing, and old previous directories are removed once `--out` is a
valid contract, each only if it is itself a valid contract.

Measured: `smolf publish --run 20261030T031055` → exit 0, `latest.json`
2026-10-12, `hyacinth_corrected_ha` 818.34, `pct_reservoir_corrected`
64.86, GOOD, CLEAN, RED, `smolf_version` 0.3.0, `schema_version` 2.0.0.
The 2024-06-09 control (Planetary Computer copy) publishes raw 432.25 ha →
corrected 13.74 ha, `removed_ha` 418.51, EXPOSED_BED, with 41 851 class-5
pixels (418.51 ha).

Sources: `tests/test_publish.py` (fixture run end to end, refusals,
schema negatives, guard, class-5 mask); `tests/test_publish_regression.py`
(cache-gated control scenes); `tests/test_contract_doc.py`.
