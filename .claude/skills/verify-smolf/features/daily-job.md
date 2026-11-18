# Daily job

The scheduled daily job is three commands in order: `smolf levels update`, then `smolf series build --extend`, then `smolf publish --latest`. It runs from a read-only code checkout with every write under `SMOLF_DATA_DIR`. It must finish with a fresh contract and leave the checkout untouched, and a misconfigured data dir must fail fast.

## Sub-features

- `daily-bad-data-dir` rejects a relative or in-checkout `SMOLF_DATA_DIR` with exit 1 (two different messages).
- `daily-levels`, `daily-build` and `daily-publish` are the three steps.
- `daily-untouched` proves the checkout did not change.

## How to get to it (user POV)

- A scheduler runs, with `SMOLF_DATA_DIR=<absolute dir outside the checkout>`:
  `smolf levels update && smolf series build --extend && smolf publish --latest`. Exit 2 from the build means retry later (stale level record or Earth Search unreachable; nothing written). `levels update` exits 1 when the SIH download fails, even for a passing outage. `publish` exits 2 when it refuses a run or cannot read a Sentinel-2 photo (network outage; retry later).

## Driving it with smolf-verify

Preconditions:

- A run made with `new --ref <commit> --shared-cache`. `doctor --net` shows `checkout: read-only`, `git: clean`, and 200 for all three hosts.

- **Stamp.** Run `touch "$RUN/evidence/stamp"` before the first step.
- **Bad data dir.** Run `(. "$RUN/env"; cd /tmp; SMOLF_DATA_DIR=rel "$SMOLF" levels show 2026-11-05; echo rc=$?)`. You get `error: SMOLF_DATA_DIR='rel' must be an absolute path outside the checkout` and `rc=1`. Repeat with `SMOLF_DATA_DIR="$SMOLF_CODE/var"`: `error: SMOLF_DATA_DIR='…/checkout/var' must be outside the checkout (…/checkout)` and `rc=1`. Save both outputs to `$RUN/evidence/`.
- **Levels.** Run `$V run "$RUN" daily-levels -- levels update`. `.rc` `0`, with `output: $RUN/data/hydro/ENDHD.csv`.
- **Build.** Run `$V run "$RUN" daily-build -- series build --extend --workers 4`. `.rc` `0`, with `errors=0`. Measured 2026-11-18: `requested=471 ok=469 skipped=2`, wall 2:25 with a warm shared cache (2:40–3:38 on 2026-11-16). `requested` grows as new scenes appear, so treat it as a floor.
- **Publish.** Run `$V run "$RUN" daily-publish -- publish --latest`. `.rc` `0`, about 1.5 min (94 s on 2026-11-18; it reads the Sentinel-2 photos over the network). In `latest.json`, `current_level.levels_sha256` equals the hash of `$RUN/data/hydro/ENDHD.csv`, `schema_version` is `3.0.0`, and `image/` holds the latest scene's WebP plus `image/clipped/`, all listed in `publish.json`. The series run's `manifest.json` (under `$RUN/data/outputs/series/<run_id>/`) has `code.dirty: false`. The full checks are in publish.md.
- **Checkout untouched.** Run `git -C "$RUN/checkout" status --porcelain` (empty) and `find "$RUN/checkout" -newer "$RUN/evidence/stamp" -not -path '*/.venv/*' -not -path '*/__pycache__/*'` (empty). Save both outputs as `daily-untouched` evidence.

## Gotchas

- `--ref` checks out committed code. Uncommitted changes are not under test, so commit them first or say they are excluded.
- If the SIH has not published the level for a new scene's date yet, or Earth Search is unreachable, the build exits 2. That is the designed "retry later" path, not a failure. Bad build arguments also exit 2, so read the `error:` line.
- An SIH outage stops the chain at `levels update` with exit 1 (`error: SIH download failed after 4 attempts: …`), not 2. A scheduler that retries only on 2 will not retry it.
- Publish exit 2 now includes a transient outage of the photo reads (`refused: true-colour image of … could not be read: …`), so it deserves the same retry as the build's exit 2. Doctor probes the STAC APIs and the SIH, not the COG hosts the photos are read from.
- On a target host, the read-only mount, the UID that owns the checkout (`git safe.directory`) and the container's network all differ from this local stand-in. `chmod -R a-w` on a worktree does not prove any of them.
