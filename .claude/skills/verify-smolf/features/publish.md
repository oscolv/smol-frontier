# Publish the contract

`smolf publish` turns the newest complete, clean series run into the versioned data contract for downstream consumers: `latest.json`, `series.json`, `publish.json` (sha256 of every other file) and the latest scene's map. The output directory is replaced atomically. Refusals exit 2 and leave the previous output untouched. A contract that fails its own schema exits 1.

## Sub-features

- `publish-default` publishes the newest complete run to `$SMOLF_DATA_DIR/outputs/publish/`.
- `publish-out` publishes to an explicit `--out` directory.
- `publish-refuse-foreign` refuses an `--out` that holds unrelated files.

## How to get to it (user POV)

- Run `smolf publish [--run <run_id>] [--out <dir>]`.

## Driving it with smolf-verify

Preconditions:

- A complete, clean series run in this run's data dir (see series-build.md). The latest scene's cache file must still match the run's recorded hash.

- **Default.** Run `$V run "$RUN" publish-default -- publish`. `.rc` `0` and `published: $RUN/data/outputs/publish/latest.json`. Copy `latest.json` and `publish.json` into the evidence. `latest.json` has `current_level.date` equal to the refreshed record's last day with a level, and `current_level.levels_sha256` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`. Every hash in `publish.json` matches `sha256sum` of the file it names.
- **Explicit out.** Run `$V run "$RUN" publish-out -- publish --out "$RUN/data/alt-publish"`. `.rc` `0`, and the files land there, not in the default directory.
- **Foreign out.** `mkdir -p "$RUN/data/foreign" && echo x > "$RUN/data/foreign/notes.txt"`, then run `$V run "$RUN" publish-refuse-foreign -- publish --out "$RUN/data/foreign"`. `.rc` `2`, with one `refused:` line. `notes.txt` is still there.

## Gotchas

- `refused: run … was built from a dirty tree` means the checkout was dirty during `series build`. Fix the checkout and rebuild. Never edit the manifest.
- Publishing the same run with the same level record gives byte-identical JSON. After a level refresh, only `current_level` (and `publish.json`) change.
- The map rebuild re-reads the cached scene and checks its hash. A cache file changed since the build makes publish refuse.
