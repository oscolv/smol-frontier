# smol-frontier

Independent, auditable verification of water-hyacinth coverage change in
Endhó Reservoir, Mexico. A deterministic Sentinel-2 pipeline (two-stage
GMM classifier, per-scene quality traffic light, SIH hydrology inputs, and
an exposed-bed correction) reports coverage figures any reviewer can
reproduce from public data; a Frontier verification layer and agent will
plug in on top (not built yet).

Prior research and third-party data are credited in `docs/provenance.md`.

## Install

Requires uv and Python ≥ 3.11:

```sh
uv sync
```

## Commands

```sh
uv run smolf fetch 2026-10-12        # download one day's scene into data/cache
uv run smolf classify 2026-10-12     # classify + quality + exposed-bed correction
uv run smolf levels update           # refresh the SIH water-level record
uv run smolf levels show 2026-10-12  # level for one exact day (no interpolation)
uv run smolf hydro curve             # build the stage–area curve
uv run smolf hydro flood-map         # build the per-pixel flood-stage map
uv run smolf series build            # build or resume the hyacinth time series
uv run smolf gate identity           # 30-scene sample vs the author's series v2026-11-19 (--all: every scene)
uv run smolf gate identity --baseline  # haze rescue off, vs the pre-rescue series v2026-10-22
uv run smolf publish --latest        # publish the newest complete run (or --run <id>)
uv run pytest                        # tests (no network; live tests need SMOLF_LIVE=1)
```

## Daily job

`smolf` can run as a scheduled daily job from a read-only code checkout:

```sh
export SMOLF_DATA_DIR=/path/to/writable/data
uv run --frozen --no-sync smolf levels update
uv run --frozen --no-sync smolf series build --extend
uv run --frozen --no-sync smolf publish --latest
```

With `SMOLF_DATA_DIR` set (read at run time; unset or empty keeps the repo
layout; it must be an absolute path outside the checkout, otherwise every
command fails with `error: …` and exit 1), every write goes under that folder:

```text
$SMOLF_DATA_DIR/
  cache/                  ingested scenes
  outputs/                classified GeoTIFFs
  outputs/series/<run>/   series runs (scene JSONs, series.csv, manifest.json)
  outputs/publish/        the published data contract
  outputs/identity_gate/  identity gate reports
  hydro/ENDHD.csv[.json]  the refreshed SIH level record and its sidecar
```

Until `hydro/ENDHD.csv` exists there, readers use the committed
`data/hydro/ENDHD.csv`, and the first `levels update` validates the
download against it. The other inputs (AOI, stage–area curve, flood-stage
map, GSW raster, reference series) stay read-only in the checkout.

`series build` exits 0 when the run completed, 2 on a precondition refusal
(nothing written, retry later: stale level record or new-scene catalog
unreachable; also its own argument checks, `--workers < 1` and `--extend`
with `--resume`), 130 on interrupt, and 1 on anything else, including a
resume whose code fingerprint changed; see `docs/method.md` §9. Errors and
refusals go to stderr. `publish --latest` takes the newest complete run;
use `--run <id>` to name one.

Read-only checkout notes:

- Build the environment once, then run the job as
  `uv run --frozen --no-sync smolf …` (or `.venv/bin/smolf …`) so `uv` does
  not try to sync or lock `.venv` inside the checkout;
  `UV_PROJECT_ENVIRONMENT` can put the venv elsewhere.
- The run's code fingerprint calls `git`, so `git` must be installed and
  the checkout's `.git` directory must be present.
- When the job runs under a different user than the checkout's owner, git
  refuses the repository; allow it once with
  `git config --global --add safe.directory <checkout>`.

## Data

- Sentinel-2 L2A: Copernicus Sentinel data (ESA), free and open access,
  via Microsoft Planetary Computer / Earth Search.
- Reservoir water level: public records of Mexico's National Hydrometric
  Information System (SIH).
- Surface-water occurrence: JRC Global Surface Water v1.4, publicly
  available from the European Commission's Joint Research Centre.
- Identity-gate reference series: author's data in `tests/reference/`, released under CC BY 4.0 (see `tests/reference/LICENSE`): v2026-11-19 (468 scenes, with the haze rescue; the gate's reference) and v2026-10-22 (465 scenes, before it; the `--baseline` reference).

See each provider's terms for reuse conditions. This repository's code is
MIT-licensed (LICENSE).
