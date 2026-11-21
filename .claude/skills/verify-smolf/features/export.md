# Download bundle

`smolf export series-csv` turns a finished publish into the download bundle: `series.csv` (one row per `series.json` scene, POOR scenes included), `series_monthly.csv` (one row per `monthly` entry), `README.txt` (English, ASCII), `LEEME.txt` (Spanish, UTF-8) and `SHA256SUMS` (over the other four, `sha256sum -c` format). It reads only `publish.json`, `series.json` and `latest.json` from the publish dir and never touches the network. Before writing, it checks that `publish.json` passes its schema, that the other two hash to their `publish.json` entries and pass theirs, and that all three name the same run. The output directory is replaced atomically. Refusals exit 2 with one `refused: …` line on stderr, an empty stdout, and nothing written. The bundle is outside the data contract (`docs/downloads.md`).

## Sub-features

- `export-default` writes the bundle from the default publish dir into `$SMOLF_DATA_DIR/outputs/downloads/`.
- `export-again` rewrites the same bundle byte for byte.
- `export-out` reads an explicit `--from` and writes an explicit `--out`.
- `export-tamper` refuses a publish whose `series.json` no longer matches `publish.json`.
- `export-old-contract` refuses a publish from an older minor contract that lacks a property the packaged schemas require.
- `export-no-publish` refuses a `--from` with no `publish.json`.
- `export-foreign` refuses an `--out` that holds a file the export does not own.
- `export-inside` refuses an `--out` inside the publish dir.

## How to get to it (user POV)

- Run `smolf export series-csv [--from <publish dir>] [--out <dir>]` after `smolf publish`.

## Driving it with smolf-verify

Preconditions:

- A successful default publish in this run, written by the same smolf release (see publish.md). No network needed; each command takes seconds.
- `P=$RUN/data/outputs/publish` and `D=$RUN/data/outputs/downloads` below.

- **Default.** Run `$V run "$RUN" export-default -- export series-csv`. `.rc` `0`, `.out` is `exported: $RUN/data/outputs/downloads`, `.err` empty. `ls $D` shows exactly `LEEME.txt README.txt SHA256SUMS series.csv series_monthly.csv`, and `(cd $D && sha256sum -c SHA256SUMS)` gives 4 `OK`. `tail -n +2 $D/series.csv | wc -l` equals `jq '.scenes|length' $P/series.json` (471 for run `20261121T021625`, v0.10.0, 2026-11-21) and `tail -n +2 $D/series_monthly.csv | wc -l` equals `jq '.monthly|length' $P/series.json` (118). The headers have 31 and 10 columns (`head -1 … | tr , '\n' | wc -l`). Every row's third column is `sha256sum $P/series.json`, and columns 1, 4–7 are the run id, the `schema_version` of `series.json` (`3.4.0`), the publishing `smolf_version` (`0.10.0`), `CC-BY-4.0` and `Oscar Olvera Neria (UAM Azcapotzalco)`. `file` reports `README.txt` as ASCII and `LEEME.txt` as UTF-8, and `README.txt` has an `exported by smolf: <version>` line. Copy `$D` into the evidence before cleanup.
- **Again.** Save `sha256sum $D/*`, run `$V run "$RUN" export-again -- export series-csv`, and compare: every hash is identical.
- **Explicit paths.** Run `$V run "$RUN" export-out -- export series-csv --from "$P" --out "$RUN/data/dl2"`. `.rc` `0`, `.out` `exported: $RUN/data/dl2`, and every file is byte-identical to `$D`.
- **Tampered publish.** `cp -r "$P" "$RUN/data/tampered" && echo >> "$RUN/data/tampered/series.json"`, then run `$V run "$RUN" export-tamper -- export series-csv --from "$RUN/data/tampered" --out "$RUN/data/dl-t"`. `.rc` `2`, `.out` empty, `.err` `refused: $RUN/data/tampered/series.json does not match its publish.json entry (sha256 <new> != <the series.json sha256 in publish.json>)`, and `$RUN/data/dl-t` does not exist.
- **Older contract.** Fake a 3.3.0 publish that is still internally consistent: `cp -r "$P" "$RUN/data/old" && jq '.schema_version="3.3.0" | del(.stage_area_curve, .exposed_bed_example)' "$P/series.json" > "$RUN/data/old/series.json"`, then re-hash it in the copy's index: `H=$(sha256sum "$RUN/data/old/series.json" | cut -d' ' -f1); jq --arg h "$H" '(.files[] | select(.path=="series.json") | .sha256) = $h' "$P/publish.json" > "$RUN/data/old/publish.json"`. Run `$V run "$RUN" export-old-contract -- export series-csv --from "$RUN/data/old" --out "$RUN/data/dl-o"`. `.rc` `2`, `.out` empty, `.err` exactly `refused: series.json fails its schema: <root>: 'stage_area_curve' is a required property; <root>: 'exposed_bed_example' is a required property`, and `$RUN/data/dl-o` does not exist (measured 2026-11-21).
- **No publish.** Run `$V run "$RUN" export-no-publish -- export series-csv --from "$RUN/data/nope" --out "$RUN/data/dl-n"`. `.rc` `2`, `.err` `refused: cannot read $RUN/data/nope/publish.json: No such file or directory`.
- **Foreign file.** `echo x > $D/notes.txt`, then run `$V run "$RUN" export-foreign -- export series-csv`. `.rc` `2`, `.err` `refused: --out $RUN/data/outputs/downloads holds a file the export does not own: notes.txt`. The five bundle files keep their hashes. Remove `notes.txt` afterwards.
- **Inside the publish dir.** Hash `$P/publish.json`, then run `$V run "$RUN" export-inside -- export series-csv --out "$P/dl"`. `.rc` `2`, `.err` `refused: --out $P/dl is inside the publish directory $RUN/data/outputs/publish`. `$P/dl` does not exist and `publish.json` keeps its hash.
- After the refusals, `ls -a $RUN/data/outputs` shows no `.downloads.*` staging entry.

## Gotchas

- The input checks run before any `--out` check, so a bad `--out` is reported only when the publish itself is valid. A tampered `series.json` hits the hash check before the schema check.
- `exported:` echoes `--out` as given (normalised by `Path`: no trailing slash or `./`), not resolved.
- The bundle files are not in `publish.json`. Export never changes the publish, so its `sha256sum -c` still passes afterwards.
- `--out` refusals for symlinked or several-previous dirs reuse publish's wording (`… several previous publishes remain …`).
- After upgrading smolf, re-run `smolf publish` before exporting: export validates against the packaged schemas, so a publish from an older minor release (3.3.0 lacks `stage_area_curve` and `exposed_bed_example`) is refused with `series.json fails its schema`. The message names the file without its path, unlike the hash refusals.
- The daily job does not run export (see daily-job.md). A new publish leaves the old bundle in place until export runs again.
