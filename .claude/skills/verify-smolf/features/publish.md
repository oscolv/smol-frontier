# Publish the contract

`smolf publish` turns one complete, clean series run (named with `--run <id>`, or the newest with `--latest`) into the versioned data contract for downstream consumers: `latest.json`, `series.json`, `publish.json` (sha256 of every other file) and the latest scene's map as `map/<date>_<item_id>.tif` plus a display `.png`. The output directory is replaced atomically. Refusals exit 2 with one line on stderr and keep the previous contract. Before any check, publish recovers its own crash leftovers next to `--out` (a lone `.<name>.previous-*` is renamed back, allowlisted `.<name>.staging-*` dirs are removed), even when it then refuses. A contract that fails its own schema exits 1.

## Sub-features

- `publish-default` publishes the newest complete run (`--latest`) to `$SMOLF_DATA_DIR/outputs/publish/`.
- `publish-no-choice` refuses when neither `--run` nor `--latest` is given.
- `publish-both` refuses when both `--run` and `--latest` are given.
- `publish-out` publishes to an explicit `--out` directory.
- `publish-empty-out` accepts an existing empty `--out` directory.
- `publish-crash-recover` recovers from a crash between the swap's two renames.
- `publish-several-previous` refuses when `--out` is missing and several previous publishes remain.
- `publish-link` publishes through a symlinked `--out` into its target and keeps the link.
- `publish-out-file` refuses an `--out` that is a regular file.
- `publish-refuse-foreign` refuses an `--out` that holds unrelated files.
- `publish-unknown-run` refuses a `--run` id that does not exist.

## How to get to it (user POV)

- Run `smolf publish (--run <run_id> | --latest) [--out <dir>]`.

## Driving it with smolf-verify

Preconditions:

- A complete, clean series run in this run's data dir (see series-build.md). The latest scene's cache file must still match the run's recorded hash.

