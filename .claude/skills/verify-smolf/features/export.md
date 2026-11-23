# Downloads (`smolf export`)

`smolf export series-csv` turns a finished publish into the download bundle: `series.csv` (one row per `series.json` scene, POOR scenes included), `series_monthly.csv` (one row per `monthly` entry), `README.txt` (English, ASCII), `LEEME.txt` (Spanish, UTF-8), `reservoir_outline.zip` (the reservoir polygon as a shapefile, since v0.11.0) and `SHA256SUMS` (over the other five, `sha256sum -c` format). It reads `publish.json`, `series.json` and `latest.json` from the publish dir plus the committed AOI polygon, and never touches the network. Before writing, it checks that `publish.json` passes its schema, that the other two hash to their `publish.json` entries and pass theirs, and that all three name the same run. The output directory is replaced atomically. Refusals exit 2 with one `refused: …` line on stderr, an empty stdout, and nothing written. `smolf export maps` (since v0.11.0) writes one classified GeoTIFF per scene with figures into `outputs/maps/`, rebuilt from the run behind the publish and the scene cache, plus `index.csv`, `README.txt` and `SHA256SUMS`; it refuses the same way. Both are outside the data contract (`docs/downloads.md`).

## Sub-features

- `export-default` writes the bundle from the default publish dir into `$SMOLF_DATA_DIR/outputs/downloads/`.
- `export-again` rewrites the same bundle byte for byte.
- `export-out` reads an explicit `--from` and writes an explicit `--out`.
- `export-tamper` refuses a publish whose `series.json` no longer matches `publish.json`.
- `export-old-contract` refuses a publish from an older minor contract that lacks a property the packaged schemas require.
- `export-no-publish` refuses a `--from` with no `publish.json`.
- `export-foreign` refuses an `--out` that holds a file the export does not own.
- `export-inside` refuses an `--out` inside the publish dir.
- `maps-default` writes every scene map from the default publish dir into `$SMOLF_DATA_DIR/outputs/maps/`.
- `maps-out` writes them to an explicit `--out`, byte-identical to the default export.
- `maps-inside`, `maps-tamper` and `maps-no-run` refuse an `--out` inside the publish dir, a tampered publish, and a publish whose run is not in the data dir.
- `maps-foreign` refuses an `--out` that holds a file the export does not own.

## How to get to it (user POV)

- Run `smolf export series-csv [--from <publish dir>] [--out <dir>]` after `smolf publish`.
- Run `smolf export maps [--from <publish dir>] [--out <dir>]` after `smolf publish`, in the data dir that holds the published run and its scene cache.

## Driving it with smolf-verify

Preconditions:

- A successful default publish in this run, written by the same smolf release (see publish.md). `series-csv` needs no network and takes seconds. `maps` needs the series run directory and the scene cache in this data dir (`--shared-cache`); with the cache warm it makes no network call, but takes about 6 min for 469 maps.
- `P=$RUN/data/outputs/publish` and `D=$RUN/data/outputs/downloads` below.

