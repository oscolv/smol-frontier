# Prior Work Disclosure

All code in this repository was written from scratch during the Meta Global AI
Developer Hackathon building period (October 27 – November 4, 2026), using Muse Code.
The first commit (`1ded43a`) is dated October 28, 2026; see `git log`.

This project builds on the author's own prior research, which is disclosed below.
No code from earlier projects was copied or imported.

## What existed before the hackathon

| Item | What it is | How it is used here |
|---|---|---|
| **SMOL research codebase** (private; mentioned on the hackathon Discord, Oct 27–28, 2026) | The author's 2024–2026 research pipeline for monitoring water hyacinth in Endhó Reservoir | **Not used.** Only its methodology (knowledge, not code) informed this rewrite |
| **Classification methodology** | Two-stage Gaussian Mixture Model on spectral indices, exposed-bed correction from reservoir level, quality criteria. Manuscript in preparation (O. Olvera Neria et al.) | Re-implemented from scratch; thresholds documented with provenance in `config.py` |
| **Reservoir polygon** (`data/aoi/`) | Created by the author from JRC Global Surface Water v1.4, occurrence ≥ 25 % (Pekel et al., 2016, *Nature* 540:418–422) | Area of interest; reservoir area computed from it |
| **Reference time series v2026-11-19** (`tests/reference/`) | The author's Sentinel-2 series with the haze rescue (468 scenes, 2017–2026; CC BY 4.0 — see `tests/reference/LICENSE`; manuscript in preparation). The gold standard | **Validation only:** the identity gate checks the pipeline against it. Never used as model input |
| **Reference time series v2026-10-22** (`tests/reference/`) | The same series before the haze rescue (465 scenes, 2017–2026; first released in this repository under CC BY 4.0) | **Validation only:** the baseline for `smolf gate identity --baseline` (rescue off). Never used as model input |
| **Haze rescue** (smol PR #45, merged 2026-11-19) | The author's rescue of water hyacinth under thin haze: reference functions `mascara.neblina_vegetal` (haze over vegetation), `lecho.espejo_del_dia` (the day's water mirror), `classifier._asignar_neblina` and `classify(..., neblina_mask=)` (the frozen second pass); experiment log `docs/cc/guarda_ndvi_neblina_2026-11-18.md` | Re-implemented from scratch (`classify/masks.py::haze_vegetation`, `hydro/bed.py::load_day_mirror`, `classify/gmm.py::haze_pass`); `CLOUD_NDVI_MAX` and its measured sensitivity in `config.py` |

The reference copies used by the identity gate are the author's reduced
validation extracts, with only the columns the gate reads — `fecha,
item_id, quality, regimen, valid_frac, lirio_denso_ha, interfase_ha,
lirio_total_ha, agua_ha, suelo_ha`:
`tests/reference/serie_2017-01-01_2026-12-31_v2026-11-19.csv` (468
scenes, plus `lirio_bajo_neblina_ha`, the hyacinth under haze; the default
reference) and `tests/reference/serie_2017-01-01_2026-12-31_v2026-10-22.csv`
(465 scenes; the `--baseline` reference). Areas are raw classifier output,
without exposed-bed correction. The fixed 30-scene gate sample
(`tests/reference/gate_scenes.csv`: `date, item_id, stratum`) was drawn
from v2026-10-22; all 30 item ids are also in v2026-11-19.

## Third-party data
- Sentinel-2 L2A: Copernicus Sentinel data (ESA), via Microsoft Planetary Computer / Earth Search.
- Reservoir water level: public records of Mexico's National Hydrometric Information System (SIH).

## Built during the hackathon
Everything else: downloader, radiometric normalization, classifier, hydrological
correction, quality assessment, the Frontier verification layer (change comparison,
seasonal baseline, claim levels), the agent, vision and voice components, report and CLI.
