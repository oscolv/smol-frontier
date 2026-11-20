# Publish contract (`schema_version` 3.3.0)

What `smolf publish` writes and what a downstream consumer may rely on.
The JSON Schemas packaged with smolf (`src/smol_frontier/schemas/`:
`latest.schema.json`, `series.schema.json`, `publish.schema.json`, JSON
Schema draft 2020-12) are the source of truth; this document explains
them field by field. `tests/test_contract_doc.py` fails if a schema
property is missing here.

First release containing this contract: smolf 0.8.0 (tag `v0.8.0`).
Changes from earlier versions are listed under "Versioning".

## Principles

- **Neutral.** smolf publishes every figure as computed. It never hides,
  filters, smooths or substitutes a figure. A DOUBTFUL scene, a scene whose
  `valid_frac` is below `frac_publishable`, or a large jump between scenes
  is published like any other; the downstream consumer applies its own
  display gates using the quality light, the visible fraction, the dates,
  the series history and the published constants. The only scenes without
  figures are POOR scenes, which the pipeline already skips upstream.
  Since 3.0.0 smolf also labels each scene with the deterministic part of
  that gate (`display_status`); the label hides nothing, and the
  consumer still decides what to show.
- **No recomputation downstream.** Every number a consumer displays
  (percentages, cover band, reservoir area and outline, thresholds) is in
  the contract, and so is the deterministic quality gate
  (`display_status`), so figures derived from "the displayed scenes" agree
  with what the consumer shows.
- **Provenance, not contract.** The series run's `manifest.json` is
  internal provenance; it is never copied out. Each document carries
  `run_id` and `series_sha256` so any figure traces back to the exact run.
- **Derived products, plus Sentinel-2 photos.** Both map writers
  (GeoTIFF and PNG) accept a uint8 class array with values 0–5 and nothing
  else (the PNG writer colours the classes itself), and only allowlisted
  files are ever written. The one exception is a separate, guarded
  true-colour writer that accepts only uint8 RGBA from STAC collection
  `sentinel-2-l2a`, published under the Copernicus licence with
  attribution (see "True-colour image"). PlanetScope imagery and raw
  reflectance of any sensor cannot be published.

## Directory layout

The output directory (default `outputs/publish/`, or
`$SMOLF_DATA_DIR/outputs/publish/` when `SMOLF_DATA_DIR` is set; `--out`
always wins) contains exactly:

```text
latest.json
series.json
publish.json
map/<date>_<item_id>.tif
map/<date>_<item_id>.png
image/<date>_<item_id>.webp
image/clipped/<date>_<item_id>.webp   (one per same_month year with a scene)
```

The two map files and `image/<date>_<item_id>.webp` are those of the
scene described by `latest.json`; the clipped photos are those of the
`same_month` scenes. Files of earlier publishes are not kept. File names
change with the scene, so a cache can never pair a new `latest.json` with
an old map or image.

## Conventions

- Field names are English and reuse the `series.csv` column names; units
  are in the names: `_ha` hectares, `_m` metres above sea level (water
  stage), `pct_` percent (0–100), `_frac` fraction (0–1).
- Rounding: hectares, percentages and water levels to 2 decimals;
  fractions to 4 decimals; outline coordinates (degrees) to 6 decimals.
- Dates: UTC acquisition date, ISO `YYYY-MM-DD`; no acquisition time in
  3.0.0.
- `null` is explicit: a field defined by the schema is always present,
  with `null` where the value does not exist (never omitted).
- Additional properties are allowed in every object, so a consumer pinned
  to 3.0.0 keeps validating files from a later additive (minor) release.

## Spanish → English mapping

The prior research and the SIH data use Spanish terms; the contract uses
these English names.

| Source term | Contract |
| --- | --- |
| `porc_lirio_vaso` (percent of reservoir covered) | `pct_reservoir_corrected` (and `pct_reservoir_raw`) |
| `frac_valida` (valid fraction) | `valid_frac` |
| semáforo de calidad BUENA / DUDOSA / MALA | `quality_light` GOOD / DOUBTFUL / POOR |
| semáforo de manejo (management traffic light) | `cover_band` GREEN / YELLOW / RED |
| "ajustado por nivel" (adjusted for water level) | `*_corrected` |
| régimen limpio / LECHO / TURBIDEZ | `regime` CLEAN / EXPOSED_BED / TURBIDITY |
| lirio denso / interfase | `dense_*` / `interface_*` |
| `lirio_bajo_neblina_ha` (hyacinth under haze) | `hyacinth_under_haze_ha` |
| cota (water-surface stage) | `level_m` |
| vaso (reservoir basin polygon) | `reservoir_outline` (its area: `reservoir_area_ha`) |