- **Default.** Run `$V run "$RUN" export-default -- export series-csv`. `.rc` `0`, `.out` is `exported: $RUN/data/outputs/downloads`, `.err` empty. `ls $D` shows exactly `LEEME.txt README.txt SHA256SUMS reservoir_outline.zip series.csv series_monthly.csv`, and `(cd $D && sha256sum -c SHA256SUMS)` gives 5 `OK`. `tail -n +2 $D/series.csv | wc -l` equals `jq '.scenes|length' $P/series.json` (472 for run `20261123T022429`, v0.11.0, 2026-11-23, and for `20261122T162250`) and `tail -n +2 $D/series_monthly.csv | wc -l` equals `jq '.monthly|length' $P/series.json` (118). The headers have 31 and 10 columns (`head -1 … | tr , '\n' | wc -l`). Every row's third column is `sha256sum $P/series.json`, and columns 1, 4–7 are the run id, the `schema_version` of `series.json` (`3.4.0`), the publishing `smolf_version` (`0.11.0`), `CC-BY-4.0` and `Oscar Olvera Neria (UAM Azcapotzalco)`. `file` reports `README.txt` as ASCII and `LEEME.txt` as UTF-8, and `README.txt` has an `exported by smolf: <version>` line. `unzip -l $D/reservoir_outline.zip` lists `reservoir_outline.shp .shx .dbf .prj .cpg` and `README.txt`; unzipped and read with the checkout's Python (`geopandas.read_file`), the shapefile is EPSG:4326, one feature, columns `area_ha` and `geometry`, `area_ha` `1261.75` = `latest.json` `.reservoir_area_ha`. Copy `$D` into the evidence before cleanup.
- **Again.** Save `sha256sum $D/*`, run `$V run "$RUN" export-again -- export series-csv`, and compare: every hash is identical, the zip's included.
- **Explicit paths.** Run `$V run "$RUN" export-out -- export series-csv --from "$P" --out "$RUN/data/dl2"`. `.rc` `0`, `.out` `exported: $RUN/data/dl2`, and every file is byte-identical to `$D`.
- **Tampered publish.** `cp -r "$P" "$RUN/data/tampered" && echo >> "$RUN/data/tampered/series.json"`, then run `$V run "$RUN" export-tamper -- export series-csv --from "$RUN/data/tampered" --out "$RUN/data/dl-t"`. `.rc` `2`, `.out` empty, `.err` `refused: $RUN/data/tampered/series.json does not match its publish.json entry (sha256 <new> != <the series.json sha256 in publish.json>)`, and `$RUN/data/dl-t` does not exist.
- **Older contract.** Fake a 3.3.0 publish that is still internally consistent: `cp -r "$P" "$RUN/data/old" && jq '.schema_version="3.3.0" | del(.stage_area_curve, .exposed_bed_example)' "$P/series.json" > "$RUN/data/old/series.json"`, then re-hash it in the copy's index: `H=$(sha256sum "$RUN/data/old/series.json" | cut -d' ' -f1); jq --arg h "$H" '(.files[] | select(.path=="series.json") | .sha256) = $h' "$P/publish.json" > "$RUN/data/old/publish.json"`. Run `$V run "$RUN" export-old-contract -- export series-csv --from "$RUN/data/old" --out "$RUN/data/dl-o"`. `.rc` `2`, `.out` empty, `.err` exactly `refused: series.json fails its schema: <root>: 'stage_area_curve' is a required property; <root>: 'exposed_bed_example' is a required property`, and `$RUN/data/dl-o` does not exist (measured 2026-11-21).
- **No publish.** Run `$V run "$RUN" export-no-publish -- export series-csv --from "$RUN/data/nope" --out "$RUN/data/dl-n"`. `.rc` `2`, `.err` `refused: cannot read $RUN/data/nope/publish.json: No such file or directory`.
- **Foreign file.** `echo x > $D/notes.txt`, then run `$V run "$RUN" export-foreign -- export series-csv`. `.rc` `2`, `.err` `refused: --out $RUN/data/outputs/downloads holds a file the export does not own: notes.txt`. The six bundle files keep their hashes. Remove `notes.txt` afterwards.
- **Inside the publish dir.** Hash `$P/publish.json`, then run `$V run "$RUN" export-inside -- export series-csv --out "$P/dl"`. `.rc` `2`, `.err` `refused: --out $P/dl is inside the publish directory $RUN/data/outputs/publish`. `$P/dl` does not exist and `publish.json` keeps its hash.
- After the refusals, `ls -a $RUN/data $RUN/data/outputs` shows no `.downloads.*`, `.dl-*.*` or other staging entry (the explicit `--out` refusals would stage next to their target in `$RUN/data`).

Maps (`M=$RUN/data/outputs/maps`):

