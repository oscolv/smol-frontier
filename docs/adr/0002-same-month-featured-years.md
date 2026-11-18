# 0002. Mark four featured years in same_month, chosen without hectares

- **Status:** accepted (contract 3.1.0, smolf 0.6.0; spec #56, issue #57)
- **Amends:** contract 3.0.0's "the consumer chooses which years to show"
  (`docs/contract.md`, "Same month, every year")

## Context

`latest.json`'s `same_month` block carries one representative scene and
one reservoir-clipped Sentinel-2 photo per year of the series: ten years
for 2017–2026, one more every year. The downstream homepage wants four
photos it can read at a glance: the current year plus three earlier
years.

Contract 3.0.0 left the choice to the consumer. That puts a sampling rule
a reader can question ("why those years?") in front-end code, untested
and undocumented, and every consumer would re-implement it.

## Decision

- smolf marks the years itself: each `same_month.years[]` entry carries a
  boolean `featured`, optional in the schema and always written.
- The rule spans the record: the current year (the last entry), and the
  first, middle and last of the calendar years before it (index 0,
  ⌊(n−1)/2⌋ and n−1 over the n prior years, empty years included).
  Targets resolve in the order first, last prior, middle; an empty or
  already featured target falls back to a prior year with a scene (first
  forward, last prior backward, middle to the nearest with ties to the
  earlier year), or stays empty. For 2017–2026 it features 2017, 2021,
  2025 and 2026.
- The rule reads only scene counts, never hectares. Choosing years by
  value would editorialize: the block could be accused of picking the
  years that tell a story. Spanning the record keeps the baseline (what
  the reservoir looked like at the start) next to last year and the
  current year.
- The rule lives in the pure `same_month()` aggregate, which already
  holds each year's `n_scenes`; no new module.
- Every year keeps its hectares and its clipped photo, so the change is
  additive (minor version).

## Alternatives rejected

- **The last three years before the current one.** Readable, but drops
  the start of the record, so the block shows recent noise rather than
  the change since the series began.
- **Fixed lags (1, 3 and 5 years back).** Arbitrary numbers a reader
  would question, and the oldest year still drifts away from the start
  of the record as the series grows.
- **Selection by value or change point** (the extreme years, the biggest
  jump). Editorializes the block, and judging change belongs to the
  verification layer, not to a display flag.
- **Contract 4.0.0: photos only for featured years** (`image: null`
  otherwise; 4 COG reads instead of 10). Breaking for the consumer, whose
  migration cost outweighs ~50 s of publish time and a few tens of KB.
- **A top-level list of featured years.** Duplicates information and
  could name a year missing from `years`.

## Consequences

- Consumers render `featured: true` years without a rule of their own;
  the schema guarantees each has a `scene` and an `image`.
- At most four years are featured; fewer only when fewer than three prior
  years have a scene. JSON Schema cannot express the bound, so smolf's
  tests enforce it.
- The featured set moves as the series grows (the middle year shifts and
  the last prior year follows the calendar); it never moves when only
  hectares change.
