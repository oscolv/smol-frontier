# Daily job

The scheduled daily job is three commands in order: `smolf levels update`, then `smolf series build --extend`, then `smolf publish --latest`. It runs from a read-only code checkout with every write under `SMOLF_DATA_DIR`. It must finish with a fresh contract and leave the checkout untouched, and a misconfigured data dir must fail fast.

## Sub-features

- `daily-bad-data-dir` rejects a relative or in-checkout `SMOLF_DATA_DIR` with exit 1 (two different messages).
- `daily-levels`, `daily-build` and `daily-publish` are the three steps.
- `daily-untouched` proves the checkout did not change.

## How to get to it (user POV)

- A scheduler runs, with `SMOLF_DATA_DIR=<absolute dir outside the checkout>`:
  `smolf levels update && smolf series build --extend && smolf publish --latest`. Exit 2 from the build means retry later (stale level record or Earth Search unreachable; nothing written). `levels update` exits 1 when the SIH download fails, even for a passing outage, and also when it rejects the download: a parse failure, fewer days, an earlier last day or a missing day (no retry and no flag fixes these), or a level revision over 0.05 m (only `--force` accepts it). `publish` exits 2 when it refuses a run or cannot read a Sentinel-2 photo (network outage; retry later).

## Driving it with smolf-verify

Preconditions:

- A run made with `new --ref <commit> --shared-cache`. `doctor --net` shows `checkout: read-only`, `git: clean`, and 200 for all three hosts.

- **Stamp.** Run `touch "$RUN/evidence/stamp"` before the first step.
- **Bad data dir.** Run `(. "$RUN/env"; cd /tmp; SMOLF_DATA_DIR=rel "$SMOLF" levels show 2026-11-05; echo rc=$?)`. You get `error: SMOLF_DATA_DIR='rel' must be an absolute path outside the checkout` and `rc=1`. Repeat with `SMOLF_DATA_DIR="$SMOLF_CODE/var"`: `error: SMOLF_DATA_DIR='…/checkout/var' must be outside the checkout (…/checkout)` and `rc=1`. Save both outputs to `$RUN/evidence/`.
- **Levels.** Run `$V run "$RUN" daily-levels -- levels update`. `.rc` `0`, with `output: $RUN/data/hydro/ENDHD.csv`.
- **Build.** Run `$V run "$RUN" daily-build -- series build --extend --workers 4`. `.rc` `0`, with `errors=0`. Measured 2026-11-27 against the v2026-11-19 reference: `requested=472 ok=469 skipped=3` (run `20261127T233656`, v0.14.0; the same on 2026-11-25, run `20261125T021642`, v0.12.0; the same on 2026-11-23, run `20261123T030410`, v0.11.0; the same at v0.10.0 on 2026-11-22; 471/469/2 before the 2026-11-21 scene appeared, at v0.10.0, v0.9.0 and v0.8.1; 0.8.0 gave 469). Wall 3:24 with a warm shared cache on 2026-11-27 (3:47 on 2026-11-25, 3:19 on 2026-11-23, 2:57 on 2026-11-22, 3:15 on 2026-11-21). `requested` grows as new scenes appear, so treat it as a floor.
- **Publish.** Run `$V run "$RUN" daily-publish -- publish --latest`. `.rc` `0`, about 1.5–5 min (221 s on 2026-11-27 at v0.14.0, 122 s on 2026-11-25 at v0.12.0, 76 s on 2026-11-23 at v0.11.0, 112 s and later 201 s on 2026-11-22 at v0.10.0; it reads the Sentinel-2 photos over the network and re-classifies the cached 2024-06-09 example scene). In `latest.json`, `current_level.levels_sha256` equals the hash of `$RUN/data/hydro/ENDHD.csv`, `schema_version` is `3.4.0` and `smolf_version` the release under test (`0.14.0` at `v0.14.0`) in all three JSON files, `image/` holds the latest scene's WebP plus `image/clipped/` (present only when `same_month` is not `null`), and `map/` holds four files (the latest `.tif` and `.png`, the example's `.png` and `.raw.png`; three if the example day is the latest scene, two if `exposed_bed_example` is `null`), all listed in `publish.json`. `series.json` `stage_area_curve.sha256` equals the run manifest's `.inputs.stage_area_curve_csv`. The series run's `manifest.json` (under `$RUN/data/outputs/series/<run_id>/`) has `code.dirty: false`. The full checks are in publish.md.
- **Checkout untouched.** Run `git -C "$RUN/checkout" status --porcelain` (empty) and `find "$RUN/checkout" -newer "$RUN/evidence/stamp" -not -path '*/.venv/*' -not -path '*/__pycache__/*'` (empty). Save both outputs as `daily-untouched` evidence.

## Gotchas

- `--ref` checks out committed code. Uncommitted changes are not under test, so commit them first or say they are excluded.
- If the SIH has not published the level for a new scene's date yet, or Earth Search is unreachable, the build exits 2. That is the designed "retry later" path, not a failure. Bad build arguments also exit 2, so read the `error:` line.
- An SIH outage stops the chain at `levels update` with exit 1 (`error: SIH download failed after 4 attempts: …`), not 2. A scheduler that retries only on 2 will not retry it.
- Publish exit 2 now includes a transient outage of the photo reads (`refused: true-colour image of … could not be read: …`), so it deserves the same retry as the build's exit 2. Doctor probes the STAC APIs and the SIH, not the COG hosts the photos are read from.
- `smolf export series-csv`, `smolf export maps` and `smolf export viewer` are not among the three steps (README's daily job). The bundle under `$SMOLF_DATA_DIR/outputs/downloads/`, the maps under `$SMOLF_DATA_DIR/outputs/maps/` and the viewer archive under `$SMOLF_DATA_DIR/outputs/viewer/` keep the last export until each is run again; `export maps` and `export viewer` need the scene cache the build filled, and `export viewer` also the network for its photos. See export.md.
- On a target host, the read-only mount, the UID that owns the checkout (`git safe.directory`) and the container's network all differ from this local stand-in. `chmod -R a-w` on a worktree does not prove any of them.
