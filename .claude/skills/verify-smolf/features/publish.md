# Publish the contract

`smolf publish` turns one complete, clean series run (named with `--run <id>`, or the newest with `--latest`) into the versioned data contract for downstream consumers: `latest.json`, `series.json`, `publish.json` (sha256 of every other file) and the latest scene's map as `map/<date>_<item_id>.tif` plus a display `.png`. The output directory is replaced atomically. Refusals exit 2 with one line on stderr and leave the previous output untouched. A contract that fails its own schema exits 1.

## Sub-features

- `publish-default` publishes the newest complete run (`--latest`) to `$SMOLF_DATA_DIR/outputs/publish/`.
- `publish-no-choice` refuses when neither `--run` nor `--latest` is given.
- `publish-out` publishes to an explicit `--out` directory.
- `publish-refuse-foreign` refuses an `--out` that holds unrelated files.
- `publish-unknown-run` refuses a `--run` id that does not exist.

## How to get to it (user POV)

- Run `smolf publish (--run <run_id> | --latest) [--out <dir>]`.

## Driving it with smolf-verify

Preconditions:

- A complete, clean series run in this run's data dir (see series-build.md). The latest scene's cache file must still match the run's recorded hash.

- **Default.** Run `$V run "$RUN" publish-default -- publish --latest`. `.rc` `0` and `published: $RUN/data/outputs/publish/latest.json`. Copy `latest.json` and `publish.json` into the evidence. `latest.json` has `current_level.date` equal to the refreshed record's last day with a level, and `current_level.levels_sha256` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`. Every hash in `publish.json` matches `sha256sum` of the file it names.
- **No choice.** Run `$V run "$RUN" publish-no-choice -- publish`. `.rc` `2`, `.err` is `error: pass --run <run_id> or --latest`, `.out` is empty, and the publish dir is unchanged.
- **Explicit out.** Run `$V run "$RUN" publish-out -- publish --latest --out "$RUN/data/alt-publish"`. `.rc` `0`, and the files land there, not in the default directory.
- **Foreign out.** `mkdir -p "$RUN/data/foreign" && echo x > "$RUN/data/foreign/notes.txt"`, then run `$V run "$RUN" publish-refuse-foreign -- publish --latest --out "$RUN/data/foreign"`. `.rc` `2`, with one `refused: --out … is not a previous publish …` line in `.err`. `notes.txt` is still there.
- **Unknown run.** Run `$V run "$RUN" publish-unknown-run -- publish --run nope`. `.rc` `2`, `.err` `refused: unknown run: nope`.
- **Determinism.** Copy `latest.json` and `series.json` aside, run `$V run "$RUN" publish-again -- publish --latest`, and `cmp` them: both identical. `publish.json` differs (it holds `generated_utc`).

## Gotchas

- `refused: run … was built from a dirty tree` means the checkout was dirty during `series build`. Fix the checkout and rebuild. Never edit the manifest.
- `--latest` takes the newest complete run by id. A later run in the same data dir (for example the reference-only run of a resume test) wins over the `--extend` run, and `latest.json` then shows the reference series' last scene. Use `--run <id>` when the data dir holds more than the run you mean.
- `current_level` has `date`, `level_m` and `levels_sha256`, read from the data-dir record when it exists, else the committed one. It is `null` when there is no record with a level.
- Publishing the same run with the same level record gives byte-identical `latest.json` and `series.json`. After a level refresh, only `current_level` (and `publish.json`) change.
- The map rebuild re-reads the cached scene and checks its hash. A cache file changed since the build makes publish refuse. A missing cache file is re-downloaded by its exact item id first (network).
