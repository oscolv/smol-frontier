# Publish contract (`schema_version` 2.0.0)

What `smolf publish` writes and what a downstream consumer may rely on.
The JSON Schemas packaged with smolf (`src/smol_frontier/schemas/`:
`latest.schema.json`, `series.schema.json`, `publish.schema.json`, JSON
Schema draft 2020-12) are the source of truth; this document explains
them field by field. `tests/test_contract_doc.py` fails if a schema
property is missing here.

First release containing this contract: smolf 0.3.0 (tag `v0.3.0`).
Changes from 1.0.0 are listed under "Versioning".

## Principles

- **Neutral.** smolf publishes every figure as computed. It never hides,
  filters, smooths or substitutes a figure. A DOUBTFUL scene, a scene whose
  `valid_frac` is below `frac_publishable`, or a large jump between scenes
  is published like any other; the downstream consumer applies its own
  display gates using the quality light, the visible fraction, the dates,
  the series history and the published constants. The only scenes without
  figures are POOR scenes, which the pipeline already skips upstream.
- **No recomputation downstream.** Every number a consumer displays
  (percentages, cover band, reservoir area, thresholds) is in the contract.
- **Provenance, not contract.** The series run's `manifest.json` is
  internal provenance; it is never copied out. Each document carries
  `run_id` and `series_sha256` so any figure traces back to the exact run.
