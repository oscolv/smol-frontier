# Threshold sensitivity re-verification (step 9z)

Re-verifies the prior-research threshold claims with this repo's code, over
the 465 reference scenes. This step changes no threshold value and no
classification result.

Command (run from the repo root; ~11 min wall with 8 workers):

    uv run python experiments/sensitivity.py --workers 8

## Method

`experiments/sensitivity.py` reads every reference scene from `data/cache`
and runs the raw pipeline — `classify()` + quality light + regime, no
exposed-bed correction, the same path as `smolf gate identity --all` — under
9 configurations: TAU_WATER in 0.50–0.99 with TAU_HYACINTH = 0.55, and
TAU_HYACINTH in 0.45/0.55/0.65 with TAU_WATER = 0.80, with (0.80, 0.55) as
the shared baseline. Configurations run sequentially, one override at a
time; scenes within a configuration run concurrently.

`Settings` is a frozen dataclass read as a module global, so each override
is applied only inside the experiment script via `dataclasses.replace` plus
patching the `settings` module attribute in `config.py` (defines the fields)
and `classify/gmm.py` (the only reader of TAU_WATER/TAU_HYACINTH — verified
by grep; `stage1`/`stage2` bind `s = settings` at call time). The originals
are restored after each configuration. No production code path gains a
parameter or changes behavior.

Per-scene results (4185 rows: 465 scenes x 9 configurations) go to
`outputs/sensitivity/sensitivity.csv`, and the cloud on/off rows to
`outputs/sensitivity/cloud.csv`; both are git-ignored and regenerable with
the command above. Only the script and this note are committed.

## Controls

1. The baseline configuration (0.80, 0.55) reproduces the reference run
   `outputs/identity_gate/gate_20261028T212616.csv` with max |Δ total| =
   0.0000 ha over all 465 scenes. The experiment path is the gate path.
2. Every override demonstrably reached the classifier: each non-baseline
   configuration changed class pixel counts in 309–462 of 465 scenes (table
   below). No configuration had zero changed scenes, so no extra check was
   needed; the override was additionally verified directly for every
   configuration by asserting the patched `gmm.settings` values inside the
   run (the script records the effective values per row in the CSV).

## Results vs baseline

|Δ| figures in ha; "recent" is the 107 scenes from 2024-11-01 onward. No
configuration changed any scene's light or regime (0/465 and 0/107 in all 8
rows).

| config (water, hyacinth) | changed / 465 | \|Δ total\| all: med / p95 / max (date) | \|Δ total\| recent: med / p95 / max (date) |
|---|---|---|---|
| (0.50, 0.55) | 326 | 0.00 / 1.13 / 7.92 (2019-04-17) | 0.00 / 0.15 / 2.46 (2025-08-18) |
| (0.60, 0.55) | 315 | 0.00 / 0.90 / 3.98 (2019-04-17) | 0.00 / 0.11 / 1.52 (2025-08-18) |
| (0.70, 0.55) | 309 | 0.00 / 0.50 / 1.60 (2023-05-01) | 0.00 / 0.03 / 0.78 (2025-08-18) |
| (0.90, 0.55) | 316 | 0.00 / 0.93 / 6.78 (2023-05-01) | 0.00 / 0.13 / 0.95 (2025-08-18) |
| (0.95, 0.55) | 322 | 0.00 / 2.03 / 13.28 (2023-05-01) | 0.00 / 0.26 / 1.82 (2025-08-18) |
| (0.99, 0.55) | 338 | 0.00 / 5.62 / 22.68 (2023-05-01) | 0.00 / 1.66 / 3.99 (2025-12-26) |
| (0.80, 0.45) | 460 | 0.00 / 0.00 / 0.00 (none moved) | 0.00 / 0.00 / 0.00 (none moved) |
| (0.80, 0.65) | 462 | 0.00 / 0.00 / 0.00 (none moved) | 0.00 / 0.00 / 0.00 (none moved) |

| config (water, hyacinth) | \|Δ dense\| all: med / p95 / max (date) | \|Δ dense\| recent: med / p95 / max (date) |
|---|---|---|
| (0.50, 0.55) | 1.48 / 32.57 / 281.69 (2025-04-27) | 14.18 / 126.16 / 281.69 (2025-04-27) |
| (0.60, 0.55) | 1.08 / 22.56 / 277.82 (2025-04-27) | 9.35 / 116.91 / 277.82 (2025-04-27) |
| (0.70, 0.55) | 0.65 / 12.28 / 143.56 (2026-05-15) | 4.96 / 33.36 / 143.56 (2026-05-15) |
| (0.90, 0.55) | 1.15 / 13.92 / 276.68 (2024-12-26) | 6.09 / 49.74 / 276.68 (2024-12-26) |
| (0.95, 0.55) | 1.66 / 24.65 / 318.12 (2024-12-26) | 11.07 / 109.51 / 318.12 (2024-12-26) |
| (0.99, 0.55) | 3.55 / 50.97 / 343.62 (2024-12-26) | 20.68 / 214.82 / 343.62 (2024-12-26) |
| (0.80, 0.45) | 1.75 / 19.96 / 124.54 (2025-03-11) | 13.72 / 40.85 / 124.54 (2025-03-11) |
| (0.80, 0.65) | 1.85 / 25.59 / 107.59 (2024-12-31) | 16.84 / 57.21 / 107.59 (2024-12-31) |

