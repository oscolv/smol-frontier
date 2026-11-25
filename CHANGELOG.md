# Changelog

Notable changes to `smolf`, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every release says
what happened to the published data contract (`schema_version`, see
`docs/contract.md`), because that is what consumers check before upgrading.
Exports outside the contract are described in `docs/downloads.md`, design
rules in `docs/decisions.md`.

## [Unreleased]

## [0.13.0] - 2026-11-25

Contract: schema 3.4.0, unchanged.

### Changed

- `smolf export viewer` reuses the previous export's folders across runs
  (same smolf version and config): a scene whose map key and figures are
  unchanged is kept, and a rebuilt map keeps its photo when its photo key
  (item, backend, bounds, reservoir polygon) matches. A daily export after
  a new run reads only the new photos (#109). `index.json` gains
  `config_hash` and, per scene, `map_key` and `photo_key`. The first
  export after upgrading rebuilds every folder once.
- The committed SIH level record runs through 2026-11-24 (#106).

## [0.12.0] - 2026-11-25

Contract: schema 3.4.0, unchanged.

### Added

- `smolf export viewer`, the per-date viewer archive, outside the contract
  (#104, issue #102). It writes one folder per scene with figures:
  - `photo.webp`, built like `latest.image`;
  - `classified.webp`, lossless, decoded and checked against its classes;
  - `figures.json`, with the `series.json` fields, hectares and percent per
    class, and wet biomass.

  It also writes `index.json`, `README.txt` and `SHA256SUMS` into
  `outputs/viewer/`, and is incremental within a run.
- Wet biomass: corrected hyacinth hectares × 18.32 kg/m², with a range of
  11.67–23.84. The density is the UAM-A 2025 field measurement, and the
  figure appears in the viewer archive only (decision 20).

## [0.11.0] - 2026-11-23

Contract: schema 3.4.0, unchanged.

### Added

- `smolf export maps`: one published-format class map (classes 0–5) per
  scene with figures, checked against the run and `series.json`, with
  `index.csv` and `SHA256SUMS`, into `outputs/maps/` (#100, issue #96).
- `reservoir_outline.zip` in the `series-csv` bundle: the reservoir polygon
  as a shapefile (EPSG:4326, `area_ha` 1261.75, CC BY 4.0) (#100).

### Changed

- The repository is public: read-only CI token, local agent state ignored,
  README with author, citation and issue link (#92, #94).

## [0.10.0] - 2026-11-21

Contract: schema 3.4.0, additive.

### Added

- `series.json` `stage_area_curve`: the committed stage–area curve in
  hectares with bootstrap bounds, the SRH V(h) anchors and its sha256. Every
  publish checks it against the run (#88, issue #85).
- `series.json` `exposed_bed_example`: the 2024-06-09 dry-season control,
  with its raw, removed and corrected hectares, level, margin and expected
  bed, plus raw and corrected PNG maps (#89).

### Fixed

- `--extend` help names `EXTEND_START`; the bundle docs list `LEEME.txt`
  (#82).

## [0.9.0] - 2026-11-20

Contract: schema 3.3.0, unchanged.

### Added

- `smolf export series-csv`, the download bundle, outside the contract
  (#80, issue #76). It writes `series.csv`, `series_monthly.csv`,
  `README.txt` and `LEEME.txt` (CC BY 4.0, English and Spanish) and
  `SHA256SUMS`.

### Changed

- README rewritten for the public GitHub page; prior-work provenance moved
  to `docs/provenance.md` (#74, #75).

## [0.8.1] - 2026-11-19

Contract: schema 3.3.0, unchanged.

### Fixed

- `series build --extend` searches new scenes from the fixed
  `EXTEND_START` (2026-10-13), not from the reference's last day. This
  restores 2026-10-19 and 2026-11-06, which 0.8.0 runs missed (#72).

## [0.8.0] - 2026-11-19

Contract: schema 3.3.0, additive.

### Added

- Haze rescue: a frozen second pass recovers water hyacinth under thin haze
  inside the day's water mirror (#71, issue #70; decision 17).
- `hyacinth_under_haze_ha` in `series.json` and `latest.json`.
- `smolf gate identity --baseline` compares against the rescue-off
  reference.

### Changed

- **Control values moved.** The identity gate's reference is now the
  author's v2026-11-19 series (2026-10-12: 818.34 → 824.63 ha). See
  AGENTS.md, Control values.
- The committed SIH level record runs through 2026-11-18 (#68).

## [0.7.1] - 2026-11-18

Contract: schema 3.2.0, unchanged.

### Fixed

- `smolf classify` refuses (exit 2, before any catalog search) a day newer
  than the local SIH level record: run `smolf levels update` first (#66,
  issue #65).

## [0.7.0] - 2026-11-18

Contract: schema 3.2.0.

### Changed

- Every published Sentinel-2 photo is RGBA, clipped to the reservoir and
  transparent on no-data pixels, with gamma 1.6 after the fixed 0–0.30
  window (#62, issue #60; experiment in #61).

## [0.6.0] - 2026-11-18

Contract: schema 3.1.0, additive.

### Added

- `same_month.years[].featured` marks up to four representative years for
  the homepage (#58).

## [0.5.0] - 2026-11-18

Contract: schema 3.0.0, **breaking**.

### Added

- Homepage data (spec #44):
  - every scene carries `display_status` (#50);
  - `display_ha` and hectare cut-offs (#51);
  - `latest.json` carries a Sentinel-2 true-colour image (#52);
  - `series.json` carries monthly medians (#53);
  - `latest.json` carries the same-month block (#54).

## [0.4.0] - 2026-11-17

Contract: schema 2.1.0, additive.

### Added

- `latest.json` carries the reservoir outline (`reservoir_outline`) (#43,
  issue #42).

## [0.3.0] - 2026-11-16

Contract: schema 2.0.0, **breaking**.

### Changed

- `removed_ha` follows `bed_applied`: it is `null` when the correction did
  not apply (#40, issue #38).
- `publish` is a `publish/` package with hardened `--out` handling (#39).

### Added

- `SMOLF_DATA_DIR` relocates every write, and an unattended daily job gets
  exit-2 refusals that mean "retry later" (#21).

### Fixed

- `smolf publish` needs an explicit run; a resume fingerprint mismatch is a
  non-retryable exit; errors go to stderr (#23).
- A catalog outage in `classify` and `fetch` is one error line with exit 2
  (#27).
- `AOI_PATH` is excluded from `config_hash`, so a moved checkout can resume
  a run (#28).

## [0.2.0] - 2026-11-16

Contract: schema 1.0.0, first release.

### Added

- The classification pipeline: validity masks and indices, two-stage GMM
  with rescue and substrate test, quality light and regime, and the
  exposed-bed correction from the SIH level record, stage–area curve and
  flood-stage map.
- The resumable series builder with a per-run manifest, and the identity
  gate against the author's reference series (CC BY 4.0).
- `smolf publish`, the versioned data contract (#11).

[Unreleased]: https://github.com/oscolv/smol-frontier/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/oscolv/smol-frontier/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/oscolv/smol-frontier/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/oscolv/smol-frontier/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/oscolv/smol-frontier/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/oscolv/smol-frontier/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/oscolv/smol-frontier/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/oscolv/smol-frontier/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/oscolv/smol-frontier/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/oscolv/smol-frontier/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/oscolv/smol-frontier/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/oscolv/smol-frontier/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/oscolv/smol-frontier/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/oscolv/smol-frontier/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/oscolv/smol-frontier/releases/tag/v0.2.0
