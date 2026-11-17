# Classify one day

`smolf classify <date>` finds that day's Sentinel-2 scene, classifies the reservoir into water, dense hyacinth, interface and bare soil, applies the exposed-bed correction, prints the figures with the quality light and regime, and writes a classified GeoTIFF.

## Sub-features

- `classify-control` reproduces the 2026-10-12 control values (clean regime, nothing removed).
- `classify-dry-season` reproduces the 2024-06-09 exposed-bed correction.
- `classify-no-scene` reports a day with no scene and exits 1.

## How to get to it (user POV)

- Run `smolf classify YYYY-MM-DD` in a terminal.

## Driving it with smolf-verify

Preconditions:

- Doctor shows 200 for Earth Search and Planetary Computer. The scene search hits the network even when the scene is cached.
- Shared cache, or about 30 s per uncached scene.

- **Control day.** Run `$V run "$RUN" classify-control -- classify 2026-10-12`. `.rc` is `0`. `.out` has `water_ha: 415.86`, `dense_ha: 350.24`, `interface_ha: 468.10`, `soil_ha: 0.11`, `hyacinth_total_ha: 818.34 (raw 818.34)`, `pct_reservoir: 64.86 (raw 64.86)`, `reservoir_area_ha: 1261.75`, `light: GOOD`, `regime: clean`, `bed_removed_ha: 0.00`. The last line is `output: $RUN/data/outputs/<scene>_classified.tif`.
- **Map matches the figures.** Run `$V hectares "$RUN" <output path>`. You get `water_ha: 415.86`, `dense_hyacinth_ha: 350.24`, `interface_ha: 468.10`, `bare_soil_ha: 0.11`. Save the output next to the evidence (`... | tee "$RUN/evidence/<n>-classify-control.hectares"`).
- **Dry season.** Run `$V run "$RUN" classify-dry-season -- classify 2024-06-09`. `.rc` is `0`, with `hyacinth_total_ha: 13.74 (raw 432.25)`, `bed_level_m: 2004.60`, `bed_margin_m: 2005.54`, `bed_expected_ha: 696.80`, `bed_removed_ha: 418.51`, `light: GOOD`, `regime: EXPOSED_BED`.
- **No scene.** Run `$V run "$RUN" classify-no-scene -- classify 2026-10-13`. `.rc` is `1` and `.out` reads `no scenes found for 2026-10-13`. Nothing new appears in `$RUN/data/outputs/`.

## Gotchas

- The control values come from AGENTS.md "Control values". If they move, the change must be flagged, not accepted.
- The 2026-10-12 scene is served by Planetary Computer (`S2B_MSIL2A_…`). The bed correction reads the day's level from the level record, so a refreshed record can change `bed_level_m` but must not change the hectares on 2026-10-12.
- Without `SMOLF_DATA_DIR` the map goes to the repo's `outputs/`. Always drive through `smolf-verify` so it lands in the run.
- `boa_offset_applied looks wrong … using 0.0` on stderr is a known radiometry guard message, not a failure.
