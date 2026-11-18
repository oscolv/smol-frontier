# 0001. Publish Sentinel-2 true colour, never PlanetScope

- **Status:** accepted (contract 3.0.0, smolf 0.5.0; spec #44, issue #48)
- **Amends:** `docs/decisions.md` entry 13 ("leaking source imagery
  impossible by construction")

## Context

The downstream site wants a natural-colour photo of the current scene that
it can toggle with the classified map, and, per year, a reservoir-clipped
photo of a representative scene. Decision 13 called publishing source
imagery "impossible by construction": the raster guard accepts only a uint8
class map, and the allowlist names no imagery file.

That ban was written for PlanetScope, whose licence does not allow
redistributing its imagery; a publisher that could write imagery could
leak it. smolf's own pipeline reads only Sentinel-2 L2A, which the
Copernicus licence lets anyone redistribute, free of charge, provided the
source is attributed ("Copernicus Sentinel data <year>").

## Decision

- smolf publishes Sentinel-2 true colour and nothing else as imagery.
- A separate writer (`publish.truecolour.write_true_colour_webp()`) is the
  only way an image reaches the contract. Its guard
  (`guard_true_colour()`) refuses any array that is not uint8, does not
  have 3 (RGB) or 4 (RGBA, clipped) bands, or does not come from STAC
  collection `sentinel-2-l2a`. The class-map guard is unchanged, so the
  map writers still accept classes 0–5 only.
- The allowlist grows by exactly two patterns,
  `image/<date>_<item_id>.webp` and `image/clipped/<date>_<item_id>.webp`,
  in the writer and in `publish.json`'s closed `files[].path` pattern.
- Every image carries its attribution string in the contract.
- Images are read at publish time from the item's COGs (B04/B03/B02,
  radiometry module, one BOA offset from B02, bilinear warp onto the
  `map.png` grid), not from the scene cache: the cache is zeroed outside
  the reservoir, and changing ingest would re-hash every cached scene.
- Images are illustrations bound to `item_id`. They are listed with their
  sha256 in `publish.json` but, unlike the map, are not verified against
  the run. A read failure refuses the publish (exit 2) so a contract is
  never partial.

## Consequences

- PlanetScope (or any other collection) cannot reach the contract even if
  a future reader returned it: the collection check sits in the writer.
- Raw reflectance cannot be published either: the guard demands uint8, and
  only the fixed stretch in `config.py` produces it.
- Publish gains a network dependency on the STAC catalog for the latest
  scene; a catalog outage is a refusal that keeps the previous contract.
- A backend that reprocesses an item can change its photo between two
  publishes of the same run; no published figure changes with it.