- **Default.** Run `$V run "$RUN" publish-default -- publish --latest`. `.rc` `0` and `published: $RUN/data/outputs/publish/latest.json`. Copy `latest.json` and `publish.json` into the evidence. `latest.json` has `current_level.date` equal to the refreshed record's last day with a level, and `current_level.levels_sha256` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`. Every hash in `publish.json` matches `sha256sum` of the file it names. All three JSON files carry `schema_version` `3.0.0` and `smolf_version` `0.5.0` (contract 3.0.0, tag `v0.5.0`); `latest.json` has `reservoir_outline` of `type` `MultiPolygon`; in `series.json`, any scene with `bed_applied: false` has `removed_ha` and every `*_corrected` field `null`. Every scene of `series.json` and `latest.json` has `display_status`: `HIDDEN` when `quality_light` is not `GOOD` or `hyacinth_corrected_ha` is `null`, else `LOWER_BOUND` when `valid_frac` < `constants.frac_publishable`, else `FULL`. Check it with `jq '[.scenes[] | select((.quality_light != "GOOD" or .hyacinth_corrected_ha == null) != (.display_status == "HIDDEN"))] | length' series.json` (expect `0`) and the same shape for `LOWER_BOUND`. Every scene's `display_ha` is `null` exactly when `hyacinth_corrected_ha` is, and otherwise its `dense + interface` equals `total`: `jq '[.scenes[] | select((.display_ha == null) != (.hyacinth_corrected_ha == null) or (.display_ha != null and .display_ha.dense + .display_ha.interface != .display_ha.total))] | length' series.json` (expect `0`). `latest.json` `constants.green_max_ha` is `378.53` and `red_min_ha` `630.88`.
- **No choice.** Run `$V run "$RUN" publish-no-choice -- publish`. `.rc` `2`, `.err` is `error: pass --run <run_id> or --latest`, `.out` is empty, and the publish dir is unchanged.
- **Both flags.** Run `$V run "$RUN" publish-both -- publish --run x --latest`. `.rc` `2`, `.err` is `error: pass either --run or --latest, not both`, `.out` is empty.
- **Explicit out.** Run `$V run "$RUN" publish-out -- publish --latest --out "$RUN/data/alt-publish"`. `.rc` `0`, and the files land there, not in the default directory.
- **Empty out.** `mkdir -p "$RUN/data/empty"`, then run `$V run "$RUN" publish-empty-out -- publish --latest --out "$RUN/data/empty"`. `.rc` `0`, and the four contract entries (`latest.json`, `map`, `publish.json`, `series.json`) land there.
- **Crash recovery.** Publish once to `--out "$RUN/data/pub"`. Then fake a crash mid-swap: `mv "$RUN/data/pub" "$RUN/data/.pub.previous-.pub.staging-crash1"`, `mkdir "$RUN/data/.pub.staging-crash1"` and copy `latest.json` into it. Run `$V run "$RUN" publish-crash-recover -- publish --latest --out "$RUN/data/pub"`. `.rc` `0`, `pub` holds a fresh contract, and no `.pub.*` entry remains. Repeat the fake crash and run `-- publish --run nope --out "$RUN/data/pub"`: `.rc` `2` (`refused: unknown run: nope`), yet `pub` is restored with the same `latest.json` (same sha256) and the staging dir is gone.
- **Several previous.** With `pub` missing and two dirs `.pub.previous-.pub.staging-a1` and `.pub.previous-.pub.staging-b2`, run `$V run "$RUN" publish-several-previous -- publish --latest --out "$RUN/data/pub"`. `.rc` `2`, `.err` `refused: --out $RUN/data/pub is missing and several previous publishes remain (.pub.previous-.pub.staging-a1, .pub.previous-.pub.staging-b2); restore one by hand`, and both dirs are left as they were.
- **Symlinked out.** After Explicit out, run `ln -s alt-publish "$RUN/data/link"`, then `$V run "$RUN" publish-link -- publish --latest --out "$RUN/data/link"`. `.rc` `0` (`published: $RUN/data/link/latest.json`), `link` is still a symlink, and the files are refreshed in `alt-publish`.
- **File as out.** `touch "$RUN/data/afile"`, then run `$V run "$RUN" publish-out-file -- publish --latest --out "$RUN/data/afile"`. `.rc` `2`, `.err` `refused: --out $RUN/data/afile exists and is not a directory`.
- **Foreign out.** `mkdir -p "$RUN/data/foreign" && echo x > "$RUN/data/foreign/notes.txt"`, then run `$V run "$RUN" publish-refuse-foreign -- publish --latest --out "$RUN/data/foreign"`. `.rc` `2`, `.err` `refused: --out $RUN/data/foreign is not a previous publish (no publish.json or files outside the allowlist)`. `notes.txt` is still there.
- **Unknown run.** Run `$V run "$RUN" publish-unknown-run -- publish --run nope`. `.rc` `2`, `.err` `refused: unknown run: nope`.
- **Determinism.** Copy `latest.json` and `series.json` aside, run `$V run "$RUN" publish-again -- publish --latest`, and `cmp` them: both identical. `publish.json` differs (it holds `generated_utc`).

## Gotchas

- `refused: run … was built from a dirty tree` means the checkout was dirty during `series build`. Fix the checkout and rebuild. Never edit the manifest.
- `--latest` takes the newest complete run by id. A later run in the same data dir (for example the reference-only run of a resume test) wins over the `--extend` run, and `latest.json` then shows the reference series' last scene. Use `--run <id>` when the data dir holds more than the run you mean.
- `current_level` has `date`, `level_m` and `levels_sha256`, read from the data-dir record when it exists, else the committed one. It is `null` when there is no record with a level.
- Publishing the same run with the same level record gives byte-identical `latest.json` and `series.json`. After a level refresh, only `current_level` (and `publish.json`) change.
- The map rebuild re-reads the cached scene and checks its hash. A cache file changed since the build makes publish refuse. A missing cache file is re-downloaded by its exact item id first (network).
