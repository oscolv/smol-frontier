---
name: verify-smolf
description: Drive the real `smolf` CLI (smol-frontier, water-hyacinth series for the Endhó reservoir) end to end and capture proof — classify a day, refresh SIH levels, build the series, publish the contract, or run the whole daily job from a read-only checkout. Use it to verify a change works in the real CLI, not just in pytest.
---

# Verify smolf

`smolf` is a short-lived CLI (Typer). There is no server: each verification step is one command with an exit code, `key: value` stdout lines, and files written under `SMOLF_DATA_DIR`. Every network call is real (STAC catalogs, the SIH level record). Use `scripts/smolf-verify` for all of it; it isolates the run, records evidence, and cleans up what it created.

```bash
V=.claude/skills/verify-smolf/scripts/smolf-verify   # from the repo root
```

## Launch

```bash
uv sync                                  # once, in the repo checkout
RUN=$($V new --ref HEAD --shared-cache)  # prints the run dir
```

- `--ref <commit>` checks the commit out into `$RUN/checkout` as a git worktree, runs `uv sync --frozen` there, and makes it read-only (`chmod -R a-w`). That is the production shape: code read-only, all writes under `SMOLF_DATA_DIR`. Without `--ref` the run uses the repo's own `.venv/bin/smolf` and checkout (writable). Commit first: `--ref` checks out committed code only.
- `--shared-cache` symlinks `$RUN/data/cache` to the repo's `data/cache` (about 480 scenes, ~1.4 MB each). Without it the first `series build` downloads every scene (about an hour). Never run two builds against the same shared cache at once: cache files are written in place.
- Ready means `doctor` passes (below). No seed step: an empty data dir is valid. Readers fall back to the committed level record until `levels update` writes `$RUN/data/hydro/ENDHD.csv`.

## Doctor

```bash
$V doctor "$RUN" --net
```

Read-only. It needs `smolf: <version>` (the CLI validates `SMOLF_DATA_DIR` on every command), `git: clean`, and, for real-network features, HTTP 200 from Earth Search, Planetary Computer and the SIH. Act on what it says:

- `git: DIRTY` means every series run records `dirty: true` and `publish` refuses it with exit 2. Commit or use `--ref`.
- `smolf: FAIL ... must be an absolute path outside the checkout` means a bad `SMOLF_DATA_DIR`.
- `net: 000` means that feature's real path cannot be verified now. Report it as unverified. Do not swap in a mock.

## Drive

```bash
$V run "$RUN" <feature-id> -- <smolf args>    # e.g. -- classify 2026-10-12
$V hectares "$RUN" <classified.tif>           # per-class hectares of a written map
```

`run` executes `$SMOLF <args>` from `/tmp` with the run's `SMOLF_DATA_DIR`, prints stdout, and records `<n>-<feature-id>.cmd/.out/.err/.rc` in `$RUN/evidence/`. It always exits 0 itself. Read the recorded `.rc`.

Two `run` options inject into smolf's Python for that one command only, through a `sitecustomize.py` written to `$RUN/inject/`:

- `--block-host <host>` (repeatable) makes DNS fail for that host at `socket.getaddrinfo`, which is where a real outage surfaces. Use `earth-search.aws.element84.com` to simulate Earth Search down. It covers Python HTTP (STAC API, Planetary Computer signing, the SIH), not GDAL/curl reads of scene files. Proof that the block caused the failure: `.err` contains `blocked by smolf-verify`.
- `--log-level INFO` turns on smolf's own log, which the CLI never configures (by default only WARNING and above reach stderr). The feature recipes, with exact commands and expected values, are in [`features/`](features/README.md).

## Evidence

All proof lives in `$RUN/evidence/` (default root `~/.local/state/smolf-verify/`, override with `SMOLF_VERIFY_HOME`). Proof standards:

- Drive the real CLI path. No direct calls to internal functions, no pytest fixtures, no faked HTTP. Mocks belong in the test suite, not here.
- Capture the command, stdout, stderr and exit code (`run` does this), plus the state afterwards. Examples: hectares of the written GeoTIFF (`hectares`), the series `manifest.json` fields, `latest.json` / `publish.json` from the publish dir, and the level record's last day.
- Verify side effects, not just stdout: files under `$RUN/data/`, and for a read-only run, `git -C "$RUN/checkout" status --porcelain` empty and nothing new under the checkout (`find "$RUN/checkout" -newer <stamp> -not -path '*/.venv/*' -not -path '*/__pycache__/*'`).
- `levels update --check` is the only dry run. It still downloads from the SIH. It writes nothing, so check that `$RUN/data/hydro/` stays absent.
- Copy any extra artifact you rely on (a manifest, `latest.json`) into `$RUN/evidence/` before cleanup. Cleanup deletes `$RUN/data/`.

## Cleanup

```bash
$V cleanup "$RUN"
```

Restores write permission on and removes the run's own worktree (`$RUN/checkout`), prunes worktree records, and deletes `$RUN/data/`. The shared cache is only a symlink, so its target is untouched. It keeps `$RUN/evidence/` and prints its path. It touches nothing it did not create. Run it after every failed attempt too. Remove old evidence dirs by hand when no longer needed.

## Helpers

- `scripts/smolf-verify`: `new`, `doctor`, `run`, `hectares`, `cleanup`, as shown above. Run it without arguments for usage.