The run's generic `light` column is published as `quality_light`; its
empty/`clean` regime value is published as `CLEAN`.

## Per-scene fields

Shared by `latest.json` (top level) and every entry of `series.json`
(`scenes`). "Corrected" means after the exposed-bed correction: hyacinth
pixels lying above the day's water level plus an uncertainty margin are
vegetation on the exposed bed, not floating hyacinth, and are removed.

| Field | Type | Unit | Null when | Meaning |
| --- | --- | --- | --- | --- |
| `date` | string `YYYY-MM-DD` | — | never | UTC acquisition date. |
| `item_id` | string | — | never | Exact STAC item id of the scene used. |
| `quality_light` | enum `GOOD`, `DOUBTFUL`, `POOR` | — | never | Scene quality light. `latest.json` is always GOOD or DOUBTFUL (its schema rejects POOR). |
| `regime` | enum `CLEAN`, `EXPOSED_BED`, `TURBIDITY` | — | POOR scene | Optical regime of the scene. |
| `valid_frac` | number 0–1 | fraction | never | Visible (cloud-free, observed) fraction of the reservoir. |
| `cloud_frac` | number 0–1 | fraction | POOR scene | Clouded fraction of the reservoir. |
| `dense_ha` | number ≥ 0 | ha | POOR scene | Dense hyacinth, raw. |
| `interface_ha` | number ≥ 0 | ha | POOR scene | Interface hyacinth (mat edge / sparse cover), raw. |
| `hyacinth_raw_ha` | number ≥ 0 | ha | POOR scene | Dense + interface, before the exposed-bed correction. |
| `hyacinth_under_haze_ha` | number ≥ 0 | ha | POOR scene | Part of `hyacinth_raw_ha` (dense + interface) seen through thin haze inside the day's water mirror and rescued by the classifier's frozen second pass; raw, not exposed-bed corrected. `0` when nothing was rescued, and whenever the exposed-bed correction was not applied (no mirror: no SIH level for the day, flood-stage map or stage–area curve missing, or grid mismatch). Since 3.3.0. |
| `pct_reservoir_raw` | number 0–100 | % | POOR scene | `hyacinth_raw_ha` over `reservoir_area_ha`. |
| `bed_applied` | boolean | — | POOR scene | Whether the exposed-bed correction was applied. |
| `bed_reason` | string | — | POOR scene | Empty when applied; otherwise why not (e.g. no SIH level for the day). |
| `level_m` | number | m a.s.l. | POOR scene, or no level for the day | Water level of the scene day used by the correction. |
| `removed_ha` | number ≥ 0 | ha | POOR scene, or `bed_applied` false | Hyacinth removed by the correction (map class 5). |
| `dense_corrected_ha` | number ≥ 0 | ha | POOR scene, or `bed_applied` false | Dense hyacinth after the correction. |
| `interface_corrected_ha` | number ≥ 0 | ha | POOR scene, or `bed_applied` false | Interface hyacinth after the correction. |
| `hyacinth_corrected_ha` | number ≥ 0 | ha | POOR scene, or `bed_applied` false | Total hyacinth after the correction; the public figure. |
| `pct_reservoir_corrected` | number 0–100 | % | POOR scene, or `bed_applied` false | `hyacinth_corrected_ha` over `reservoir_area_ha`; the public percentage. |
| `cover_band` | enum `GREEN`, `YELLOW`, `RED` | — | POOR scene, or `bed_applied` false | Management band of the published (2-decimal) `pct_reservoir_corrected`: GREEN below `green_max_pct` (30), YELLOW from 30 to below `red_min_pct` (50), RED at or above 50. Never "50.00 % YELLOW". |
| `display_status` | enum `FULL`, `LOWER_BOUND`, `HIDDEN` | — | never | Display gate of the scene (see below). |
| `display_ha` | object | ha | POOR scene, or `bed_applied` false | Whole-hectare figures for display, whose parts sum to the total (see below). |
| `display_ha.total` | integer ≥ 0 | ha | no | `hyacinth_corrected_ha` rounded half up. |
| `display_ha.dense` | integer ≥ 0 | ha | no | `dense_corrected_ha` apportioned to whole hectares. |
| `display_ha.interface` | integer ≥ 0 | ha | no | `interface_corrected_ha` apportioned to whole hectares; `dense + interface == total`. |

When `bed_applied` is false, `removed_ha`, every `*_corrected` field,
`cover_band` and `display_ha` are `null` and `bed_reason` says why; smolf never substitutes the raw
figure. When it is true, they are all non-null. Both schemas define every
non-POOR scene identically (`tests/test_publish.py` keeps them in step). A
POOR entry in `series.json` carries `date`, `item_id`, `quality_light`
(POOR), `valid_frac` and `display_status` (`HIDDEN`), and `null` for every
other field (`display_ha` included).

