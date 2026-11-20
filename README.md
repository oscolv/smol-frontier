# smol-frontier

smol-frontier measures how much of Endhó Reservoir, in Hidalgo, Mexico, is
covered by water hyacinth. It reads Sentinel-2 satellite scenes and reports
the covered area in hectares for each scene. The pipeline is deterministic,
and every input is public, so anyone can rerun it and get the same figures.

The current release is smolf 0.8.1. A verification layer and an agent that
explain change over time will run on top of these figures. They are not
built yet.

## What you get for each scene

`smolf classify` and `smolf series build` report these figures for every
Sentinel-2 scene of the reservoir:

- Dense hyacinth and interface hyacinth (mat edges and sparse cover), in
  hectares.
- Open water and exposed soil, in hectares.
- The hyacinth share of the reservoir. The reservoir area is 1261.75 ha,
  computed from the polygon in `data/aoi/`.
- A quality light, GOOD, DOUBTFUL, or POOR, that comes from the valid
  fraction, the cloud fraction, and how well the classifier separates water
  from vegetation.
- A regime, clean, TURBIDITY, or EXPOSED_BED, that tells you how to read the
  scene.
- Corrected figures. In the dry season, plants on the exposed lakebed look
  like hyacinth. The correction removes hyacinth above the day's water
  level, plus a safety margin, from the reported figures.

`smolf publish` turns one complete series run into versioned JSON files, a
map of the latest scene, and Sentinel-2 true-colour photos.
[`docs/contract.md`](docs/contract.md) describes every field.

## How the pipeline works

The pipeline runs these stages in order:

1. Search Microsoft Planetary Computer for Sentinel-2 L2A scenes of the
   reservoir. If that search fails, search Earth Search.
2. Download the bands, normalize the radiometry, and mask clouds and invalid
   pixels.
3. Classify every pixel with a two-stage Gaussian mixture model on spectral
   indices. A second pass rescues hyacinth under thin haze.
4. Assign the quality light and the regime.
5. Correct for the exposed bed. The correction uses the day's water level
   from Mexico's National Hydrometric Information System (SIH), a
   stage–area curve, and a per-pixel flood-stage map.

[`docs/method.md`](docs/method.md) explains each stage and gives every
threshold with its source. [`docs/decisions.md`](docs/decisions.md) records
why the design is the way it is.

## The figures match the author's reference series

This code reimplements the author's earlier research method from scratch.
An identity gate checks it against the author's reference series of 468
scenes from 2017 to 2026. On 2026-11-19 the full gate gave these results:

- Median difference in total hyacinth: 0.01 ha.
- 95th percentile: 0.11 ha.
- Largest difference: 0.43 ha, on 2023-10-28.
- Quality light and regime: the same on all 468 scenes.

To rerun the full gate, use `uv run smolf gate identity --all`.

## Install

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or later. Commands
that read scenes or water levels need network access.

```sh
git clone https://github.com/oscolv/smol-frontier.git
cd smol-frontier
uv sync
```

## Classify one day

Run the classifier on 2026-10-12, the project's control day:

```sh
uv run smolf classify 2026-10-12
```

The command downloads the scene, classifies it, and writes a GeoTIFF of the
classes under `outputs/`. The summary on stdout includes these lines:

```text
dense_ha: 350.24
interface_ha: 474.39
hyacinth_total_ha: 824.63 (raw 824.63)
pct_reservoir: 65.36 (raw 65.36)
light: GOOD
regime: clean
```

The number in parentheses is the figure before the exposed-bed correction.
On this day the reservoir is full, so the correction removes nothing.

`smolf classify` refuses a day after the last day of the local SIH record.
To classify a recent day, run `uv run smolf levels update` first.

## Commands

