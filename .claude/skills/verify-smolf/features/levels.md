# SIH level record

The level record is the reservoir's daily water level from Mexico's SIH. `smolf levels update` downloads it, validates it against the local copy, and stores it. `smolf levels show <date>` prints one day. With `SMOLF_DATA_DIR` set, the refreshed copy lives at `$SMOLF_DATA_DIR/hydro/ENDHD.csv`, and until it exists every reader falls back to the committed copy.

## Sub-features

- `levels-show-fallback` shows a day from the committed copy in an empty data dir.
- `levels-check` downloads and validates without writing.
- `levels-update` writes the refreshed copy and its `.json` sidecar into the data dir.
- `levels-show-refreshed` shows a day that only the refreshed copy has.
- `levels-temp-cleanup` removes temp files left by a killed refresh, logs each one, and keeps unrelated files.
- `levels-show-missing` reports a day the record does not have and exits 1.

## How to get to it (user POV)

- Run `smolf levels show YYYY-MM-DD`.
- Run `smolf levels update [--check] [--force]`.

## Driving it with smolf-verify

Preconditions:

- Fresh run (empty `$RUN/data/hydro/`). `doctor --net` shows 200 for the SIH.

- **Fallback read.** Run `$V run "$RUN" levels-show-fallback -- levels show 2026-11-18`. `.rc` `0`, with `level_m: 2018.03`. `$RUN/data/hydro/` does not exist.
- **Dry run.** Run `$V run "$RUN" levels-check -- levels update --check`. `.rc` `0`, with `days:`, `last_day_with_level:`, `level_revisions: 0` and `check only: not written`. `$RUN/data/hydro/` still does not exist.
- **Refresh.** Run `$V run "$RUN" levels-update -- levels update`. `.rc` `0`. The last line is `output: $RUN/data/hydro/ENDHD.csv`. Both `ENDHD.csv` and `ENDHD.csv.json` exist there, and the sidecar's `sha256` equals `sha256sum ENDHD.csv`.
- **Refreshed read.** Take `last_day_with_level` from the update output and run `$V run "$RUN" levels-show-refreshed -- levels show <that day>`. `.rc` `0`, with a `level_m:` line. If that day is after 2026-11-18, the committed copy alone could not answer it.
- **Killed-refresh leftovers.** After a refresh, run `echo x > "$RUN/data/hydro/ENDHD.csv.killed01.tmp"; echo x > "$RUN/data/hydro/ENDHD.csv.json.killed02.tmp"; echo x > "$RUN/data/hydro/notes.tmp"`, then `$V run "$RUN" levels-temp-cleanup --log-level INFO -- levels update`. `.rc` `0`. Both `ENDHD.csv*.killed*.tmp` files are gone, `notes.tmp` stays, and `.err` has one `INFO smol_frontier.hydro.levels: removed stale temp file <path>` line per removed file, next to the `SIH download ok` INFO line.
- **Missing day.** Run `$V run "$RUN" levels-show-missing -- levels show 1900-01-01`. `.rc` `1`, `.err` `no level for 1900-01-01`.

## Gotchas

- Without `--log-level INFO` the removal still happens but stderr stays empty. The CLI configures no logging, so an operator sees no trace of it by default.
- The SIH sits behind bot protection. The CLI makes 4 attempts (up to about 4.5 min) and then exits 1 with `error: SIH download failed after 4 attempts: <reason>` on stderr. Report that as unverified, not as a bug.
- The SIH sometimes publishes the same day and sometimes days late. Don't hard-code an expected last day.
- `levels-show-fallback` relies on the committed record ending 2026-11-18. A later committed refresh moves that date, so update the expected value then.
- A rejected refresh (fewer days, an earlier end, a missing day, or a level revision over 0.05 m without `--force`) prints `error: …` on stderr, exits 1 and leaves the old copy. In an empty data dir the baseline is the committed copy.