### `display_status`

Since 3.0.0 smolf publishes the deterministic part of the consumer's
quality gate, so every figure derived from "the scenes a consumer shows"
uses one rule:

- `HIDDEN`: `quality_light` is not GOOD (DOUBTFUL and POOR scenes), or
  `hyacinth_corrected_ha` is `null` (no exposed-bed correction). The scene
  feeds no displayed figure.
- `LOWER_BOUND`: otherwise, when the published `valid_frac` is below
  `constants.frac_publishable`. Part of the reservoir was not visible, so
  the hectares read "at least" and the percentage is not a full figure.
- `FULL`: every other scene.

The rule reads the published (rounded) values, so a consumer can check it
against the documents. It covers the quality gate only: editorial
decisions a consumer takes on its own (holding a scene after a large jump,
withdrawing one by hand) are not known to smolf and are not reflected.
Every figure is still published as computed (see Principles).

### `display_ha`

Since 3.0.0 every scene with corrected figures also carries them as whole
hectares, so a consumer can print "N ha: D dense and I interface" with no
arithmetic and the sentence always adds up. smolf computes them from the
published (2-decimal) figures:

- `total` is `hyacinth_corrected_ha` rounded half up (818.50 → 819, never
  to even).
- `dense` and `interface` start as the floors of `dense_corrected_ha` and
  `interface_corrected_ha`. The units still missing to reach `total` go
  one each to the part with the larger fractional remainder; on equal
  remainders dense takes it.

Example: 630.56 = 399.88 + 230.68 gives `total` 631; the floors 399 + 230
leave 2 units, one to each part: 400 + 231. The 2-decimal fields are
unchanged and remain the open-data figures; `display_ha` is for display
only.

## `latest.json`

The latest scene of the run that has figures (the last `ok` scene, GOOD
or DOUBTFUL), with every per-scene field above at the top level, plus:

