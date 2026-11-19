# True-colour gamma for the published photos (issue #60)

Chooses the gamma that `smolf publish` applies to the Sentinel-2
true-colour photos (the `latest.json` photo and the same-month photos).
This step changes no classification result and no published figure; it only
picks the value of `TRUE_COLOUR_GAMMA`.

Command (run from the repo root; four COG reads over the network, up to
~30 s each):

    uv run python experiments/truecolour_gamma.py \
        --contract-dir outputs/publish --gammas 1.0 1.4 1.6 2.2

## Why a gamma at all

The published photos use one fixed linear window, reflectance 0–0.30 →
0–255, the same for every image so dates and years compare visually
(`TRUE_COLOUR_MIN`/`MAX`, ADR 0001). L2A reflectance of the reservoir is
low: the median inside the polygon is 0.015–0.025 in red and blue and
0.02–0.045 in green on the four photos below. Without gamma, the reservoir
renders at 6–9 % mean brightness: the water is near black and the
hyacinth mat dark green.

The request in #60 was to copy smol's `export_rgb` stretch (per-image,
per-channel p2/p98 plus gamma 1/2.2). The per-image percentiles were
rejected because they break the cross-date comparison above. Only the gamma
was kept, applied after the fixed window: `x = clip((v - MIN) / (MAX -
MIN), 0, 1) ** (1/gamma)`. This decision is recorded in the #60 comments.

## Method

`experiments/truecolour_gamma.py` reads the contract's `latest.json`, takes
the four `same_month` years marked `featured`, and reads B04/B03/B02 again
from each item's COGs through `publish.truecolour.read_true_colour` (the
same radiometry and BOA-offset guard as `smolf publish`). It rebuilds the
web-Mercator grid from the `latest.json` photo's `bounds` and pixel size.
Each photo is clipped to the reservoir polygon with the validity alpha
proposed in #60: a pixel is opaque when it is inside the polygon and any
band is > −0.15. It is rendered once per gamma.

`--backend` must be the run's STAC backend (Earth Search by default), the
one `smolf publish` read the contract's photos from.

The rebuilt grid and read are checked on every run: at gamma 1 the render
must reproduce the contract's own clipped photos. On the contract below the
alpha agrees on 99.996 % of pixels (a few edge pixels) and the mean RGB
difference is 0.86–1.34 / 255, the size of the WebP quality-90 loss.

Outputs (git-ignored, `<outputs>/truecolour_gamma/`): `montage.png` (one
row per gamma, one column per year, on the site's background) and
`brightness.csv` (mean RGB brightness over the opaque pixels).

## Results

Contract published 2026-11-06 (latest scene 2026-11-01, month 11; featured
years 2017, 2021, 2025, 2026). Mean brightness inside the reservoir, % of
255:

| gamma | 2017 (7 ha) | 2021 (5 ha) | 2025 (783 ha) | 2026 (735 ha) |
|---|---|---|---|---|
| 1 (current) | 6.8 | 6.4 | 9.0 | 7.3 |
| 1/1.4 | 14.3 | 13.8 | 17.3 | 14.9 |
| **1/1.6** | **18.2** | **17.5** | **21.3** | **18.7** |
| 1/2.2 | 28.7 | 28.0 | 32.1 | 29.2 |

No pixel inside the polygon fell under the −0.15 validity threshold on
these scenes, so the alpha rule changed nothing here.

Visual reading of the montage:

- **1** — the water is near black and the mat dark green; the sparse strips
  of 2017 and 2021 are barely visible.
- **1/1.4** — the mat reads green, but 2017 and 2021 still look much like
  the current photos.
- **1/1.6** — the mat, its pale edges and the open-water channels inside it
  are clear. The water stays dark with a blue tint, and the sparse strips of
  2017 and 2021 show.
- **1/2.2** (smol's value) — the most detail, but the clean-water years
  turn a milky grey-green that reads as haze, and the clean-vs-infested
  contrast against the page background drops.

## Decision

`TRUE_COLOUR_GAMMA = 1.6`, chosen by the maintainer on 2026-11-18 from the
montage above. It is applied to true colour only, never to a NIR/SWIR
composite. The `config.py` comment should cite this note as its HOW.
