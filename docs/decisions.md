# Design decisions

Eleven project rules, each with its one-line reason and where it is
enforced in this repo. Anything not implemented yet says **planned**.

## 1. Installable package, real CLI entry point

- **Rule:** installable package with `pyproject.toml` and a CLI entry
  point; no "run from inside the directory" scripts.
- **Why:** one install, one command (`smolf`) — nothing depends on the
  caller's working directory.
- **Where in this repo:** `pyproject.toml` (`[project.scripts]`,
  `smolf = smol_frontier.cli:app`); there are no `.py` files at the repo
  root and no `scripts/` directory.

## 2. One config, computed reservoir area

- **Rule:** one config; the reservoir area is computed from the polygon in
  `aoi.py` and a test checks it against 1261.75 ha.
- **Why:** a single source of truth makes every area comparable across runs.
- **Where in this repo:** `src/smol_frontier/config.py` (all thresholds),
  `src/smol_frontier/aoi.py` (`reservoir_area_ha()`),
  `tests/test_aoi.py`, `tests/test_regression_scenes.py`.

## 3. Single-responsibility modules, thin CLI

- **Rule:** single-responsibility modules; `cli.py` only parses arguments
  and calls.
- **Why:** algorithms stay testable without a command line.
- **Where in this repo:** `src/smol_frontier/cli.py` (parsing, progress
  output, exit codes) delegates to `ingest.py`, `classify/pipeline.py`,
  `quality.py`, `hydro/`, `gate.py`, `series.py`.

## 4. Only validated methods in `src/`

- **Rule:** only methods that survived validation live in `src/` (2-stage
  GMM, 3 quality criteria, regime, exposed bed by stage); anything
  experimental goes to `experiments/`.
- **Why:** the package stays the trusted pipeline, never a notebook graveyard.
- **Where in this repo:** `src/smol_frontier/classify/`, `quality.py`,
  `hydro/` hold only the validated pipeline; `experiments/` — **planned**
  (the directory does not exist yet).

## 5. One BOA-offset application point, baseline-gated

- **Rule:** the BOA offset is applied in exactly one function in
  `sources/radiometry.py`, gated by the STAC item's processing baseline,
  with tests for both sources.
- **Why:** the +0.1 step once corrupted a whole series; a single gated
  rule cannot drift.
- **Where in this repo:** `sources/radiometry.py` (`boa_offset()` decides,
  `dn_to_reflectance()` is the single place adding it);
  `tests/test_radiometry.py` (baseline × flag branches),
  `tests/test_stac.py::test_boa_offset_flag` (both catalogs' flags).

## 6. Two STAC sources, one contract, equivalent cache

- **Rule:** two STAC sources with one contract; local cache keyed by
  (source, item_id); cache hit == miss enforced in tests.
- **Why:** either catalog can serve a scene without changing the numbers.
- **Where in this repo:** `sources/stac.py` (`SceneMeta` contract,
  automatic fallback); `tests/test_ingest.py` (hit == miss, single offset
  for all bands); the (source, item_id) cache key — **planned** (today the
  key is the item id in `ingest.py`; the two catalogs' id schemes cannot
  collide, so this is a hardening step, not a bug).

## 7. Analyses are CLI subcommands or tracked experiments

- **Rule:** every analysis is a CLI subcommand or lives in `experiments/`
  with its own README and manifest-tracked outputs.
- **Why:** any figure in a report can be re-run from one command.
- **Where in this repo:** `smolf fetch/classify/levels/hydro/gate/series`;
  the `experiments/` half — **planned** (see rule 4).

## 8. Per-run manifest, hash-identified series

- **Rule:** per-run `manifest.json` (code, inputs, parameters, library
  versions); a series is identified by hash, not by date.
- **Why:** a run is reproducible and citable from one file.
- **Where in this repo:** `src/smol_frontier/series.py` (manifest with
  fingerprint, input hashes, per-scene entries, `series.csv` sha256),
  `tests/test_series.py`, `outputs/series/20261028T221314/`.

## 9. Exact pins, locked CI

- **Rule:** exact pins in `pyproject.toml`, resolved in `uv.lock`; CI
  installs from the lock.
- **Why:** the same code plus the same libraries gives the same numbers.
- **Where in this repo:** `pyproject.toml` (`==` pins), `uv.lock`,
  `.github/workflows/ci.yml` (`uv sync`).

## 10. One public percentage, fixed denominator

- **Rule:** one public percentage with a fixed denominator (the reservoir
  polygon); any other ratio carries "diagnostic" in its name.
- **Why:** readers never wonder "percent of what?".
- **Where in this repo:** `pct_reservoir` over `aoi.py` (the only public
  percentage); the "diagnostic" naming for the remaining ratios
  (`valid_fraction`, `cloud_fraction`, `nir_water_frac`) — **planned**.

## 11. Spectral cloud test from the start

- **Rule:** spectral cloud test from the start because SCL misses low haze;
  2025-07-29 is the regression scene. A visual veto with a vision model is
  planned for the agent layer.
- **Why:** missed haze once passed as valid with a fully false cover.
- **Where in this repo:** `classify/masks.py` (`spectral_cloud()`),
  `tests/test_masks.py`, `tests/test_regression_scenes.py`
  (`test_haze_scene_2025_07_29_is_poor`); the visual veto — **planned**
  (the agent layer is not built yet).