| Field | Type | Unit | Null | Meaning |
| --- | --- | --- | --- | --- |
| `schema_version` | string `3.x.y` | — | no | Contract version; smolf writes exactly `3.3.0`, the schema accepts any `3.x.y` (no trailing newline). |
| `smolf_version` | string | — | no | smolf version that wrote the file (`0.8.0` for this release). |
| `newest_acquisition` | object | — | no | Newest scene of the run of any quality (never an error scene). It may be POOR and newer than the scene above: "a newer scene exists but was unusable". |
| `newest_acquisition.date` | string `YYYY-MM-DD` | — | no | Its acquisition date. |
| `newest_acquisition.item_id` | string | — | no | Its STAC item id. |
| `newest_acquisition.quality_light` | enum `GOOD`, `DOUBTFUL`, `POOR` | — | no | Its quality light. |
| `newest_acquisition.valid_frac` | number 0–1 | fraction | no | Its visible fraction. |
| `run_counts` | object | — | no | Scene counts of the run, for transparency. |
| `run_counts.ok` | integer ≥ 0 | scenes | no | Scenes with figures. |
| `run_counts.skipped` | integer ≥ 0 | scenes | no | POOR scenes (no figures). |
| `run_counts.errors` | integer ≥ 0 | scenes | no | Process failures; excluded from `series.json`. |
| `run_id` | string | — | no | Series run the contract was derived from. |
| `series_sha256` | string, 64 hex | — | no | sha256 of the run's `series.csv`; identifies the series. |
| `reservoir_area_ha` | number > 0 | ha | no | Denominator of every percentage, computed from the reservoir polygon (1261.75 ha). |
| `reservoir_outline` | object | — | no | The reservoir polygon itself (JRC Global Surface Water v1.4, occurrence ≥ 25 %, 1984–2021; Pekel et al., 2016), as an RFC 7946 GeoJSON geometry, for drawing the outline on a web map. Since 2.1.0; required since 3.0.0. |
| `reservoir_outline.type` | string `MultiPolygon` | — | no | Always a MultiPolygon, even for a single part. |
| `reservoir_outline.coordinates` | array of polygons | degrees, EPSG:4326 | no | Each polygon is a list of closed linear rings (exterior first, then holes); each position is `[longitude, latitude]` to 6 decimals (~0.1 m). Exterior rings run counter-clockwise and holes clockwise (RFC 7946 right-hand rule). Reprojected to UTM it encloses `reservoir_area_ha` to within 0.01 ha. |
| `constants` | object | — | no | Thresholds a consumer needs for its own gates. |
| `constants.frac_publishable` | number 0–1 | fraction | no | Minimum `valid_frac` for a percentage to be fully trustworthy (0.95). smolf still publishes the percentage below it; the consumer decides (e.g. show hectares only, as a lower bound). |
| `constants.green_max_pct` | number 0–100 | % | no | Upper bound (exclusive) of GREEN (30.0). |
| `constants.red_min_pct` | number 0–100 | % | no | Lower bound (inclusive) of RED (50.0). |
| `constants.green_max_ha` | number ≥ 0 | ha | no | `green_max_pct` of `reservoir_area_ha`, rounded half up to 2 decimals (378.53), for drawing the level line on a hectare chart. Derived from the published area, never hard-coded. |
| `constants.red_min_ha` | number ≥ 0 | ha | no | `red_min_pct` of `reservoir_area_ha`, rounded half up to 2 decimals (630.88). Derived like `green_max_ha`. |
| `current_level` | object or `null` | — | yes | Last day with a level in the local SIH level record at publish time (the refreshed copy under `SMOLF_DATA_DIR` when it exists, else the committed one), independent of the scene and of the wall clock; `null` when the record has no level. |
| `current_level.date` | string `YYYY-MM-DD` | — | no | That day. |
| `current_level.level_m` | number | m a.s.l. | no | Its water level. |
| `current_level.levels_sha256` | string, 64 hex | — | no | sha256 of the level record file, to judge freshness and provenance. |
| `map` | object | — | no | The scene's map files. |
| `map.geotiff` | string | — | no | Relative path `map/<date>_<item_id>.tif`. |
| `map.png` | string | — | no | Relative path `map/<date>_<item_id>.png`. |
| `map.bounds` | `[[south, west], [north, east]]` | degrees, EPSG:4326 | no | Extent of the PNG, 6 decimals, for a web-map image overlay. |
| `image` | object | — | no | Sentinel-2 true-colour photo of the same scene (see "True-colour image"). Since 3.0.0. |
| `image.path` | string | — | no | Relative path `image/<date>_<item_id>.webp`. |
| `image.bounds` | `[[south, west], [north, east]]` | degrees, EPSG:4326 | no | Always equal to `map.bounds`: the photo is on exactly the `map.png` grid. |
| `image.attribution` | string | — | no | `Copernicus Sentinel data <year>`, the year of the scene; show it wherever the photo is shown (Copernicus licence). |
| `same_month` | object or `null` | — | yes | The same calendar month in every year of the series (see "Same month, every year"); `null` when the run has no `FULL` scene. Since 3.0.0. |
| `same_month.month` | integer 1–12 | — | no | Calendar month of the latest `FULL` scene of the run (not necessarily the scene above). |
| `same_month.years` | array of year objects | — | no | One per year, ascending, from the year of the first scene of `series.json` to the year of that `FULL` scene. |
| `same_month.years[].year` | integer | — | no | The year. |
| `same_month.years[].n_scenes` | integer ≥ 0 | scenes | no | That month's scenes that year with `display_status` `FULL` or `LOWER_BOUND`. |
| `same_month.years[].scene` | object or `null` | — | when `n_scenes` is 0 | The representative scene: the lower median of those scenes. |
| `same_month.years[].scene.item_id` | string | — | no | Its STAC item id (as in `series.json`). |
| `same_month.years[].scene.date` | string `YYYY-MM-DD` | — | no | Its acquisition date. |
| `same_month.years[].scene.hyacinth_corrected_ha` | number ≥ 0 | ha | no | Its published corrected hectares. |
| `same_month.years[].scene.display_status` | enum `FULL`, `LOWER_BOUND` | — | no | Its `display_status`; `LOWER_BOUND` reads "at least". |
| `same_month.years[].image` | object or `null` | — | when `n_scenes` is 0 | Its reservoir-clipped true-colour photo. |
| `same_month.years[].image.path` | string | — | no | Relative path `image/clipped/<date>_<item_id>.webp`. |
| `same_month.years[].image.attribution` | string | — | no | `Copernicus Sentinel data <year>`, the year of that scene. |
| `same_month.years[].featured` | boolean | — | no | `true` for the years a compact block shows: the current year and the first, middle and last earlier years (see "Featured years"); `false` otherwise. A featured year always has a `scene` and an `image`. Optional in the schema, always written by smolf. Since 3.1.0. |

The hectare cut-offs are for drawing; `cover_band`, computed from the
published percentage, stays authoritative. At the exact boundary the two
comparisons can disagree: 378.52 ha is below `green_max_ha` but publishes
as 30.00 %, so its `cover_band` is YELLOW (and 630.87 ha publishes as
50.00 %, RED).

