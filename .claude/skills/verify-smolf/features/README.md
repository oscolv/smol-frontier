# smolf verification map

This directory is the maintained source for verifying what `smolf` users observe. Read this index, then use the matching feature file as the recipe.

## Baseline preconditions

- A run created with `smolf-verify new --ref <commit> --shared-cache`, so the checkout is read-only and the cache is warm.
- `smolf-verify doctor "$RUN" --net` reports `git: clean`, `checkout: read-only`, and 200 for every catalog the feature uses.
- Never point a run at someone else's `SMOLF_DATA_DIR`, and never run two `series build` runs on one shared cache at once.

## Driving conventions

- Every command goes through `smolf-verify run "$RUN" <feature-id> -- <args>`. Use the sub-feature ID as `<feature-id>`.
- Commands are literal. Keep dates and flags unchanged: the expected values depend on them.
- Read the exit code from the recorded `.rc`, the figures from the `.out` lines (`key: value`).
- Steps that call the network (STAC, SIH) can fail for outside reasons. Report them as unverified with the recorded stderr. Don't retry with a different path and call it verified.

## Proof and skip reporting

- CLI proof is the `.cmd/.out/.err/.rc` quartet that `run` writes.
- State proof is a second, read-only view: `smolf-verify hectares` on a written map, `levels show` after an update, manifest or `latest.json` fields copied into the evidence dir.
- For read-only runs, also prove the checkout is untouched (git status and a `find -newer` listing).
- Record which entry point you drove. A skipped entry point is reported as skipped, not as covered by another one.

## Feature entry contract

Each feature file has an H1, one paragraph, then exactly four H2s: `Sub-features`, `How to get to it (user POV)`, `Driving it with smolf-verify`, `Gotchas`.

## Features

- [Classify one day](./classify.md): classified map and hectares for one Sentinel-2 date, with the control values.
- [SIH level record](./levels.md): refresh, dry run, show one day, first refresh into an empty data dir.
- [Series build](./series-build.md): the resumable series run, `--extend`, and the 0/2/1/130 exit codes.
- [Publish the contract](./publish.md): `latest.json`, `series.json`, `publish.json`, the latest map and the exposed-bed example maps, with its refusals.
- [Downloads](./export.md): `smolf export series-csv` (bundle with the reservoir outline zip) and `smolf export maps` (one map per scene) from a finished publish of the same release, with their refusals.
- [Daily job](./daily-job.md): `levels update` then `series build --extend` then `publish`, from a read-only checkout.

Not mapped yet: `smolf fetch`, `smolf gate identity` (needs ~468 scenes and writes a report), `smolf hydro curve` and `smolf hydro flood-map` (maintenance commands that write committed inputs; never run them from a verification run).