- **Derived products only.** Both raster writers (GeoTIFF and PNG) accept
  a uint8 class array with values 0–5 and nothing else (the PNG writer
  colours the classes itself), and only allowlisted files are ever
  written, so source imagery or reflectance of any sensor cannot be
  published.

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
```

The two map files are those of the scene described by `latest.json`; maps
of earlier scenes are not kept. Map file names change with the scene, so a
cache can never pair a new `latest.json` with an old map.

## Conventions

- Field names are English and reuse the `series.csv` column names; units
  are in the names: `_ha` hectares, `_m` metres above sea level (water
  stage), `pct_` percent (0–100), `_frac` fraction (0–1).
- Rounding: hectares, percentages and water levels to 2 decimals;
  fractions to 4 decimals.
- Dates: UTC acquisition date, ISO `YYYY-MM-DD`; no acquisition time in
  2.0.0.
- `null` is explicit: a field defined by the schema is always present,
  with `null` where the value does not exist (never omitted).
- Additional properties are allowed in every object, so a consumer pinned
  to 2.0.0 keeps validating files from a later additive (minor) release.

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
| cota (water-surface stage) | `level_m` |
| vaso (reservoir basin polygon) | `reservoir_area_ha` |

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

When `bed_applied` is false, `removed_ha`, every `*_corrected` field and
`cover_band` are `null` and `bed_reason` says why; smolf never substitutes the raw
figure. When it is true, they are all non-null. Both schemas define every
non-POOR scene identically (`tests/test_publish.py` keeps them in step). A
POOR entry in `series.json` carries `date`, `item_id`, `quality_light`
(POOR) and `valid_frac`, and `null` for every other field.

## `latest.json`

The latest scene of the run that has figures (the last `ok` scene, GOOD
or DOUBTFUL), with every per-scene field above at the top level, plus:

| Field | Type | Unit | Null | Meaning |
| --- | --- | --- | --- | --- |
| `schema_version` | string `2.x.y` | — | no | Contract version; smolf writes exactly `2.0.0`, the schema accepts any `2.x.y` (no trailing newline). |
| `smolf_version` | string | — | no | smolf version that wrote the file (`0.3.0` for this release). |
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
| `constants` | object | — | no | Thresholds a consumer needs for its own gates. |
| `constants.frac_publishable` | number 0–1 | fraction | no | Minimum `valid_frac` for a percentage to be fully trustworthy (0.95). smolf still publishes the percentage below it; the consumer decides (e.g. show hectares only, as a lower bound). |
| `constants.green_max_pct` | number 0–100 | % | no | Upper bound (exclusive) of GREEN (30.0). |
| `constants.red_min_pct` | number 0–100 | % | no | Lower bound (inclusive) of RED (50.0). |
| `current_level` | object or `null` | — | yes | Last day with a level in the local SIH level record at publish time (the refreshed copy under `SMOLF_DATA_DIR` when it exists, else the committed one), independent of the scene and of the wall clock; `null` when the record has no level. |
| `current_level.date` | string `YYYY-MM-DD` | — | no | That day. |
| `current_level.level_m` | number | m a.s.l. | no | Its water level. |
| `current_level.levels_sha256` | string, 64 hex | — | no | sha256 of the level record file, to judge freshness and provenance. |
| `map` | object | — | no | The scene's map files. |
| `map.geotiff` | string | — | no | Relative path `map/<date>_<item_id>.tif`. |
| `map.png` | string | — | no | Relative path `map/<date>_<item_id>.png`. |
| `map.bounds` | `[[south, west], [north, east]]` | degrees, EPSG:4326 | no | Extent of the PNG, 6 decimals, for a web-map image overlay. |

## `series.json`

| Field | Type | Null | Meaning |
| --- | --- | --- | --- |
| `schema_version` | string `2.x.y` | no | Contract version; smolf writes exactly `2.0.0`, the schema accepts any `2.x.y` (no trailing newline). |
| `smolf_version` | string | no | smolf version that wrote the file. |
| `run_id` | string | no | Series run. |
| `series_sha256` | string, 64 hex | no | sha256 of the run's `series.csv`. |
| `scenes` | array of per-scene objects | no | Every `ok` and every POOR (skipped) scene of the run, sorted by date then item id, with the per-scene fields above. Error scenes are excluded (see `run_counts.errors` in `latest.json`). |

Quality flags (degrading/informational) are not part of 2.0.0.

## `publish.json`

The integrity index, written last. It is the only non-deterministic file
and never lists itself.

| Field | Type | Null | Meaning |
| --- | --- | --- | --- |
| `schema_version` | string `2.x.y` | no | Contract version; smolf writes exactly `2.0.0`, the schema accepts any `2.x.y` (no trailing newline). |
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

## Determinism and integrity

- `latest.json` and `series.json` are canonical JSON: sorted keys, 2-space
  indent, UTF-8, trailing newline, no wall-clock timestamp. The same run
  plus the same level record yields byte-identical files, so unchanged
  inputs produce no spurious diffs. A daily level refresh followed by a
  publish of the same run changes only `current_level` (and `publish.json`).
- `publish.json` lists the sha256 of every other file; verify them after
  any transfer.
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
| 1 | Unexpected error, or smolf's own output failing its schema or the raster guard (a bug); nothing is published. |

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
- the re-classified scene's hectares per class differ from the run row at
  the hundredth;
- the exposed-bed correction was applied to the latest scene and the
  flood-stage map or the stage–area curve differs (sha256) from the one
  the run's manifest recorded. These two hashes are checked only in that
  case: without the correction no class 5 exists, and neither input
  touches the published figures;
- the exposed-bed margin recomputed from the row's `level_m` with the
  committed stage–area curve does not round to the row's margin, or the
  class-5 area differs from `removed_ha` at the hundredth.

## Versioning

- `schema_version` is the contract version; `smolf_version` is the
  software version. They are distinct: a smolf release may leave the
  contract unchanged. smolf writes exactly its own `schema_version`; each
  schema accepts any version with the same major (pattern
  `^2\.[0-9]+\.[0-9]+$`), so files from a later minor release still
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
- **Breaking (major `schema_version`):** removing or renaming a field,
  changing a type, unit or meaning, adding or removing an enum value
  (enums are closed, so a new value fails an older schema), making a
  required field optional, or adding a required field.
- **Additive (minor `schema_version`):** adding an optional field. Nothing
  else is minor.
- Each schema's `$id` is a stable, unversioned identifier ending in
  `<name>.schema.json`; the version lives only in `schema_version`.
  Schemas are not copied into the output directory.
- Consumers should pin a smolf tag, take the three schemas from that tag
  and validate every file with them before use. Those schemas accept any
  `2.x.y`, so a later minor release keeps validating and a new major
  release is rejected until the consumer moves to its tag.
