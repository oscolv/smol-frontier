# Series build

`smolf series build` rebuilds the hyacinth time series: every reference scene, plus newer scenes with `--extend`, classified and bed-corrected. It writes one run directory with per-scene JSONs, `series.csv` and `manifest.json`. Its exit code tells a scheduler what happened: 0 complete, 2 precondition refusal (retry later, nothing written), 1 anything else.

## Sub-features

- `series-extend` builds the full series plus new scenes and writes a complete run.
- `series-stale-levels` refuses with exit 2 when a new scene is newer than the level record.
- `series-resume` resumes an interrupted run id.

## How to get to it (user POV)

- Run `smolf series build [--extend] [--workers N]`.
- Run `smolf series build --resume <run_id>`.

## Driving it with smolf-verify

Preconditions:

- Shared cache, clean checkout, and doctor 200 for Earth Search. About 470 STAC item lookups: 2.5–4 min with a warm cache.

- **Stale levels.** In a fresh run, before any `levels update`, run `$V run "$RUN" series-stale-levels -- series build --extend --workers 4`. If a scene exists after the committed record's last day (2026-11-05), `.rc` is `2` and `.out` has one `error: SIH level record ends 2026-11-05; no level for …` line. `$RUN/data/outputs/series/` does not exist. If `.rc` is `0`, no scene was newer, so record that the precondition is unmet and don't count it as proof.
- **Extend.** After `levels update` (see levels.md), run `$V run "$RUN" series-extend -- series build --extend --workers 4`. `.rc` `0`, with `counts: requested=N ok=… skipped=… errors=0` and `run_dir: $RUN/data/outputs/series/<run_id>`. Copy `manifest.json` into the evidence. It shows `status: complete`, `code.dirty: false`, and `inputs.ENDHD_csv` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`.
- **Resume.** Start a build and interrupt it: `timeout -s INT 30 $SMOLF series build` (source `$RUN/env` first, run from `/tmp`). That gives exit 130 and an `interrupted: resume with …` line. Then run `$V run "$RUN" series-resume -- series build --resume <run_id>`. `.rc` `0`, with the same run dir and status complete.

## Gotchas

- Exit 2 also covers `--workers 0` and `--extend` with `--resume` (argument checks) and an unreachable Earth Search on `--extend` (`error: Earth Search is unreachable … Retry later.`). Read the message, not just the code.
- A dirty checkout doesn't fail the build. It records `code.dirty: true`, which `publish` later refuses.
- `errors=` counts per-scene failures (network blips). The run still completes, so check that `errors=0` before calling it proof.
- The 2 skipped scenes are POOR-light scenes (valid fraction too low). They are expected and stay out of `series.csv`.