### Same month, every year

`same_month` backs a year-over-year block that matches the homepage
figure: its month is the calendar month (UTC date) of the latest `FULL`
scene of the run. `latest.json`'s own scene is unchanged (the last `ok`
scene, which may be `LOWER_BOUND` or `HIDDEN`). Years run from the year
of the first scene in `series.json` (POOR scenes included) to the year of
that `FULL` scene, so every year of the series appears, with its photo,
for a consumer that draws the whole series; `featured` marks four of
them for a compact block.

For each year, the month's `FULL` and `LOWER_BOUND` scenes are ordered by
`hyacinth_corrected_ha` (exact published decimals), ties broken by date
then item id, and the ⌈n/2⌉-th is the representative (the lower median),
so the figure always belongs to a real scene with a real photo. A year
with no such scene carries `n_scenes: 0`, `scene: null` and
`image: null`. Each representative has a clipped photo (see
"True-colour image"); place it with `map.bounds`, since every photo is on
the `map.png` grid.

#### Featured years

Since 3.1.0 smolf marks the years a compact block shows (`featured:
true`), so a consumer does not re-implement a selection rule. The rule
reads only each year's `n_scenes`, never hectares, so the choice cannot
favour a story (ADR 0002):

- The current year, the last entry of `years`, is always featured; it
  always has a scene.
- The prior years are the calendar years before it, empty years
  included, in ascending order; let n be their count. The targets are
  index 0 (first), index n−1 (last prior) and index ⌊(n−1)/2⌋ (middle).
- They are resolved in that order: first, last prior, middle. A target
  that is empty (`n_scenes` 0) or already featured falls back: the first
  forward to the next prior year with a scene not yet featured; the last
  prior backward to the previous one (never onto the current year); the
  middle to the nearest prior year with a scene not yet featured, ties
  going to the earlier year. If no candidate remains, the slot stays
  empty.

So at most four years are featured, never one twice, and fewer than four
only when fewer than three prior years have a scene (then all of them
are featured). For a November series from 2017 to 2026 the featured
years are 2017, 2021, 2025 and 2026. The schema guarantees that a
featured year has a non-null `scene` and `image`; the bound of four is
not expressible in JSON Schema and is enforced by smolf's tests.

## `series.json`

| Field | Type | Null | Meaning |
| --- | --- | --- | --- |
| `schema_version` | string `3.x.y` | no | Contract version; smolf writes exactly `3.3.0`, the schema accepts any `3.x.y` (no trailing newline). |
| `smolf_version` | string | no | smolf version that wrote the file. |
| `run_id` | string | no | Series run. |
| `series_sha256` | string, 64 hex | no | sha256 of the run's `series.csv`. |
| `scenes` | array of per-scene objects | no | Every `ok` and every POOR (skipped) scene of the run, sorted by date then item id, with the per-scene fields above. Error scenes are excluded (see `run_counts.errors` in `latest.json`). |
| `monthly` | array of month objects | no | Median corrected hectares per calendar month, for the series chart (see below). Empty only when `scenes` is. |
| `monthly[].month` | string `YYYY-MM` | no | Calendar month (UTC dates). |
| `monthly[].median_ha` | number ≥ 0, ha | when `n_scenes` is 0 | Ordinary median of the month's non-HIDDEN `hyacinth_corrected_ha`. |
| `monthly[].n_scenes` | integer ≥ 0 | no | The month's scenes with `display_status` `FULL` or `LOWER_BOUND`. |

### `monthly`

One entry per calendar month, contiguous and in order, from the month of
the first scene to the month of the last; HIDDEN and POOR scenes count
toward that span but not toward any median. `median_ha` is the ordinary
median of the published (2-decimal) `hyacinth_corrected_ha` of the
month's `FULL` and `LOWER_BOUND` scenes: the middle value, or the mean of
the two middle values when the count is even, computed on exact decimals
and rounded half up to 2 decimals. A month with no such scene carries
`median_ha: null` and `n_scenes: 0`, so a chart breaks its line there
instead of inventing a value. `LOWER_BOUND` scenes enter the median at
face value; a consumer that wants to flag such months can count them in
`scenes`.

Quality flags (degrading/informational) are not part of 3.3.0.

## `publish.json`

The integrity index, written last. It is the only non-deterministic file
and never lists itself.

