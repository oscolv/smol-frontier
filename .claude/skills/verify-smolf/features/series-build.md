# Series build

`smolf series build` rebuilds the hyacinth time series: every reference scene, plus newer scenes with `--extend`, classified and bed-corrected. It writes one run directory with per-scene JSONs, `series.csv` and `manifest.json`. Its exit code tells a scheduler what happened: 0 complete, 2 precondition refusal (retry later, nothing written), 130 interrupted (resumable), 1 anything else.

## Sub-features

- `series-extend` builds the full series plus new scenes and writes a complete run.
- `series-stale-levels` refuses with exit 2 when a new scene is newer than the level record.
- `series-resume` resumes an interrupted run id.
- `series-catalog-down` refuses with exit 2, writing nothing, when Earth Search is unreachable on `--extend`.

## How to get to it (user POV)

- Run `smolf series build [--extend] [--workers N]`.
- Run `smolf series build --resume <run_id>`.

## Driving it with smolf-verify

Preconditions:

- Shared cache, clean checkout, and doctor 200 for Earth Search. About 470 STAC item lookups: 2.5–4 min with a warm cache.

- **Stale levels.** In a fresh run, before any `levels update`, run `$V run "$RUN" series-stale-levels -- series build --extend --workers 4`. If a scene exists after the committed record's last day (2026-11-18), `.rc` is `2`, `.err` has one `error: SIH level record ends 2026-11-18; no level for …` line, and `.out` is empty (no `run:` line). `$RUN/data/outputs/series/` does not exist. If `.rc` is `0`, no scene was newer, so record that the precondition is unmet and don't count it as proof.
- **Extend.** After `levels update` (see levels.md), run `$V run "$RUN" series-extend -- series build --extend --workers 4`. `.rc` `0`, with `counts: requested=N ok=… skipped=… errors=0` and `run_dir: $RUN/data/outputs/series/<run_id>`. Copy `manifest.json` into the evidence. It shows `status: complete`, `code.dirty: false`, and `inputs.ENDHD_csv` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`.
- **Earth Search down.** The catalog search runs before the level check, so this refusal wins even with a stale level record. Run `$V run "$RUN" series-catalog-down --block-host earth-search.aws.element84.com -- series build --extend --workers 4`. `.rc` `2`, `.out` empty, and `.err` holds one `error: Earth Search is unreachable, so new scenes cannot be listed; nothing was written. Retry later.` line plus the injected `blocked by smolf-verify` warning ending `; no other backend`. `$RUN/data/outputs/series/` is unchanged (`ls` before and after). Then, after `levels update`, repeat without `--block-host` (the Extend step does this): `.rc` `0` and a complete run. Repeated before the refresh, it exits 2 on stale levels instead. That shows the refusal was the outage and not something else.
- **Resume.** Start a build and interrupt it: `(. "$RUN/env"; cd /tmp; timeout --preserve-status -s INT 40 "$SMOLF" series build --workers 4; echo rc=$?)`. You get `rc=130` and, on stderr, an ``interrupted: resume with `smolf series build --resume <run_id>` `` line. Take `<run_id>` from the `run:` line on stdout. Then run `$V run "$RUN" series-resume -- series build --resume <run_id> --workers 4`. `.rc` `0`, with `run: <run_id> (resume)`, `counts: requested=468 … errors=0`, and status complete in that run's manifest.
- **Bad arguments.** Run `$V run "$RUN" series-badargs -- series build --extend --resume x`: `.rc` `2`, `error: --extend cannot be combined with --resume`. Run `$V run "$RUN" series-noresume -- series build --resume nope`: `.rc` `1`, `.err` `error: unknown run: nope`, `.out` empty.

## Gotchas

- Stdout order on success: `run: <id> (new|resume)`, one `[done/total] <date> <item> <status> …` line per scene processed in this invocation (a resume prints only the pending ones, its counter starting at the number already done: 138 before the interrupt plus 327 on resume, 2026-11-18, against the 465-scene v2026-10-22 reference; the v2026-11-19 reference has 468), then `run_dir:`, `series_csv:`, `series_sha256:`, `counts:`, `wall:`. `run:` is printed only once the run directory exists, so a refusal prints nothing on stdout and one `error:` line on stderr. An interrupt before that point prints `interrupted before the run started; nothing written` and offers no `--resume`.
- Without `--preserve-status`, `timeout` exits 124 and hides smolf's 130.
- A resume fingerprint mismatch (`refusing resume of run …`) exits 1, not 2: the code, config or dirty flag changed since the run started, so no retry helps. Start a new run instead. `unknown run` and `run exists` also exit 1.
- Exit 2 also covers `--workers 0` and `--extend` with `--resume` (argument checks) and an unreachable Earth Search on `--extend` (`error: Earth Search is unreachable … Retry later.`). Read the message, not just the code.
- A dirty checkout doesn't fail the build. It records `code.dirty: true`, which `publish` later refuses.
- `errors=` counts per-scene failures: network blips, but also a scene whose classification or bed correction raised. The run still completes, so check that `errors=0` before calling it proof.
- `skipped=` counts POOR-light scenes (valid fraction too low); they stay out of `series.csv`. A reference-only run (the Resume step) gives `skipped=0`. The `--extend` run adds 1 newer scene, skipped, as of 2026-11-19 (`requested=469 ok=468 skipped=1`, run `20261119T184404`); `requested` grows as new scenes appear.
