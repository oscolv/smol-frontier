# Design decisions

Thirteen project rules, each with its one-line reason and where it is
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

## 12. Levels before scenes; hashed inputs are not "dirty"

- **Rule:** a fresh series build refuses any scene newer than the last day in
  the local SIH level record and asks for `smolf levels update` first. Edits
  to the hydrology inputs already pinned by sha256 in the manifest
  (`ENDHD.csv`, the stage–area curve, the flood-stage map, and their
  sidecars) do not set the `dirty` flag; any other change does.
- **Why:** a scene without its day's level gets no exposed-bed correction and
  would enter `series.csv` with empty corrected columns (2026-11-01 did, with
  the SIH copy ending 2026-10-27). The level record is refreshed
  operationally, and its hash already identifies the exact bytes a run used,
  so flagging those edits as dirty would mark every operational run without
  adding provenance.
- **Where in this repo:** `series.py` (`check_levels_cover()`,
  `HASHED_INPUT_PATHS`, `is_dirty()`), `hydro/levels.py`
  (`last_day_with_level()`), `cli.py` (exit 1 on `StaleLevelsError`),
  `tests/test_series.py`.

## 13. Neutral, versioned publish contract

- **Rule:** `publish` emits a neutral, versioned contract; the run manifest
  is provenance, not contract. `smolf publish` derives a fixed allowlist of
  files (`latest.json`, `series.json`, `publish.json`, the latest scene's
  map) from one complete, clean run; each JSON file carries
  `schema_version` and is validated against its packaged JSON Schema before
  an atomic swap. A schema accepts any version with its major, so only
  optional fields are additive (minor); any enum change is breaking (major).
  Every figure is published as computed — nothing is hidden, filtered or
  substituted — and display gates belong to the downstream consumer.
- **Why:** a consumer that read the run directory would couple to internal
  layout, recompute percentages or bands (two numbers for one thing), and
  have no way to detect a breaking change. A neutral contract with the
  constants it needs keeps one source for every number; versioned schemas
  make upgrades deliberate; the raster guard and the allowlist make leaking
  source imagery impossible by construction.
- **Where in this repo:** `src/smol_frontier/publish.py` (`publish_run()`,
  `SCHEMA_VERSION`, `ALLOWED_FILES`), `src/smol_frontier/schemas/`,
  `classify/geotiff.py` (`write_published_geotiff()` and `write_display_png()`, the uint8 0–5 guard),
  `cli.py` (exit 2 on `PublishRefused`, exit 1 on `ContractInvalid`),
  `docs/contract.md`, `tests/test_publish.py`,
  `tests/test_publish_regression.py`, `tests/test_contract_doc.py`.
