# smol-frontier — agent conventions

## Language

- Everything in the repository is in English — code, comments, docs, CLI
  output, column and file names.
- Exceptions: third-party data kept byte-for-byte (the SIH CSV headers stay
  as published), proper nouns (Endhó, SIH), and the Spanish text of
  published deliverables for Spanish readers: only `LEEME.txt` of the
  download bundle, confined to the marked block in `publish/export.py`
  (`docs/decisions.md` 19). Its CSV column names and codes stay English.

## Layout and toolchain

- Package layout: `src/smol_frontier/`; CLI is `smolf` (Typer).
- Run everything with `uv run` (`uv run pytest`, `uv run ruff check .`,
  `uv run smolf ...`). The environment lives in `.venv/` at the repo root.

## Architecture map

The pipeline in order, one line per stage:

- STAC search — `sources/stac.py`
- Ingest + cache — `ingest.py` (`smolf fetch`)
- Radiometry — `sources/radiometry.py`
- Validity masks + indices — `classify/masks.py`, `classify/indices.py`
- Pre-masks + GMM Stage 1/2 + rescue + substrate — `classify/gmm.py`
  (plus `haze_pass`, the frozen second pass over haze-over-vegetation
  pixels)
- `classify()` — `classify/pipeline.py` (`smolf classify`; loads the day's
  water mirror, `hydro.bed.load_day_mirror`, and rescues hyacinth under
  thin haze inside it with the haze second pass; reports
  `hyacinth_under_haze_ha`)
- Quality light + regime — `quality.py`
- Exposed-bed correction — `hydro/bed.py`, using levels (`hydro/levels.py`,
  `smolf levels`), stage–area curve (`hydro/curve.py`, `smolf hydro curve`)
  and flood-stage map (`hydro/flood_map.py`, `smolf hydro flood-map`)
- Series builder + manifest — `series.py` (`smolf series build`, resumable;
  a series is identified by its `series.csv` sha256, not by date)
- Publish contract — `publish/` (`select`, `documents`, `aggregates`,
  `map`, `truecolour`, `writer`) + `schemas/` (`smolf publish`; neutral,
  versioned JSON + latest map + its Sentinel-2 true-colour photo and
  reservoir-clipped same-month photos, read from the items' COGs at
  publish time, from one complete run; `series.json` also carries the
  committed `stage_area_curve` and the 2024-06-09 `exposed_bed_example`
  with its raw and corrected PNGs; see
  `docs/contract.md`)
- Download bundle — `publish/export.py` (`smolf export series-csv`;
  `series.csv`, `series_monthly.csv`, `README.txt`, `LEEME.txt`,
  `reservoir_outline.zip` (`publish/outline.py`, the AOI as a shapefile)
  and `SHA256SUMS` from a verified publish; outside the contract, see
  `docs/downloads.md`)
- Per-scene maps — `publish/map_export.py` (`smolf export maps`; one
  published-format class map per scene with figures, rebuilt with
  `build_map` from the publish's run and the scene cache, checked against
  `series.json`, plus `index.csv`, `README.txt`, `SHA256SUMS`; outside the
  contract, see `docs/downloads.md`)
- Default write paths — `paths.py` (`SMOLF_DATA_DIR` relocates cache, outputs
  and the refreshed SIH level record)
- Experiments — `experiments/` (re-verification scripts + notes; per-scene
  outputs under `outputs/`)
- Frontier verification layer + agent — plugs in after the corrected
  figures (change comparison, seasonal baseline, claim levels);
  NOT BUILT YET.

Method and design rules live in `docs/method.md` and `docs/decisions.md`.

## Data layout

- `data/aoi/` — reservoir polygon (committed).
- `data/hydro/` — committed hydrology inputs, each with a provenance JSON
  sidecar: `ENDHD.csv[.json]`, `stage_area_curve.csv[.json]`,
  `flood_stage.tif[.json]`, `gsw_occurrence_endho_v1_4_2021.tif`. The
  `*.tif` there are force-added on purpose (`*.tif` is gitignored for
  cache/outputs).
- `SMOLF_DATA_DIR` (optional) relocates `data/cache/`, `outputs/` and the
  refreshed level record to a writable folder; it must be an absolute path
  outside the checkout (otherwise every command exits 1).
- `tests/reference/` — the author's reference series (CC BY 4.0):
  `…_v2026-11-19.csv` (468 scenes, with the haze rescue; the identity
  gate's reference) and `…_v2026-10-22.csv` (465 scenes, the rescue-off
  baseline for `smolf gate identity --baseline`), plus `gate_scenes.csv`.
- `data/cache/` — ingested scenes (gitignored, regenerable via
  `smolf fetch`).
- `outputs/` — classified GeoTIFFs (gitignored, regenerable via
  `smolf classify`).
- `outputs/series/` — series runs, one directory per run id with scene
  JSONs, `series.csv`, and `manifest.json` (gitignored, regenerable via
  `smolf series build`).
- `outputs/publish/` — the published data contract (`latest.json`,
  `series.json`, `publish.json`, the latest scene's map; gitignored,
  regenerable via `smolf publish`).
- `outputs/downloads/` — the download bundle derived from the publish
  (`series.csv`, `series_monthly.csv`, `README.txt`, `LEEME.txt`,
  `reservoir_outline.zip`, `SHA256SUMS`; gitignored, regenerable via
  `smolf export series-csv`).