| Field | Type | Null | Meaning |
| --- | --- | --- | --- |
| `schema_version` | string `3.x.y` | no | Contract version; smolf writes exactly `3.3.0`, the schema accepts any `3.x.y` (no trailing newline). |
| `smolf_version` | string | no | smolf version that wrote the file. |
| `run_id` | string | no | Series run. |
| `series_sha256` | string, 64 hex | no | sha256 of the run's `series.csv`. |
| `generated_utc` | string `YYYY-MM-DDTHH:MM:SSZ` | no | When this publish ran (UTC). |
| `files` | array, at least one entry | no | Every other published file, sorted by path. |
| `files[].path` | string | no | POSIX path relative to the output directory; only allowlisted names. |
| `files[].sha256` | string, 64 hex | no | sha256 of the file's bytes. |

## Map files

Both files show the scene of `latest.json` with these classes:

| Code | Class | Colour (`config.py`) |
| --- | --- | --- |
| 0 | nodata (outside the reservoir or unobserved) | transparent |
| 1 | water | `COLOR_WATER` `#1d2e42` |
| 2 | dense hyacinth | `COLOR_DENSE_HYACINTH` `#15c912` |
| 3 | interface | `COLOR_INTERFACE` `#8ed89d` |
| 4 | bare soil | `COLOR_BARE_SOIL` `#a0783c` |
| 5 | removed by the exposed-bed correction | `COLOR_BED_REMOVED` `#d0573a` |

Class 5 exists only in the published map; the classifier's own outputs
stay 0–4. Its pixel count × 0.01 ha equals `removed_ha` to the hundredth,
so the map agrees with the corrected figures: classes 2 and 3 are the
hyacinth that counts in `hyacinth_corrected_ha`. When the correction was
not applied, no pixel is class 5.

- **GeoTIFF** (`map.geotiff`): single band, uint8, the scene's native CRS
  (UTM zone 14N for Endhó) and 10 m grid, nodata 0, embedded colour table,
  per-class legend band tags. For GIS download and analysis.
- **PNG** (`map.png`): RGBA, warped to EPSG:3857 (web Mercator) with
  nearest-neighbour resampling, same palette, nodata fully transparent.
  Place it on a web map with `map.bounds`, given in EPSG:4326 as
  `[[south, west], [north, east]]` (latitude first).

## True-colour image

`image.path` is a natural-colour photo of the `latest.json` scene, for a
map/photo toggle on a web map; each `same_month.years[].image.path` is
one of a same-month scene. All of them:

- **Grid:** exactly the `map.png` grid (same width, height and EPSG:3857
  transform), so `image.bounds` equals `map.bounds` and the two never
  misalign.
- **Clipping:** every photo is clipped to the reservoir (since 3.2.0; the
  `latest.json` photo was an unclipped rectangle before). A pixel is
  opaque (alpha 255) when it lies inside the reservoir polygon, rasterised
  on the same grid by pixel centre (nearest), and any band's reflectance
  is above −0.15 (`TRUE_COLOUR_VALID_MIN`); every other pixel is
  transparent (alpha 0), so the consumer's basemap shows around the
  reservoir and through no-data pixels.
- **Source:** bands B04, B03, B02 read at publish time straight from the
  scene's STAC item (exact `item_id`, Earth Search unless the run used
  another backend), not from smolf's scene cache, warped bilinearly onto
  the grid. DN become surface reflectance through smolf's radiometry
  module, with the scene's BOA offset derived once from B02 over the
  reservoir by the same rule and guard as the figures, so post-2022
  scenes are not washed out.
- **Stretch:** one fixed window, reflectance 0–0.30 → 0–1, then gamma
  `x ** (1/1.6)`, then 0–255 (`TRUE_COLOUR_MIN`, `TRUE_COLOUR_MAX`,
  `TRUE_COLOUR_GAMMA` in `config.py`), the same for every image, so dates
  and years compare visually. Brighter pixels (clouds, roofs) saturate.
  The gamma (since 3.2.0) lifts the reservoir from 6–9 % to 18–21 % mean
  brightness; it was chosen in `experiments/truecolour_gamma.md`.
- **Encoding:** RGBA WebP at quality 90, alpha lossless, not
  georeferenced; 17–25 KB per photo on the eleven November photos
  of the 2017–2026 series. The colour under a transparent pixel is the
  encoder's and carries no meaning.
- **Cost:** every photo is read over the network at publish time, up to
  roughly 30 s each. A series spanning ten years publishes up to eleven
  photos; a scene that is both the `latest.json` scene and a same-month
  representative is read once. Measured on 2026-11-18: the 2017–2026
  series (10 reads) published in 84 s, with clipped photos of 16–23 KB.
  Publishing therefore needs network access even when every scene is
  cached.
- **Integrity:** the image is an illustration bound to `item_id`. Its
  bytes are listed with their sha256 in `publish.json`, but unlike the
  map it is not verified against the run (the run never saw these
  pixels): a backend that reprocesses the item would change the photo,
  never a figure.
