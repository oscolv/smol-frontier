# smol-frontier

Independent, auditable verification of water-hyacinth coverage change in
Endhó Reservoir, Mexico. A deterministic Sentinel-2 pipeline (two-stage
GMM classifier, per-scene quality traffic light, SIH hydrology inputs, and
an exposed-bed correction) reports coverage figures any reviewer can
reproduce from public data; a Frontier verification layer and agent will
plug in on top (not built yet).

Built during the Meta Global AI Developer Hackathon 2026 with Muse Code;
prior research disclosed in PRIOR_WORK.md.

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
uv run smolf publish                 # publish the versioned data contract
uv run pytest                        # tests (no network; live tests need SMOLF_LIVE=1)
```

## Daily job

`smolf` can run as a scheduled daily job from a read-only code checkout:

```sh
export SMOLF_DATA_DIR=/path/to/writable/data
uv run smolf levels update
uv run smolf series build --extend
uv run smolf publish
```

With `SMOLF_DATA_DIR` set (read at run time; unset or empty keeps the repo
layout), every write goes under that folder:

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
(nothing written, retry later: stale level record, new-scene catalog
unreachable, resume fingerprint mismatch) and 1 on anything else; see
`docs/method.md` §9.

Read-only checkout notes:

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
- Identity-gate reference series: author's data in `tests/reference/`, released under CC BY 4.0 (see `tests/reference/LICENSE`).

See each provider's terms for reuse conditions. This repository's code is
MIT-licensed (LICENSE).
