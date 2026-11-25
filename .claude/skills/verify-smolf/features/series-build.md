# Series build

`smolf series build` rebuilds the hyacinth time series: every reference scene, plus newer scenes with `--extend`, classified and bed-corrected. It writes one run directory with per-scene JSONs, `series.csv` and `manifest.json`. Its exit code tells a scheduler what happened: 0 complete, 2 precondition refusal (retry later, nothing written), 130 interrupted (resumable), 1 anything else.

## Sub-features

- `series-extend` builds the full series plus new scenes and writes a complete run.
- `series-stale-levels` refuses with exit 2 when a new scene is newer than the level record.
- `series-resume` resumes an interrupted run id.
- `series-catalog-down` refuses with exit 2, writing nothing, when Earth Search is unreachable on `--extend`.
- `series-badargs` refuses `--extend` with `--resume` (exit 2); `series-noresume` reports an unknown `--resume` id (exit 1).

## How to get to it (user POV)

- Run `smolf series build [--extend] [--workers N]`.
- Run `smolf series build --resume <run_id>`.

## Driving it with smolf-verify

Preconditions:

- Shared cache, clean checkout, and doctor 200 for Earth Search. 468 STAC item lookups (471 or more with `--extend` since v0.8.1): 2.5–4 min with a warm cache (3:13 with `--extend` and 3:02 for a resume of 387 scenes on 2026-11-25 at v0.12.0; 2:57 with `--extend` on 2026-11-23 at v0.11.0; 3:38 and 2:57 with `--extend` and 2:24 for a resume of 357 scenes on 2026-11-22 at v0.10.0; 3:06 and 2:56 for 353 on 2026-11-21).

- **Stale levels.** In a fresh run, before any `levels update`, run `$V run "$RUN" series-stale-levels -- series build --extend --workers 4`. If a scene exists after the committed record's last day (2026-11-18), `.rc` is `2`, `.err` has one `error: SIH level record ends 2026-11-18; no level for …` line, and `.out` is empty (no `run:` line). `$RUN/data/outputs/series/` does not exist. If `.rc` is `0`, no scene was newer, so record that the precondition is unmet and don't count it as proof (the case on 2026-11-21: the newest scene was 2026-11-11; on 2026-11-22 and 2026-11-23 the 2026-11-21 scene made it exit 2 as described, `no level for 2026-11-21`). That build still writes a complete run, so a later `publish --latest` in the same data dir may pick it; use `--run <id>` there.
- **Extend.** After `levels update` (see levels.md), run `$V run "$RUN" series-extend -- series build --extend --workers 4`. `.rc` `0`, with `counts: requested=N ok=… skipped=… errors=0` and `run_dir: $RUN/data/outputs/series/<run_id>`. Copy `manifest.json` into the evidence. It shows `status: complete`, `code.dirty: false`, and `inputs.ENDHD_csv` equal to `sha256sum $RUN/data/hydro/ENDHD.csv`.
- **Earth Search down.** The catalog search runs before the level check, so this refusal wins even with a stale level record. Run `$V run "$RUN" series-catalog-down --block-host earth-search.aws.element84.com -- series build --extend --workers 4`. `.rc` `2`, `.out` empty, and `.err` holds one `error: Earth Search is unreachable, so new scenes cannot be listed; nothing was written. Retry later.` line plus the injected `blocked by smolf-verify` warning ending `; no other backend`. `$RUN/data/outputs/series/` is unchanged (`ls` before and after). Then, after `levels update`, repeat without `--block-host` (the Extend step does this): `.rc` `0` and a complete run. Repeated before the refresh, it exits 2 on stale levels instead. That shows the refusal was the outage and not something else.
- **Resume.** Start a build and interrupt it: `(. "$RUN/env"; cd /tmp; timeout --preserve-status -s INT 40 "$SMOLF" series build --workers 4; echo rc=$?)`. You get `rc=130` and, on stderr, an ``interrupted: resume with `smolf series build --resume <run_id>` `` line. Take `<run_id>` from the `run:` line on stdout. Then run `$V run "$RUN" series-resume -- series build --resume <run_id> --workers 4`. `.rc` `0`, with `run: <run_id> (resume)`, `counts: requested=468 … errors=0`, and status complete in that run's manifest.
- **Bad arguments.** Run `$V run "$RUN" series-badargs -- series build --extend --resume x`: `.rc` `2`, `error: --extend cannot be combined with --resume`. Run `$V run "$RUN" series-noresume -- series build --resume nope`: `.rc` `1`, `.err` `error: unknown run: nope`, `.out` empty.