| config (water, hyacinth) | \|Δ interface\| all: med / p95 / max (date) | \|Δ interface\| recent: med / p95 / max (date) |
|---|---|---|
| (0.50, 0.55) | 1.41 / 32.57 / 281.69 (2025-04-27) | 14.18 / 126.16 / 281.69 (2025-04-27) |
| (0.60, 0.55) | 1.06 / 22.56 / 277.82 (2025-04-27) | 9.35 / 116.91 / 277.82 (2025-04-27) |
| (0.70, 0.55) | 0.60 / 12.21 / 143.56 (2026-05-15) | 4.81 / 33.34 / 143.56 (2026-05-15) |
| (0.90, 0.55) | 0.93 / 13.92 / 276.68 (2024-12-26) | 6.09 / 49.74 / 276.68 (2024-12-26) |
| (0.95, 0.55) | 1.54 / 24.58 / 318.12 (2024-12-26) | 11.07 / 109.15 / 318.12 (2024-12-26) |
| (0.99, 0.55) | 3.17 / 50.44 / 343.62 (2024-12-26) | 20.63 / 214.61 / 343.62 (2024-12-26) |
| (0.80, 0.45) | 1.75 / 19.96 / 124.54 (2025-03-11) | 13.72 / 40.85 / 124.54 (2025-03-11) |
| (0.80, 0.65) | 1.85 / 25.59 / 107.59 (2024-12-31) | 16.84 / 57.21 / 107.59 (2024-12-31) |

TAU_WATER moves dense/interface by hundreds of hectares at the extremes
because shifting the water/mat boundary changes the mat pixel set that
Stage 2 then splits; the total stays small in typical scenes.

## Verdicts

- TAU_WATER "moves the total by at most 7 ha over 0.50–0.99": does NOT
  hold. Max |Δ total| vs baseline reaches 22.68 ha at 0.99 (2023-05-01),
  13.28 ha at 0.95 (same scene), and 7.92 ha at 0.50 (2019-04-17); 15 of
  465 scenes exceed 7 ha at some grid point, all March–June dry-season
  scenes. The threshold is still stable in typical scenes (median 0.00 ha
  at every grid point, p95 at most 5.62 ha) and perfectly stable for
  quality (no light or regime change anywhere); over the mid-range
  0.60–0.90 the max is 6.78 ha. The "7 ha" figure is dropped from the
  config comment, which now carries the measured figures.
- TAU_HYACINTH "only reallocates between dense and interface": holds
  exactly. Total hyacinth is bit-identical to baseline in all 465 scenes
  at both 0.45 and 0.65 (max |Δ total| 0.0000 ha), while dense/interface
  move up to 124.54 ha (2025-03-11 at 0.45) and 107.59 ha (2024-12-31 at
  0.65), always in opposite directions.

## Spectral cloud test on/off

Same script, same cache-only path; the test is disabled only inside the
experiment by patching `masks.spectral_cloud` to all-False (restored
afterwards). Figures are raw pipeline output (no bed correction).

| scene | spectral | valid_frac | cloud_frac | total (ha) | light | regime |
|---|---|---|---|---|---|
| 2025-07-29 (`S2B_14QMH_20250729_0_L2A`, haze) | on | 0.5462 | 0.4538 | 451.64 | POOR | clean |
| 2025-07-29 (`S2B_14QMH_20250729_0_L2A`, haze) | off | 0.9806 | 0.0194 | 998.79 | GOOD | clean |
| 2025-07-24 (`S2C_14QMH_20250724_0_L2A`, control) | on | 1.0000 | 0.0000 | 486.41 | GOOD | TURBIDITY |
| 2025-07-24 (`S2C_14QMH_20250724_0_L2A`, control) | off | 1.0000 | 0.0000 | 486.44 | GOOD | TURBIDITY |

Without the test the haze scene passes as valid (0.9806) with 998.79 ha of
false hyacinth; the test removes it (43.44 % of reservoir pixels flagged
that SCL did not flag) and the scene becomes POOR. The control is
essentially untouched (+0.03 ha, 3 pixels; spectral-only fraction 0.0000).

Prior figures for 2025-07-29 without the test were valid_frac 0.98 and
1025 ha: the valid fraction reproduces (0.9806), the total does not —
this repo measures 998.79 ha on both backend copies (Earth Search and
Planetary Computer agree to 0.01 ha). The masks comment now carries the
measured figures.
