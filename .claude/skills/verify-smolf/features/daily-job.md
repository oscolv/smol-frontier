# Daily job

The scheduled daily job is three commands in order: `smolf levels update`, then `smolf series build --extend`, then `smolf publish --latest`. It runs from a read-only code checkout with every write under `SMOLF_DATA_DIR`. It must finish with a fresh contract and leave the checkout untouched, and a misconfigured data dir must fail fast.

## Sub-features

- `daily-bad-data-dir` rejects a relative or in-checkout `SMOLF_DATA_DIR` with exit 1 (two different messages).
- `daily-levels`, `daily-build` and `daily-publish` are the three steps.
- `daily-untouched` proves the checkout did not change.

## How to get to it (user POV)

- A scheduler runs, with `SMOLF_DATA_DIR=<absolute dir outside the checkout>`:
  `smolf levels update && smolf series build --extend && smolf publish --latest`. Exit 2 from the build means retry later.

## Driving it with smolf-verify

Preconditions:

- A run made with `new --ref <commit> --shared-cache`. Doctor shows `checkout: read-only`, `git: clean`, and 200 for all three hosts.

- **Stamp.** Run `touch "$RUN/evidence/stamp"` before the first step.
- **Bad data dir.** Run `(. "$RUN/env"; cd /tmp; SMOLF_DATA_DIR=rel "$SMOLF" levels show 2026-11-05; echo rc=$?)`. You get `error: SMOLF_DATA_DIR='rel' must be an absolute path outside the checkout` and `rc=1`. Repeat with `SMOLF_DATA_DIR="$SMOLF_CODE/var"`: `error: SMOLF_DATA_DIR='…/checkout/var' must be outside the checkout (…/checkout)` and `rc=1`. Save both outputs to `$RUN/evidence/`.
- **Levels.** Run `$V run "$RUN" daily-levels -- levels update`. `.rc` `0`, with `output: $RUN/data/hydro/ENDHD.csv`.
- **Build.** Run `$V run "$RUN" daily-build -- series build --extend --workers 4`. `.rc` `0`, with `errors=0`. Measured 2026-11-16: `requested=471 ok=469 skipped=2`, wall 2:40–3:38 with a warm shared cache (3:01 on 2026-11-17).
- **Publish.** Run `$V run "$RUN" daily-publish -- publish --latest`. `.rc` `0`. In `latest.json`, `current_level.levels_sha256` equals the hash of `$RUN/data/hydro/ENDHD.csv`, and the manifest has `code.dirty: false`.
- **Checkout untouched.** Run `git -C "$RUN/checkout" status --porcelain` (empty) and `find "$RUN/checkout" -newer "$RUN/evidence/stamp" -not -path '*/.venv/*' -not -path '*/__pycache__/*'` (empty). Save both outputs as `daily-untouched` evidence.

## Gotchas

- `--ref` checks out committed code. Uncommitted changes are not under test, so commit them first or say they are excluded.
- If the SIH has not published the level for a new scene's date yet, the build exits 2. That is the designed "retry later" path, not a failure.
- On a target host, the read-only mount, the UID that owns the checkout (`git safe.directory`) and the container's network all differ from this local stand-in. `chmod -R a-w` on a worktree does not prove any of them.