## Gotchas

- Stdout order on success: `run: <id> (new|resume)`, one `[done/total] <date> <item> <status> …` line per scene processed in this invocation (a resume prints only the pending ones, its counter starting at the number already done: 81 before a 40 s interrupt plus 387 on resume, the first resumed line `[82/468]`, 2026-11-25; 103 and `[104/468]` on 2026-11-23; 111 and `[112/468]` on 2026-11-22), then `run_dir:`, `series_csv:`, `series_sha256:`, `counts:`, `wall:`. `run:` is printed only once the run directory exists, so a refusal prints nothing on stdout and one `error:` line on stderr. An interrupt before that point prints `interrupted before the run started; nothing written` and offers no `--resume`.
- Without `--preserve-status`, `timeout` exits 124 and hides smolf's 130.
- A rasterio `NotGeoreferencedWarning` (`features.py … _rasterize`) on stderr of a build or resume is known noise, seen since 2026-11-17; the figures are unaffected. So are `boa_offset_applied looks wrong … using 0.0` lines (the radiometry guard: 12 in each build and in a resume, 2026-11-25 at v0.12.0 as at v0.10.0 and v0.11.0). Read `.rc` and the `error:` lines, not the stderr size.
- A resume fingerprint mismatch (`refusing resume of run …`) exits 1, not 2: the code, config or dirty flag changed since the run started, so no retry helps. Start a new run instead. Any run started at another commit hits this (`git_commit` differs), so a run interrupted before an upgrade cannot be resumed after it. Runs started before v0.12.0 also differ in `config_hash`, which covers every setting: 0.10.0 added `EXPOSED_BED_EXAMPLE_DATE`, 0.11.0 changed no setting, and 0.12.0 added the `BIOMASS_WET_*` and `BIOMASS_SOURCE` settings. `unknown run` and `run exists` also exit 1.
- Exit 2 also covers `--workers 0` and `--extend` with `--resume` (argument checks) and an unreachable Earth Search on `--extend` (`error: Earth Search is unreachable … Retry later.`). Read the message, not just the code.
- A dirty checkout doesn't fail the build. It records `code.dirty: true`, which `publish` later refuses. Changes to the hashed hydrology inputs (`data/hydro/ENDHD.csv`, the stage–area curve, the flood-stage map, their `.json` sidecars) don't count as dirty: the manifest pins them by sha256.
- `errors=` counts per-scene failures: network blips, but also a scene whose classification or bed correction raised. The run still completes, so check that `errors=0` before calling it proof.
- `skipped=` counts POOR-light scenes (valid fraction too low); they stay out of `series.csv`. A reference-only run (the Resume step) gives `skipped=0`. The `--extend` run adds 4 scenes from 2026-10-13 on dates the reference lacks (2026-10-19, 2026-11-11 and 2026-11-21 skipped, 2026-11-06 ok), as of 2026-11-25 at v0.12.0 (`requested=472 ok=469 skipped=3`, run `20261125T011151`; valid_frac 0.50 for 2026-11-11 and 0.39 for 2026-11-21, under `MIN_VALID_FRACTION` 0.80; the same on 2026-11-23 at v0.11.0, run `20261123T022429`; the same on 2026-11-22 at v0.10.0, run `20261122T162250`; 471/469/2 without the 2026-11-21 scene from v0.8.1 on 2026-11-19 to 2026-11-21); `requested` grows as new scenes appear, except a scene whose tile cloud cover in the catalog is over `MAX_CLOUD_COVER_CATALOG` (50 %): `--extend` never lists it (2026-11-16, 92 %).