- `outputs/maps/` — the per-scene class maps of the publish
  (`<date>_<item_id>.tif`, `index.csv`, `README.txt`, `SHA256SUMS`;
  gitignored, regenerable via `smolf export maps`).
- `$SMOLF_DATA_DIR/` — when set (read at call time by `paths.py`), holds
  every write instead: `cache/`, `outputs/…` (same subfolders as above,
  `outputs/downloads/` and `outputs/maps/` included) and
  `hydro/ENDHD.csv[.json]` (the refreshed SIH record; readers fall back to
  the committed copy until it exists). Other inputs stay in the checkout.

## Parameters and provenance

- Every threshold lives in `config.py` (or as a named module constant) with a
  comment explaining WHY it exists and HOW it was calibrated.
- Provenance from prior research points to `docs/provenance.md`.
- Never hard-code the reservoir area; it comes from `aoi.py` (computed from
  the polygon).

## Pipeline invariants

- Every default write path (and the level record's read path) is resolved by
  `paths.py`; no other module derives a write path from the repo root.
- Radiometry only in `sources/radiometry.py`; the BOA offset is computed once
  per scene from B02 and applied to every band.
- Re-implement the documented methodology (see docs/provenance.md); correctness
  is validated by an identity gate against the author's reference series.
  When the brief and your instincts disagree, the brief wins — flag the
  discrepancy instead of "fixing" it.
- Core principle for the verification layer and the agent: models never
  compute a number. Every quantity comes from a deterministic, tested
  function; the model selects tools, explains results, and abstains when
  data or quality are insufficient.

## Control values (golden numbers)

Any change that moves these must be flagged, not silently accepted
(`tests/test_regression_scenes.py` guards them when the scenes are cached):

Moved by #69 (haze rescue, v0.8.0; author's reference v2026-11-19). The
regression helper passes the day's water mirror, as `smolf classify` does:

- 2026-10-12: hyacinth 824.63 ha (dense 350.24, interface 474.39), of which
  6.29 under haze; water 415.86, soil 0.11, 65.36 % of 1261.75 ha; light
  GOOD; regime clean; exposed bed: applied, nothing removed. (Author:
  824.63 = dense 347.49 + interface 477.14, haze 6.29; same total, the
  dense/interface split gap predates #69.)
- 2024-06-09 (deep dry season, level 2004.60 m): raw 434.21 ha (1.96
  under haze) → corrected 15.70 ha (margin 2005.54 m, expected bed
  696.80 ha, removed 418.51 ha); light GOOD; regime EXPOSED_BED. (Earth
  Search copy: 434.35 → 15.66, haze 1.90; author: 434.28 → 15.63, haze
  1.90; the ~0.1 ha gap is the STAC backend.)
- 2023-01-21 (Earth Search): 158.18 ha, 86.91 under haze (author 86.88);
  light GOOD; regime TURBIDITY.
- 2026-11-08 (Earth Search): 780.95 ha, 61.89 %, 49.96 under haze; light
  GOOD.
- With `mirror=None` (rescue off) the v2026-10-22 values still hold:
  2026-10-12 818.34 ha (dense 350.24, interface 468.10, 64.86 %);
  2024-06-09 raw 432.25 → corrected 13.74 ha (author 432.38 → 13.73).
- `data/hydro/ENDHD.csv` sha256 `261180f6…` (committed refresh through
  2026-11-18; `tests/test_levels.py` checks this prefix and day); `stage_area_curve.csv` and
  `flood_stage.tif` are guarded by golden tests.

## Glossary

The author's prior research and the SIH data are in Spanish:

- lirio = water hyacinth; manto = floating mat; denso = dense hyacinth;
  interfase = interface (mat edge / sparse cover); vaso = reservoir basin
  (the AOI polygon); espejo de agua = open-water surface at the day's
  level; lecho expuesto = exposed bed; cota = water-surface stage
  (m a.s.l.); estiaje = dry season; curva cota–área = stage–area curve;
  azolve = sedimentation; semáforo = traffic light;
  BUENA/DUDOSA/MALA = GOOD/DOUBTFUL/POOR; régimen LECHO/TURBIDEZ =
  EXPOSED_BED/TURBIDITY; SIH = Sistema de Información Hidrológica
  (CONAGUA), Mexico's national hydrometric information system.

## Dependencies

- Pin dependencies exactly (`==` in `pyproject.toml`, resolved in `uv.lock`).
- Keep Python >= 3.11 compatibility: if the newest release of a dependency
  requires >= 3.12, pin the newest release that still supports 3.11.

## Tests

- No network in CI: mock HTTP (or the client boundary); live tests are
  opt-in, gated by `SMOLF_LIVE=1`.
- For rare code paths, test the pure function directly or monkeypatch —
  don't build fragile synthetic scenes for paths that are nearly
  impossible to trigger naturally.

## How to verify a change

- Run `ruff check .`, `ruff format --check .`, and `pytest -q`, then
  `smolf classify 2026-10-12` and compare with the control values above.

## Before finishing

- Run `ruff check .`, `ruff format --check .`, and `pytest -q`.
- Commits: create a commit only when the user asks; conventional commit
  messages; no trailers.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues of `oscolv/smol-frontier` (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: glossary in this file's `## Glossary` (until a root `GLOSSARY.md` exists), decisions in `docs/decisions.md` and `docs/adr/`. See `docs/agents/domain.md`.