- **Default.** Run `$V run "$RUN" maps-default -- export maps`. `.rc` `0`, `.out` `exported: $RUN/data/outputs/maps`, `.err` only `boa_offset_applied …` guard lines (12 on 2026-11-23). Wall 5:44 and about 420 MB peak for 469 maps of run `20261123T022429` (v0.11.0, 2026-11-23), 7.6 MB in all. `ls $M` holds the `<date>_<item_id>.tif` files plus `index.csv README.txt SHA256SUMS`; the number of `.tif` files, `tail -n +2 $M/index.csv | wc -l` and `jq '[.scenes[]|select(.hyacinth_raw_ha!=null)]|length' $P/series.json` are equal (469). `(cd $M && sha256sum -c SHA256SUMS)` passes, one line per map plus `index.csv` and `README.txt` (471). The `index.csv` header is `date,item_id,file,sha256,bytes,run_id,series_sha256,series_json_sha256`, and every row's last column is `sha256sum $P/series.json`. The latest scene's map is byte-identical to `$P/map/<date>_<item_id>.tif` (`cmp`). `$V hectares "$RUN" $M/2024-06-09_S2A_14QMH_20240609_0_L2A.tif` gives `dense_hyacinth_ha: 4.20`, `interface_ha: 11.46`, `bed_removed_ha: 418.69`, that scene's `dense_corrected_ha`, `interface_corrected_ha` and `removed_ha` in `series.json`. `README.txt` is ASCII and says `(469 maps)`.
- **Explicit paths.** Run `$V run "$RUN" maps-out -- export maps --from "$P" --out "$RUN/data/m2"`. `.rc` `0`, `.out` `exported: $RUN/data/m2`, and `diff <(cd $M && sha256sum *) <(cd $RUN/data/m2 && sha256sum *)` is empty (the same run and cache give the same bytes).
- **Inside the publish dir.** Run `$V run "$RUN" maps-inside -- export maps --out "$P/m"`. `.rc` `2`, `.err` `refused: --out $P/m is inside the publish directory $RUN/data/outputs/publish`, at once.
- **Tampered publish.** With the tampered copy from above, run `$V run "$RUN" maps-tamper -- export maps --from "$RUN/data/tampered" --out "$RUN/data/m-t"`. `.rc` `2`, the same `does not match its publish.json entry` line as `export-tamper`.
- **Run not in the data dir.** `cp -r "$P" "$RUN/data/norun"`, set `.run_id="19990101T000000"` in the copy's `series.json`, then re-hash it into the copy's `publish.json` and set its `.run_id` too (`H=$(sha256sum "$RUN/data/norun/series.json" | cut -d' ' -f1); jq --arg h "$H" '.run_id="19990101T000000" | (.files[] | select(.path=="series.json") | .sha256) = $h' "$P/publish.json" > "$RUN/data/norun/publish.json"`). Run `$V run "$RUN" maps-no-run -- export maps --from "$RUN/data/norun" --out "$RUN/data/m-n"`. `.rc` `2`, `.err` `refused: run 19990101T000000 of the publish is not under $RUN/data/outputs/series`.
- **Foreign file.** Save `(cd $M && sha256sum *)`, `echo x > $M/notes.txt`, then run `$V run "$RUN" maps-foreign -- export maps`. `.rc` `2`, `.err` `refused: --out $RUN/data/outputs/maps holds a file the export does not own: notes.txt`, and every saved hash still matches. It refuses only after rebuilding every map (6:12 on 2026-11-23). Remove `notes.txt` afterwards.
- None of these leaves a `.maps.*`, `.m2.*` or `.m-*.*` staging entry in `$RUN/data` or `$RUN/data/outputs`, and `$P/m`, `$RUN/data/m-t` and `$RUN/data/m-n` do not exist.

## Gotchas

- The input checks run before any `--out` check, so a bad `--out` is reported only when the publish itself is valid. A tampered `series.json` hits the hash check before the schema check.
- `exported:` echoes `--out` as given (normalised by `Path`: no trailing slash or `./`), not resolved.
- The bundle files are not in `publish.json`. Export never changes the publish, so its `sha256sum -c` still passes afterwards.
- `--out` refusals for symlinked or several-previous dirs reuse publish's wording (`… several previous publishes remain …`).
- After upgrading smolf, re-run `smolf publish` before exporting: export validates against the packaged schemas, so a publish from an older minor release (3.3.0 lacks `stage_area_curve` and `exposed_bed_example`) is refused with `series.json fails its schema`. The message names the file without its path, unlike the hash refusals.
- `export maps` resolves the run as `$SMOLF_DATA_DIR/outputs/series/<run_id>` whatever `--from` says, and rebuilds through the publish's own map builder, so its other refusals (`run … is not complete`, `… series_sha256 … differs from the publish series_sha256`, `cache sha256 of <item> …`, `re-classified <item> differs from the run …`, `map of <item> differs from series.json …`) need an edited run dir or a changed cache. Never tamper with the shared cache; the test suite covers them.
- `export maps` checks `--out` for foreign files only when it writes, after the full rebuild, unlike `series-csv` and `publish`, which check before any work. The inside-the-publish check runs first.
- POOR scenes have no figures and get no map, so `index.csv` has fewer rows than `series.csv`.
- The daily job runs neither export (see daily-job.md). A new publish leaves the old bundle and maps in place until each export runs again.