- **Licence:** Sentinel-2 data are free to publish under the Copernicus
  licence with attribution; show `image.attribution` with the photo.

## Determinism and integrity

- `latest.json` and `series.json` are canonical JSON: sorted keys, 2-space
  indent, UTF-8, trailing newline, no wall-clock timestamp. The same run
  plus the same level record yields byte-identical files, so unchanged
  inputs produce no spurious diffs. A daily level refresh followed by a
  publish of the same run changes only `current_level` (and `publish.json`).
- `publish.json` lists the sha256 of every other file; verify them after
  any transfer.
- Every image is deterministic for the same item bytes: rerunning publish
  with unchanged STAC items rewrites them byte for byte.
- Before replacing the output directory, smolf validates every JSON file
  against its schema; the whole contract is assembled in a sibling
  temporary directory (`.<name>.staging-*`) and swapped in by rename, the
  old directory moving to `.<name>.previous-*` for the instant of the
  swap. A reader never sees a half-written contract, and on any failure
  the previous contract is never lost; only publish's own leftovers (stale
  staging, restorable or superseded previous directories) may be cleaned
  up first.
- `--out` is resolved to its real path first, so publishing through a
  symlinked `--out` replaces the target and keeps the link in place.
- At the start of each publish smolf cleans up its own debris next to
  `--out`: stale `.<name>.staging-*` directories are removed when they
  hold only allowlisted files (anything else is left alone); a lone
  `.<name>.previous-*` (a crash between the swap's two renames) is
  renamed back when `--out` is missing, while several are refused; once
  `--out` holds a valid contract, old `.<name>.previous-*` directories are
  removed when each is itself a valid contract.

## Refusals and exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Published. |
| 2 | Precondition refusal, one line `refused: <reason>` on stderr; nothing is written. Also: neither or both of `--run` and `--latest` (`error: …`). |
| 1 | Unexpected error, or smolf's own output failing its schema or a raster guard (class map or true colour; a bug); nothing is published. |

`publish` refuses (exit 2) when:

- the output directory exists, is not empty, and is not a previous
  publish (no `publish.json`, or any file outside the allowlist): smolf
  only ever replaces its own contract, never unrelated data. The check
  needs `publish.json` at the top level before it looks further, so a
  foreign directory is refused without walking its tree; `--out` that is
  a file is refused too;
- a symlink anywhere in the `--out` path is broken (its target is
  missing), unless it is the final component and a lone
  `.<target>.previous-*` can be restored, or several `.<name>.previous-*`
  directories remain with
  `--out` missing (restore one by hand);
- the run is unknown, or there is no complete run (`--latest` selects the
  newest run, by run id, whose manifest is readable and whose status is
  complete; a run with a corrupt manifest is skipped);
- the run given with `--run` has an unreadable manifest;
- the run is not complete, or its code fingerprint is dirty;
- the run has no scene with figures (no `ok` scene);
- the latest scene's cached file does not hash to the run's record (a
  missing scene is re-ingested by its exact item id, and the hash must
  still match), or the re-ingest fails;
- a scene with figures has no `hyacinth_under_haze_ha` in its run row
  (a run built before 3.3.0): it is never published with a guessed 0;
  rebuild the run with `smolf series build`;
- the re-classified scene's hectares per class, or its hectares rescued
  under haze, differ from the run row at the hundredth (the scene is
  re-classified with the run's water mirror, rebuilt from the row's
  `level_m` when the exposed-bed correction applied, and without one
  otherwise);
- the exposed-bed correction was applied to the latest scene and the
  flood-stage map or the stage–area curve differs (sha256) from the one
  the run's manifest recorded. These two hashes are checked only in that
  case: without the correction no class 5 exists, and neither input
  touches the published figures;
- the exposed-bed margin recomputed from the row's `level_m` with the
  committed stage–area curve does not round to the row's margin, or the
  class-5 area differs from `removed_ha` at the hundredth;
- the true-colour image of the latest scene or of any `same_month`
  scene cannot be read (STAC item missing, catalog or COG unreachable): a
  contract without its photos would be partial.

## Versioning

- `schema_version` is the contract version; `smolf_version` is the
  software version. They are distinct: a smolf release may leave the
  contract unchanged. smolf writes exactly its own `schema_version`; each
  schema accepts any version with the same major (pattern
  `^3\.[0-9]+\.[0-9]+$`), so files from a later minor release still
  validate against an older tag's schemas. A bare `$` lets a trailing
  newline through in Python's `re` (it matches before a final `\n`), so
  each schema also says `"not": {"pattern": "\n"}`. A lookahead would do
  the same but does not compile in RE2-based validators (Go, some Rust).
- Every contract change bumps the smolf version and is released under a
  git tag. History:
  - `schema_version` 1.0.0 — first release, smolf `v0.2.0`.
  - `schema_version` 2.0.0 — smolf `v0.3.0` (breaking; issue #38,
    `docs/decisions.md` entry 16): `removed_ha` is `null` whenever
    `bed_applied` is false, and the schemas require it (non-null when
    true); the `schema_version` pattern rejects a trailing newline. A
    consumer that read `removed_ha` as a number without checking
    `bed_applied` must handle `null`.
  - `schema_version` 2.1.0 — smolf `v0.4.0` (additive; issue #42):
    `latest.json` gains `reservoir_outline`, the reservoir polygon as an
    RFC 7946 MultiPolygon in EPSG:4326. It is a field of `latest.json`
    rather than a file of its own because `publish.json`'s closed
    `files[].path` pattern would make a new file a breaking change.
  - `schema_version` 3.0.0 — smolf `v0.5.0` (breaking; spec #44,
    `docs/decisions.md` entry 13 amended): every scene of `latest.json`
    and `series.json` carries the required `display_status` (`HIDDEN` for
    every POOR entry) and the required `display_ha` (integer
    `{total, dense, interface}`, `null` when the corrected figures are);
    `constants` gains the required `green_max_ha` and `red_min_ha`;
    `series.json` gains the required `monthly` (per-month median of
    corrected hectares, with `n_scenes`);
    `latest.json` gains the required, nullable `same_month` (`month`,
    `years[]` of `year`, `n_scenes`, `scene`, `image`), one lower-median
    scene and a reservoir-clipped RGBA WebP per year;
    `latest.json` gains the required `image` (`path`, `bounds`,
    `attribution`), a Sentinel-2 true-colour WebP on the `map.png` grid,
    and `publish.json`'s `files[].path` pattern admits
    `image/<date>_<item_id>.webp` and `image/clipped/<date>_<item_id>.webp`
    (ADR 0001); `reservoir_outline` is required; the version pattern
    of all three schemas is `^3\.[0-9]+\.[0-9]+$`, so 2.x consumers reject
    3.x files until they move to `v0.5.0`.
  - `schema_version` 3.1.0 — smolf `v0.6.0` (additive; spec #56, issue
    #57, ADR 0002): every `same_month.years[]` entry of `latest.json`
    gains the optional boolean `featured` (always written by smolf),
    `true` for the current year and the first, middle and last earlier
    years (see "Featured years"); the schema rejects `featured: true`
    with a null `scene` or `image`. Every year keeps its hectares and its
    clipped photo, so 3.0.0 consumers are unaffected.
  - `schema_version` 3.2.0 — smolf `v0.7.0` (minor; issue #60): no field
    changes. Every photo, the `latest.json` one included, is an RGBA WebP
    clipped to the reservoir and transparent on no-data pixels, rendered
    with gamma 1.6 after the fixed window (see "True-colour image"). A
    consumer that drew the `latest.json` photo as an opaque rectangle now
    sees its basemap around the reservoir.
  - `schema_version` 3.3.0 — smolf `v0.8.0` (minor; issue #69, spec
    #70): every scene of `latest.json` and `series.json` carries
    `hyacinth_under_haze_ha`, the raw hyacinth rescued under thin haze
    (already included in `hyacinth_raw_ha` and every figure derived from
    it). The field is required in the 3.3.0 schemas although the bump is
    minor (ruling of spec #70): the `^3\.` pattern of a 3.2 schema
    accepts 3.3.0 files and ignores the unknown field, so consumers
    pinned to 3.2 keep validating. The raw hectares of a 3.3.0 run exceed
    those of a 3.2.0 run of the same scene by `hyacinth_under_haze_ha`.
- **Breaking (major `schema_version`):** removing or renaming a field,
  changing a type, unit or meaning, adding or removing an enum value
  (enums are closed, so a new value fails an older schema), making a
  required field optional, or adding a required field (one exception,
  3.3.0: a new required field that older minor schemas ignore as an
  additional property).
- **Additive (minor `schema_version`):** adding an optional field, or
  changing how a published photo is rendered (stretch, gamma, clipping)
  while every field, type and path stays the same. Nothing else is minor.
- Each schema's `$id` is a stable, unversioned identifier ending in
  `<name>.schema.json`; the version lives only in `schema_version`.
  Schemas are not copied into the output directory.
- Consumers should pin a smolf tag, take the three schemas from that tag
  and validate every file with them before use. Those schemas accept any
  `3.x.y`, so a later minor release keeps validating and a new major
  release is rejected until the consumer moves to its tag.
