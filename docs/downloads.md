# Download bundle (`smolf export series-csv`)

What `smolf export series-csv` writes: a folder of plain files derived
from one finished publish, for a reader who wants the series as a
spreadsheet rather than as JSON. The column dictionary in the code
(`COLUMN_DICTIONARY` in `src/smol_frontier/publish/export.py`), which the
bundle's `README.txt` prints, is the source of truth for column meanings;
this document explains the bundle as a whole. The per-scene class maps,
a separate export, are described at the end
([Per-scene maps](#per-scene-maps-smolf-export-maps)).

First release containing the bundle: smolf 0.9.0.

## Outside the contract

The bundle is **not part of the publish contract** (`docs/contract.md`):

- its files are not listed in `publish.json` and are never written into
  the publish directory;
- it is not versioned by `schema_version`;
- it has no JSON Schema. Its layout may change in any smolf release, so
  a downstream consumer that needs stable fields reads `series.json`.

The bundle only re-expresses figures the contract already publishes. It
computes nothing new: every cell is a field of `series.json`, and the
README's constants and reservoir area come from `latest.json`. The
outline zip is the committed AOI polygon, checked against `latest.json`'s
reservoir area.

## Command

```sh
uv run smolf export series-csv [--from <publish dir>] [--out <dir>]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--from` | `outputs/publish/` | Publish directory to read. |
| `--out` | `outputs/downloads/` | Output directory, replaced as a whole. |

With `SMOLF_DATA_DIR` set, the defaults become
`$SMOLF_DATA_DIR/outputs/publish/` and `$SMOLF_DATA_DIR/outputs/downloads/`
(resolved at call time by `paths.py`); an explicit option always wins.

The command reads the published contract (`publish.json`, `series.json`
and `latest.json`) and the committed AOI polygon for the outline zip. It
makes no network call and reads no scene cache or run directory. On success it prints `exported: <dir>` on
stdout.

## Files

`--out` contains exactly:

```text
series.csv           one row per series.json scene, in its order
series_monthly.csv   one row per series.json `monthly` entry
README.txt           licence, disclaimer, citation, sources, provenance, column dictionary
LEEME.txt            the same in Spanish (prevails if the two differ)
reservoir_outline.zip  the reservoir polygon (vaso) as a zipped shapefile
SHA256SUMS           sha256 of the five files above
```

- `series.csv` keeps POOR scenes, with their figure cells empty, so the
  row count equals the number of scenes in `series.json`.
- `series_monthly.csv` covers every month from the first to the last
  scene month, none skipped; `median_ha` is empty when `n_scenes` is 0.
- `README.txt` is plain ASCII, wrapped at 79 columns (a line ending in a
  sha256 may run longer), and ends with a pointer to `docs/contract.md`
  for the full field definitions.
- `LEEME.txt` is its Spanish counterpart, UTF-8 (with accents), the same
  eight sections in the same order. The CSVs are the same for both
  languages: column names and codes stay English, and LEEME.txt glosses
  each code (`GOOD = BUENA`, `EXPOSED_BED = lecho expuesto`). The
  citation is byte-identical in both files, so a figure cited in either
  language traces to the same run. Both files say that LEEME.txt
  prevails if they differ. Its licence section links Creative Commons'
  official Spanish deed and legal code instead of translating them.

The same publish always gives byte-identical files: no wall clock is
read (the README's copyright year is the latest scene's year).

### Reservoir outline

`reservoir_outline.zip` (first released in smolf 0.11.0) holds
`reservoir_outline.shp`, `.shx`, `.dbf`, `.prj` and `.cpg` (UTF-8) plus a
short English `README.txt`:

- Geometry: the dissolved AOI polygon (`aoi.py`) in EPSG:4326 at full
  precision, one feature. It is written from the committed polygon at
  export time, not copied byte for byte.
- Attribute: a single field, `area_ha`, the reservoir area computed in
  EPSG:32614 and rounded to 2 decimals (1261.75 ha). This is
  `latest.json.reservoir_area_ha`; the export refuses if the committed
  polygon no longer gives that area. The committed input's `count`,
  `label` and stale `area_ha` (1262.592) are not carried over.
- README: source (created by the author from JRC Global Surface Water
  v1.4, occurrence >= 25 %, Pekel et al., 2016), area, CRS and licence
  (CC BY 4.0, same holder as the bundle, JRC GSW credited).
- Same geometry as `latest.json.reservoir_outline`, with two differences.
  `latest.json` rounds coordinates to 6 decimals (the largest gap is
  6.8e-7 degrees, about 7.5 cm) and orients rings by the RFC 7946
  right-hand rule; the shapefile keeps full precision and the shapefile
  ring convention (exterior clockwise).
- Deterministic: fixed member order and timestamps (the latest scene's
  date), and the `.dbf` header date set to the same day.

## Columns

Both CSVs start with the five provenance columns and the two rights
columns, then their own:

- `series.csv`: the per-scene fields of the contract, in the order of
  its per-scene table, with the `display_ha` object flattened in place
  into `display_ha_total`, `display_ha_dense` and `display_ha_interface`.
- `series_monthly.csv`: `month`, `median_ha`, `n_scenes`.

`README.txt` lists every column with its unit, its meaning and when the
cell is empty; a test fails if a column lacks an entry.

### Dialect

- UTF-8 without BOM, comma separator, LF line endings, one header row.
- RFC 4180 quoting, only where a value needs it.
- `null` is an empty cell; booleans are `true` / `false`.
- Numbers use fixed decimals per column, the contract's rounding:
  hectares, percentages and water levels 2 decimals (`15.70`, not
  `15.7`), fractions 4, the `display_ha_*` columns whole hectares.

### Provenance columns

Every row of both CSVs carries the same five values, so a file copied
out of its folder still traces back to the publish:

| Column | Value |
| --- | --- |
| `run_id` | Id of the series run. |
| `series_sha256` | sha256 of the run's internal `series.csv` (the series id). |
| `series_json_sha256` | sha256 of the published `series.json` the bundle was made from. |
| `schema_version` | Contract version of that `series.json`. |
| `smolf_version` | smolf version that **published** that `series.json`, not the one that exported it. |

`README.txt` repeats them under "Provenance", adds the exporting smolf
version (`exported by smolf:`), `reservoir_area_ha` and the published
constants.

### Rights columns

Every row also carries the licence, so a CSV or a single row copied
without `README.txt` still says how it may be reused and whom to credit:

| Column | Value |
| --- | --- |
| `license` | `CC-BY-4.0` (SPDX id). |
| `rights_holder` | `Oscar Olvera Neria (UAM Azcapotzalco)`. |

**Name clash.** `series_sha256` keeps its contract meaning: the hash of
the series run's internal `series.csv`. It is **not** the hash of the
`series.csv` in the bundle; that one is in `SHA256SUMS`.

## Licence

The data in the bundle is released under CC BY 4.0, held by Oscar
Olvera Neria (UAM Azcapotzalco), with the copyright year of the latest
scene; the smolf code stays under the MIT License. `README.txt` states
the licence, a disclaimer (remote-sensing estimates, not an official
measurement, no warranty), a suggested citation (naming the
`run_id`) and the attributions: modified Copernicus Sentinel data
(Sentinel-2 L2A) for the scene years, the SIH water levels (station
ENDHD), and JRC Global Surface Water v1.4 for the reservoir polygon.

## Verification

`SHA256SUMS` is in `sha256sum` format and covers `LEEME.txt`,
`README.txt`, `reservoir_outline.zip`, `series.csv` and
`series_monthly.csv` (not itself). In the bundle
folder:

```sh
sha256sum -c SHA256SUMS
```

Before writing, the export checks its input the way a consumer should:
`publish.json` passes its schema; `series.json` and `latest.json` hash to
their `publish.json` entries and pass their schemas; and all three name
the same `run_id` and `series_sha256`.

## Writing

The files are written into a sibling staging directory
(`.<name>.staging-*`) and swapped in by rename, as `publish` does, so a
reader never sees a half-written bundle and a failure keeps the previous
one. `--out` is resolved to its real path first, so exporting through a
symlink replaces the target and keeps the link. `--out` may only hold
the six bundle files; anything else is someone else's data.

## Refusals and exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Exported. |
| 2 | Refusal, one line `refused: <reason>` on stderr; nothing is written and `--out` is left as it was. |
| 1 | Unexpected error (a bug or an I/O failure). |

`export series-csv` refuses (exit 2) when:

- `publish.json`, `series.json` or `latest.json` cannot be read or is not
  valid JSON;
- any of the three fails its packaged schema (after upgrading smolf,
  re-run `smolf publish` first: a publish from an older minor release
  can lack a property the packaged schemas require, such as
  `stage_area_curve` of 3.4.0);
- `publish.json` does not list `series.json` or `latest.json`;
- `series.json` or `latest.json` does not hash to its `publish.json`
  entry;
- `publish.json` or `latest.json` names a different `run_id` or
  `series_sha256` than `series.json`;
- `--out` exists and is not a directory;
- `--out` is the publish directory or inside it;
- the committed AOI polygon does not give `latest.json`'s
  `reservoir_area_ha` (the polygon changed after the publish);
- `--out` holds anything other than the six bundle files as regular
  files (another file, a subdirectory or a symlink);
- a symlink in the `--out` path is broken or cannot be resolved, or
  `--out` is missing while several `.<name>.previous-*` directories
  remain (restore one by hand).

## Per-scene maps (`smolf export maps`)

One classified GeoTIFF per scene of a finished publish, for a reader who
wants the map behind any date of the series. First released in smolf
0.11.0. Like the bundle it is **outside the contract**: not listed in
`publish.json`, not versioned by `schema_version`, no JSON Schema.

```sh
uv run smolf export maps [--from <publish dir>] [--out <dir>]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--from` | `outputs/publish/` | Publish directory to read. |
| `--out` | `outputs/maps/` | Output directory, replaced as a whole. |

With `SMOLF_DATA_DIR` set the defaults move under it, as for
`series-csv`. The default is its own folder, not `outputs/downloads/`,
because each export replaces its directory as a whole.

### Files

```text
<date>_<item_id>.tif   one class map per scene with figures in series.json
index.csv              one row per map: scene, file, sha256, run
README.txt             files, class codes and colours, licence (CC BY 4.0)
SHA256SUMS             sha256 of every other file
```

- **Which scenes:** every scene with figures in `series.json`, DOUBTFUL
  ones included and no gate applied, so a consumer picks the scenes
  itself. POOR scenes have no figures and get no map.
- **Format:** the published map's format exactly, written by the same
  guarded writer: single-band uint8, nodata 0, embedded colour table,
  band tags `class_0`..`class_5`, EPSG:32614, 10 m, the scene grid,
  tiled 256×256, DEFLATE. Classes 0–5, where class 5 is the hyacinth
  removed by the exposed-bed correction. The latest scene's file is
  byte-identical to the publish's `map/<date>_<item_id>.tif`.
- **File names:** `<date>_<item_id>.tif`, the published map's name
  without its `map/` directory.
- **`index.csv`** columns are `date`, `item_id`, `file`, `sha256`,
  `bytes`, `run_id`, `series_sha256` (the run's internal `series.csv`
  hash, the series id) and `series_json_sha256` (the sha256 of the
  publish's `series.json`), then the reuse keys `map_key` (the scene's
  map fingerprint, the same as the viewer export's), `smolf_version` and
  `config_hash` (repeated on every row; added in 0.14.0). Rows are in
  series order. A consumer checks `run_id`, `series_sha256` and
  `series_json_sha256` against the `series.json` it serves before serving
  the maps.
- **Deterministic:** the same publish, run and cache give byte-identical
  files, whether each map was kept or built (see Reuse across runs).

### Verification

Before writing, the export:

1. Verifies the publish as `series-csv` does.
2. Resolves its run under the series directory, by `run_id`. The run
   must be complete, and its manifest's `series_sha256` and its
   `series.csv` sha256 must equal the publish's `series_sha256`.
3. Keeps or builds every map (see Reuse across runs); a built map goes
   through the publish's own map builder. That builder checks:
   - the cache sha256;
   - per-class and haze hectares against the run row;
   - the flood-stage map and stage–area curve hashes against the run
     manifest;
   - class-5 area against `removed_ha`.
4. Checks each map, kept or built, against its `series.json` scene, to
   the hundredth:
   - class 2 × 0.01 ha = `dense_corrected_ha`, and class 3 × 0.01 ha =
     `interface_corrected_ha` (when `bed_applied` is false, `dense_ha`
     and `interface_ha` instead);
   - class 5 × 0.01 ha = `removed_ha` (0 when the correction did not
     apply).

   This holds because the flood-stage map stages every reservoir pixel.

Any failure refuses (exit 2, `refused: <reason>`), and the previous
export is left as it was. The out-dir rules are those of `series-csv`:
staging plus a swap by rename, and refusal of an `--out` inside the
publish directory or holding anything other than the export's own
files.

### Reuse across runs

The export is incremental: it keeps the previous export's GeoTIFF for
every scene whose map inputs are unchanged, whatever run it came from,
and builds only new or changed scenes. A previous map is kept only when:

- the previous `index.csv` parses and has the three key columns;
- its row's `smolf_version` and `config_hash` equal the running smolf
  version and config hash (otherwise every map is rebuilt);
- its `file` is a map file name, the file hashes to the row's `sha256`
  (a missing, tampered or truncated file is rebuilt), and the row's
  `map_key` equals the scene's current one (the scene record, with its
  cache sha256 and run row, plus the run's AOI polygon, flood-stage map
  and stage–area curve hashes).

A kept map is decoded and checked against its new `series.json` scene
(step 4); if it disagrees it is rebuilt, and only a built map that
disagrees refuses. A kept scene's row keeps its `sha256` and `bytes` and
gets the new `run_id`, `series_sha256` and `series_json_sha256`;
`README.txt` and `SHA256SUMS` are rebuilt, and a scene dropped from the
series disappears with the whole-folder swap. The output is
byte-identical to a full export of the same run into an empty `--out`;
this also relies on the pinned dependencies (GDAL and zlib via the locked
rasterio wheel), which the smolf version pins through `uv.lock`.
The first export after upgrading from 0.13.0 or earlier rebuilds every
map once, because the old `index.csv` has no keys.

### Cost

The export needs the **scene cache**, because a run keeps only scene
JSONs and no rasters. A series build has already ingested every scene
(630 MB for 485 cached scenes here), so a host that builds the series
has what it needs. A missing scene is re-ingested by its exact item id
and backend (no search, no fallback).

Measured on 2026-11-23 for run `20261119T200232`:

- 469 maps in 5 min 47 s (single process, mostly single-threaded);
- 7.6 MB in total (11–22 KB per map);
- about 400 MB peak memory.

That is the cost of a full export (the first one, or after a version or
config change). Re-measure on a smaller server. A later export keeps the
unchanged maps (see Reuse across runs), so it costs a few seconds of
hashing and checking plus about 1.3 s per built map. Only built maps
need their scene in the cache.

## Per-date viewer archive (`smolf export viewer`)

One folder per scene for endho's public "map by date" viewer (issue
#102): the scene's photo, its rendered class map and its figures,
including wet biomass. First released in smolf 0.12.0. Like the other
exports it is **outside the contract**: not listed in `publish.json`,
not versioned by `schema_version`, no JSON Schema. Field names stay
stable across minor releases.

```sh
uv run smolf export viewer [--from <publish dir>] [--out <dir>]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--from` | `outputs/publish/` | Publish directory to read. |
| `--out` | `outputs/viewer/` | Output directory, replaced as a whole. |

With `SMOLF_DATA_DIR` set the defaults move under it. Progress goes to
stderr, one line per scene (`built <folder>` or `kept <folder>`);
stdout prints `exported: <dir>`.

### Files

```text
<date>_<item_id>/
  photo.webp        Sentinel-2 true colour of the scene
  classified.webp   the published class map, same grid and bounds
  figures.json      the scene's figures
index.json          run, series and the sha256 of every scene file
README.txt          layout, biomass method, licence (CC BY 4.0)
SHA256SUMS          sha256 of every other file, as <folder>/<file>
```

- **Which scenes:** the same as `export maps`. That means every scene
  with figures in `series.json`, DOUBTFUL and lower-bound ones included,
  with no gate. endho selects what it shows. POOR scenes get no folder.
- **`photo.webp`:** built exactly like `latest.image`:
  - EPSG:3857 on the class map's display grid;
  - RGBA, transparent outside the reservoir and on no-data pixels;
  - fixed reflectance window 0–0.30, then gamma 1.6, the same for every
    date.

  It is read from the item's COGs at export time, so it needs the
  network. A failed read is retried up to three times.
- **`classified.webp`:** the published map, classes 0–5 with the
  exposed-bed correction applied. It is painted with the published
  colours, class 0 is transparent, and it is the same display image as
  `latest.map.png`. It is **lossless**, so every pixel decodes to
  exactly one class. The export decodes each file and refuses it unless
  it matches the classes it was drawn from.
- **`figures.json`** (canonical JSON, sorted keys):
  - `date`, `item_id`, `run_id`, `bounds`
    (`[[south, west], [north, east]]`, EPSG:4326, shared by both
    images), `attribution` (`Copernicus Sentinel data <year>`).
  - The scene's `series.json` fields, verbatim:
    `hyacinth_corrected_ha`, `dense_corrected_ha`,
    `interface_corrected_ha`, `display_ha`, `pct_reservoir_corrected`,
    `cover_band`, `display_status`, `removed_ha`, `level_m`,
    `valid_frac`, `cloud_frac`, `bed_applied`, `quality_light`,
    `regime`. These are corrected figures only.
  - `reservoir_ha` (1261.75) and `classes`: `{ha, pct_reservoir}` for
    `water`, `dense_hyacinth`, `interface`, `bare_soil` and
    `bed_removed`, counted on the published map, plus `no_data`, the
    area the five classes leave.
    - The pipeline cannot tell cloud or shadow from unclassified
      pixels, so `no_data` is one figure.
    - Classes sum to `reservoir_ha` to the hundredth.
    - `dense_hyacinth`, `interface` and `bed_removed` equal the
      `series.json` hectares.
  - Wet biomass: `biomass_wet_t`, `biomass_wet_t_low` and
    `biomass_wet_t_high` (tonnes of wet weight), plus
    `biomass_density_kg_m2` (`{central, low, high}`) and
    `biomass_source`.
  - `pct_reservoir` and biomass are null on `LOWER_BOUND` scenes. The
    copied `series.json` fields stay verbatim there.
- **`index.json`:** `run_id`, `series_sha256`, `series_json_sha256`,
  `smolf_version`, `config_hash` (sha256 of the settings, as in a run
  manifest) and one entry per folder, in series order: `date`,
  `item_id`, `folder`, `map_key` and `photo_key` (see Incremental) and
  `files`, the sha256 of each of the three files. A consumer refuses the archive unless `run_id` and
  `series_sha256` match the series it serves.

### Wet biomass

```text
t = hyacinth_corrected_ha × 10 000 m²/ha × density kg/m² / 1 000 kg/t
```

- **Rounding:** half up, to the tonne.
- **Density:** 18.32 kg/m², range 11.67–23.84. This is the field
  measurement at Endhó (UAM Azcapotzalco, 2025, wet weight, 15
  samples); its provenance is in `config.py`.
- **Interface:** counts at the full density.
- **Area:** always the corrected area, never the raw one. On 2024-06-09
  the raw area would give about 28 times the tonnage.
- **Consistency:** it matches the author's two earlier implementations
  (smol, smol-gee) to the tonne for the same hectares; see
  `tests/test_biomass.py`.
- **Reading it:** an order-of-magnitude estimate, always shown with its
  range.

### Incremental

The export reuses the folders of the previous export in `--out`,
whatever its run, so a daily export after a new run only builds the
new or changed scenes.

- **Reusable folders:** a folder of the previous export is a candidate
  when that export was written by the same smolf version and settings
  (`smolf_version` and `config_hash` in its `index.json`) and its three
  files still hash to that index. Another version or config rebuilds
  every folder, since the map rebuild, the rendering and the photo
  window are smolf's code and settings. A development checkout that
  changes code without bumping the version should start from an empty
  `--out`.
- **Kept:** a candidate is kept (`kept <folder>`) when both hold:
  - its `map_key` equals the scene's new one. The key is the sha256 of
    the scene's run record (cache sha256 and run row) and the run's
    `aoi_polygon`, `flood_stage_tif` and `stage_area_curve_csv` hashes,
    everything the class map is built from;
  - its `figures.json` rebuilds byte for byte from the new
    `series.json` row under its old `run_id`.

  `photo.webp` and `classified.webp` are copied; `figures.json` is
  written again with the new `run_id`. No map rebuild, no photo read.
- **Map rebuilt, photo kept:** any other candidate has its map and
  figures rebuilt and checked. Its `photo.webp` is copied when its
  `photo_key` equals the new one: the sha256 of the item id, the backend
  whose COGs are read, the display bounds and the reservoir polygon
  (the BOA-offset region and the clip), which with the version and
  config is all the photo depends on (`built <folder> (photo kept)`).
- **Built:** a new scene, or a missing or tampered folder, is built
  from scratch, photo read included (`built <folder>`).
- **Dropped scenes:** a folder whose scene is not in the new
  `series.json` is not carried over.
- **Writing:** the directory is always swapped in whole. `index.json`,
  `README.txt`, `SHA256SUMS` and every `figures.json` name the current
  run, so an archive never mixes runs.

### Verification

Each built folder passes every check `export maps` makes (run, cache,
per-class hectares, class 5 against `removed_ha`, classes against
`series.json`). On top of those, the export checks the decoded
`classified.webp` and the class areas against the reservoir area. Any
failure refuses (exit 2), and the previous archive is left as it was.
The out-dir rules are those of `export maps`, with scene folders
allowed when they hold only the three scene files.
