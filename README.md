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
uv run pytest                        # tests (no network; live tests need SMOLF_LIVE=1)
```

## Data

- Sentinel-2 L2A: Copernicus Sentinel data (ESA), free and open access,
  via Microsoft Planetary Computer / Earth Search.
- Reservoir water level: public records of CONAGUA's National Hydrometric
  System (SIH).
- Surface-water occurrence: JRC Global Surface Water v1.4, publicly
  available from the European Commission's Joint Research Centre.

See each provider's terms for reuse conditions. This repository's code is
MIT-licensed (LICENSE).
