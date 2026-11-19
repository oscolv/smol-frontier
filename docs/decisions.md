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
  `hydro/` hold only the validated pipeline; `experiments/` holds one
  script plus a note per experiment (`sensitivity`, `truecolour_gamma`).

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
  the local SIH level record and asks for `smolf levels update` first;
  `smolf classify` refuses such a day (or any day when there is no local
  record) the same way, with the same check and message, before it searches
  the catalog. A day inside the record whose level is missing or NaN is a
  real SIH gap, not a stale copy: it still classifies, with
  `NO CORRECTION: <reason>`. Edits
  to the hydrology inputs already pinned by sha256 in the manifest
  (`ENDHD.csv`, the stage–area curve, the flood-stage map, and their
  sidecars) do not set the `dirty` flag; any other change does. The refusal
  is a precondition refusal: `series build` exits 0 when complete, 2 when it
  refuses a precondition that a later run can meet (stale level record,
  unreachable new-scene catalog; nothing is written), and 1 for anything
  else, including a resume fingerprint mismatch, which no retry can fix.
  `classify` uses the same codes (2 for the stale or missing record and for
  an unreachable catalog). The full list is in `docs/method.md` §9.
- **Why:** a scene without its day's level gets no exposed-bed correction and
  would enter `series.csv` with empty corrected columns (2026-11-01 did, with
  the SIH copy ending 2026-10-27); `classify` reported 2026-11-08 raw
  (730.99 ha) with the copy ending 2026-11-05. The level record is refreshed
  operationally, and its hash already identifies the exact bytes a run used,
  so flagging those edits as dirty would mark every operational run without
  adding provenance.
- **Where in this repo:** `hydro/levels.py` (`check_days_covered()`, the one
  coverage rule and message; `StaleLevelsError`; `last_day_with_level()`),
  `series.py` (`check_levels_cover()` delegates to it; `HASHED_INPUT_PATHS`,
  `is_dirty()`), `cli.py` (`classify` checks before `_search_day`; exit 2 on
  `StaleLevelsError` in `classify` and `series build`),
  `tests/test_series.py`, `tests/test_levels.py`.

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
- **Where in this repo:** `src/smol_frontier/publish/` (`publish_run()`,
  `SCHEMA_VERSION`, `ALLOWED_FILES`), `src/smol_frontier/schemas/`,
  `classify/geotiff.py` (`write_published_geotiff()` and `guard_published()`,
  the uint8 0–5 guard), `classify/display.py` (`write_display_png()`, which
  passes the same guard),
  `cli.py` (exit 2 on `PublishRefused`, exit 1 on `ContractInvalid`),
  `docs/contract.md`, `tests/test_publish.py`,
  `tests/test_publish_regression.py`, `tests/test_contract_doc.py`.
- **Amended (contract 3.0.0, spec #44):** smolf now owns the
  deterministic quality gate and publishes it per scene as
  `display_status` (`HIDDEN`: not GOOD or no corrected figure;
  `LOWER_BOUND`: published `valid_frac` below `frac_publishable`; `FULL`
  otherwise), computed by `publish.aggregates.display_status()`. The
  consumer asked for figures over "the scenes it shows" (monthly medians,
  same-month comparisons) without doing arithmetic; a second copy of the
  gate on either side would drift. Figures are still published as
  computed, nothing is filtered out, and the consumer's editorial holds and
  withdrawals stay downstream, unknown to smolf.
- **Amended (contract 3.0.0, issue #48):** "leaking source imagery
  impossible by construction" now reads "leaking PlanetScope imagery or
  raw reflectance impossible by construction". The ban existed to protect
  PlanetScope's licence, which does not allow redistributing its imagery;
  Sentinel-2 L2A is free to publish under the Copernicus licence with
  attribution. `latest.json` therefore carries a Sentinel-2 true-colour
  photo of its scene, written only through a separate guard
  (`publish.truecolour.guard_true_colour()`: collection `sentinel-2-l2a`,
  uint8, RGBA since contract 3.2.0); the class-map guard is unchanged. See
  `docs/adr/0001-publish-sentinel-2-true-colour.md`.

## 14. A catalog outage on the new-scene search is a retryable refusal

- **Rule:** `series build --extend` lists new scenes from Earth Search only.
  When that search fails because the catalog is unreachable, the build
  refuses with exit 2 (`CatalogUnavailable`) before writing anything. There
  is no Planetary Computer fallback for the new-scene search. Per-scene
  fallbacks (resolve, ingest on an anomalous product) are unchanged.
- **Why:** Planetary Computer item ids differ from Earth Search ids, so a
  series listed from Planetary Computer during an outage would get a
  different `series.csv` sha256 and the series identity would churn with
  outages. Each build rebuilds the whole series, so a refused day loses
  nothing: the next successful run covers it.
- **Where in this repo:** `sources/stac.py` (`AllBackendsFailed`),
  `series.py` (`CatalogUnavailable`, `list_extend_targets()`), `cli.py`
  (exit 2, also for `classify` and `fetch`), `docs/method.md` (exit-code
  list), `tests/test_series.py`, `tests/test_stac.py`, `tests/test_ingest.py`.

## 15. Publisher as a package; `schema_version` and `removed_ha` deferred

- **Rule:** the publisher is the package `src/smol_frontier/publish/`
  (`errors`, `select`, `documents`, `map`, `writer`), one responsibility
  per module under rule 3, and `smol_frontier.publish` keeps re-exporting
  its public names. The `schema_version` pattern `^1\.[0-9]+\.[0-9]+$`
  also accepts a trailing newline; the schema is left as is, smolf always
  writes the constant `1.0.0`, and consumers compare the exact string.
  Nulling `removed_ha` when `bed_applied` is false is deferred as well.
  Both changes wait for the next major contract version (issue #38).
- **Why:** one module had grown to hold run selection, document building,
  map rendering and the directory swap, which made each hard to test and
  to change. Changing a schema is a contract change: fixing the pattern
  or nulling a field that 1.0.0 consumers may read would break files
  that validate today, so both ride with the next major version instead
  of a silent edit.
- **Where in this repo:** `src/smol_frontier/publish/`,
  `src/smol_frontier/schemas/` (unchanged), `docs/contract.md`
  ("Versioning"), `tests/test_publish.py`, `tests/test_contract_doc.py`.

## 16. Contract 2.0.0: `removed_ha` follows `bed_applied`

- **Rule:** `schema_version` 2.0.0 (smolf 0.3.0) publishes `removed_ha` as
  `null` whenever `bed_applied` is false, like every corrected figure, and
  both per-scene schemas require it (`null` when false, a number when
  true). The `schema_version` rule is the pattern `^2\.[0-9]+\.[0-9]+$`
  plus `"not": {"pattern": "\n"}`, so a trailing newline no longer
  validates; no lookahead, which RE2-based validators reject. Supersedes
  the deferral in entry 15.
- **Why:** a removed area only exists when the correction ran; a number
  there without a correction invites a consumer to subtract it from the
  raw figure. The run rows already carry `None` in that case, but 1.0.0
  neither enforced it in the publisher nor in the schema. Both changes
  reject files that validate under 1.x, so they ship together as one
  major bump.
- **Where in this repo:** `src/smol_frontier/publish/documents.py`
  (`SCHEMA_VERSION`, `_CORRECTED_FIELDS`), `src/smol_frontier/schemas/`,
  `docs/contract.md` ("Versioning"), `tests/test_publish.py`.