| Command | What it does |
| --- | --- |
| `smolf fetch <date>` | Download one day's scene into `data/cache/`. |
| `smolf classify <date>` | Classify one day, print the summary, and write a GeoTIFF. |
| `smolf levels update` | Refresh the SIH water-level record. |
| `smolf levels show <date>` | Print the water level for one exact day. Levels are never interpolated. |
| `smolf hydro curve` | Build the stage–area curve. |
| `smolf hydro flood-map` | Build the per-pixel flood-stage map. |
| `smolf series build` | Build the hyacinth time series. `--extend` adds scenes from 2026-10-13 on that the reference series lacks. `--resume <run_id>` continues an interrupted run. |
| `smolf gate identity` | Compare a 30-scene sample with the reference series. `--all` compares every scene. `--baseline` turns the haze rescue off and compares with the earlier series v2026-10-22. |
| `smolf publish --latest` | Publish the newest complete series run. `--run <run_id>` names a run instead. |
| `smolf version` | Print the package version. |

Prefix each command with `uv run`. Run `uv run smolf <command> --help` for
every option.

To run the tests, use `uv run pytest`. The tests make no network calls.
Live tests run only when `SMOLF_LIVE=1` is set.

## Run it as a daily job

`smolf` can run on a schedule from a read-only checkout. Set
`SMOLF_DATA_DIR` to a writable folder, then run the three steps:

```sh
export SMOLF_DATA_DIR=/path/to/writable/data
uv run --frozen --no-sync smolf levels update
uv run --frozen --no-sync smolf series build --extend
uv run --frozen --no-sync smolf publish --latest
```

`SMOLF_DATA_DIR` must be an absolute path outside the checkout. Otherwise
every command prints `error: …` and exits 1. When the variable is unset or
empty, `smolf` writes inside the checkout.

With `SMOLF_DATA_DIR` set, every write goes under that folder:

| Path | Contents |
| --- | --- |
| `cache/` | Downloaded scenes. |
| `outputs/` | Classified GeoTIFFs. |
| `outputs/series/<run_id>/` | Series runs: one JSON file per scene, `series.csv`, and `manifest.json`. |
| `outputs/publish/` | The published JSON files, map, and photos. |
| `outputs/identity_gate/` | Identity gate reports. |
| `hydro/ENDHD.csv` and `hydro/ENDHD.csv.json` | The refreshed SIH level record and its provenance file. |

Until `hydro/ENDHD.csv` exists there, `smolf` reads the committed
`data/hydro/ENDHD.csv`. The first `levels update` checks the download
against that copy. The other inputs stay read-only in the checkout.

`smolf series build` uses these exit codes, so a scheduler can decide what
to do next:

| Exit code | Meaning |
| --- | --- |
| 0 | The run is complete. |
| 2 | A precondition failed and nothing was written. The level record is out of date, the catalog of new scenes is unreachable, or an argument is invalid. Retry later. |
| 130 | The run was interrupted. Continue it with `--resume <run_id>`. |
| 1 | Any other error, including a resume after the code changed. |

Errors and refusals go to stderr. [`docs/method.md`](docs/method.md) §9
gives the details.

Before you run the job from a read-only checkout, check these points:

- Build the environment once. Then run every command as
  `uv run --frozen --no-sync smolf …`, or as `.venv/bin/smolf …`, so that
  `uv` does not try to change `.venv` inside the checkout. To put the
  environment somewhere else, set `UV_PROJECT_ENVIRONMENT`.
- Install `git`, and keep the checkout's `.git` folder. Each series run
  records the commit of the code that produced it.
- If the job runs as a different user than the owner of the checkout, git
  refuses the repository. Allow it once with
  `git config --global --add safe.directory <checkout>`.

## Data and licenses

The code is under the MIT license. See [`LICENSE`](LICENSE).

The pipeline uses these data sources. Check each provider's terms before
you reuse the data.

- Sentinel-2 L2A: Copernicus Sentinel data from ESA, free and open, read
  through Microsoft Planetary Computer or Earth Search.
- Reservoir water level: public records of Mexico's National Hydrometric
  Information System (SIH).
- Surface-water occurrence: JRC Global Surface Water v1.4 from the European
  Commission's Joint Research Centre.
- Reference series in `tests/reference/`: the author's data, under CC BY 4.0.
  See [`tests/reference/LICENSE`](tests/reference/LICENSE). Version
  v2026-11-19 has 468 scenes and includes the haze rescue. Version
  v2026-10-22 has 465 scenes and comes before the haze rescue.

[`docs/provenance.md`](docs/provenance.md) describes the author's prior
research and how this project uses it.
