# smol-frontier — agent conventions

## Language

- Everything in the repository is in English — code, comments, docs, CLI
  output, column and file names.
- Exceptions: third-party data kept byte-for-byte (the SIH CSV headers stay
  as published) and proper nouns (Endhó, SIH).

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
- `classify()` — `classify/pipeline.py` (`smolf classify`)
- Quality light + regime — `quality.py`
- Exposed-bed correction — `hydro/bed.py`, using levels (`hydro/levels.py`,
  `smolf levels`), stage–area curve (`hydro/curve.py`, `smolf hydro curve`)
  and flood-stage map (`hydro/flood_map.py`, `smolf hydro flood-map`)
- Series builder + manifest — `series.py` (`smolf series build`, resumable;
  a series is identified by its `series.csv` sha256, not by date)
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
- `data/cache/` — ingested scenes (gitignored, regenerable via
  `smolf fetch`).
- `outputs/` — classified GeoTIFFs (gitignored, regenerable via
  `smolf classify`).
- `outputs/series/` — series runs, one directory per run id with scene
  JSONs, `series.csv`, and `manifest.json` (gitignored, regenerable via
  `smolf series build`).

## Parameters and provenance

- Every threshold lives in `config.py` (or as a named module constant) with a
  comment explaining WHY it exists and HOW it was calibrated.
- Provenance from prior research points to `PRIOR_WORK.md`.
- Never hard-code the reservoir area; it comes from `aoi.py` (computed from
  the polygon).

## Pipeline invariants

- Radiometry only in `sources/radiometry.py`; the BOA offset is computed once
  per scene from B02 and applied to every band.
- Re-implement the documented methodology (see PRIOR_WORK.md); correctness
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

- 2026-10-12: hyacinth 818.34 ha (dense 350.24, interface 468.10), water
  415.86, soil 0.11, 64.86 % of 1261.75 ha; light GOOD; regime clean;
  exposed bed: applied, nothing removed.
- 2024-06-09 (deep dry season, level 2004.60 m): raw 432.25 ha →
  corrected 13.74 ha (margin 2005.54 m, expected bed 696.80 ha, removed
  418.51 ha); light GOOD; regime EXPOSED_BED. (Author's pipeline, Earth
  Search backend: 432.38 → 13.73; the ~0.1 ha gap is the STAC backend.)
- `data/hydro/ENDHD.csv` sha256 `3728099d…`; `stage_area_curve.csv` and
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
  EXPOSED_BED/TURBIDITY; SIH = Mexico's national hydrometric information
  system.

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
  messages; no trailers. All code in this repository is written with Muse
  Code during the hackathon, as disclosed in README.md and PRIOR_WORK.md.
