# Download bundle (`smolf export series-csv`)

What `smolf export series-csv` writes: a folder of plain files derived
from one finished publish, for a reader who wants the series as a
spreadsheet rather than as JSON. The column dictionary in the code
(`COLUMN_DICTIONARY` in `src/smol_frontier/publish/export.py`), which the
bundle's `README.txt` prints, is the source of truth for column meanings;
this document explains the bundle as a whole.

First release containing the bundle: smolf 0.9.0.

## Outside the contract

The bundle is **not part of the publish contract** (`docs/contract.md`):

- its files are not listed in `publish.json` and are never written into
  the publish directory;
- it is not versioned by `schema_version`; the contract stays 3.3.0;
- it has no JSON Schema. Its layout may change in any smolf release, so
  a downstream consumer that needs stable fields reads `series.json`.

The bundle only re-expresses figures the contract already publishes. It
computes nothing new: every cell is a field of `series.json`, and the
README's constants and reservoir area come from `latest.json`.

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

The command reads only the published contract: `publish.json`,
`series.json` and `latest.json`. It makes no network call and reads no
scene cache or run directory. On success it prints `exported: <dir>` on
stdout.

## Files

`--out` contains exactly:

```text
series.csv           one row per series.json scene, in its order
series_monthly.csv   one row per series.json `monthly` entry
README.txt           licence, citation, sources, provenance, column dictionary
SHA256SUMS           sha256 of the three files above
```

- `series.csv` keeps POOR scenes, with their figure cells empty, so the
  row count equals the number of scenes in `series.json`.
- `series_monthly.csv` covers every month from the first to the last
  scene month, none skipped; `median_ha` is empty when `n_scenes` is 0.
- `README.txt` is plain ASCII, wrapped at 79 columns, and ends with a
  pointer to `docs/contract.md` for the full field definitions.

The same publish always gives byte-identical files: no wall clock is
read (the README's copyright year is the latest scene's year).

## Columns

Both CSVs start with the five provenance columns, then their own:

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

**Name clash.** `series_sha256` keeps its contract meaning: the hash of
the series run's internal `series.csv`. It is **not** the hash of the
`series.csv` in the bundle; that one is in `SHA256SUMS`.

## Licence

The data in the bundle is released under CC BY 4.0, with the copyright
year of the latest scene; the smolf code stays under the MIT License.
`README.txt` states the licence, a suggested citation (naming the
`run_id`) and the attributions: modified Copernicus Sentinel data
(Sentinel-2 L2A) for the scene years, the SIH water levels (station
ENDHD), and JRC Global Surface Water v1.4 for the reservoir polygon.

## Verification

`SHA256SUMS` is in `sha256sum` format and covers `README.txt`,
`series.csv` and `series_monthly.csv` (not itself). In the bundle
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
the four bundle files; anything else is someone else's data.

## Refusals and exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Exported. |
| 2 | Refusal, one line `refused: <reason>` on stderr; nothing is written and `--out` is left as it was. |
| 1 | Unexpected error (a bug or an I/O failure). |

`export series-csv` refuses (exit 2) when:

- `publish.json`, `series.json` or `latest.json` cannot be read or is not
  valid JSON;
- any of the three fails its packaged schema;
- `publish.json` does not list `series.json` or `latest.json`;
- `series.json` or `latest.json` does not hash to its `publish.json`
  entry;
- `publish.json` or `latest.json` names a different `run_id` or
  `series_sha256` than `series.json`;
- `--out` exists and is not a directory;
- `--out` holds anything other than the four bundle files as regular
  files (another file, a subdirectory or a symlink);
- a symlink in the `--out` path is broken or cannot be resolved, or
  `--out` is missing while several `.<name>.previous-*` directories
  remain (restore one by hand).
