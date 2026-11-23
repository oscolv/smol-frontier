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
  publish's `series.json`). Rows are in series order. A consumer checks
  `run_id`, `series_sha256` and `series_json_sha256` against the
  `series.json` it serves before serving the maps.
- **Deterministic:** the same publish, run and cache give byte-identical
  files.

### Verification

Before writing, the export:

1. Verifies the publish as `series-csv` does.
2. Resolves its run under the series directory, by `run_id`. The run
   must be complete, and its manifest's `series_sha256` and its
   `series.csv` sha256 must equal the publish's `series_sha256`.
3. Rebuilds every map through the publish's own map builder. That
   builder checks:
   - the cache sha256;
   - per-class and haze hectares against the run row;
   - the flood-stage map and stage–area curve hashes against the run
     manifest;
   - class-5 area against `removed_ha`.
4. Checks each map against its `series.json` scene, to the hundredth:
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

Re-measure on a smaller server. The export is not incremental: every run
rebuilds every map, because a map depends on the cache bytes, code,
config and committed inputs.
