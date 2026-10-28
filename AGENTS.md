# smol-frontier — agent conventions

## Layout and toolchain

- Package layout: `src/smol_frontier/`; CLI is `smolf` (Typer).
- Run everything with `uv run` (`uv run pytest`, `uv run ruff check .`,
  `uv run smolf ...`). The environment lives in `.venv/` at the repo root.

## Parameters and provenance

- Every threshold lives in `config.py` (or as a named module constant) with a
  comment explaining WHY it exists and HOW it was calibrated.
- Provenance from prior research points to `PRIOR_WORK.md`.
- Never hard-code the reservoir area; it comes from `aoi.py` (computed from
  the polygon).

## Pipeline invariants

- Radiometry only in `sources/radiometry.py`; the BOA offset is computed once
  per scene from B02 and applied to every band.
- Re-implement the documented methodology (see PRIOR_WORK.md); correctness
  is validated by an identity gate against the author's reference series.
  When the brief and your instincts disagree, the brief wins — flag the
  discrepancy instead of "fixing" it.
- Core principle for the verification layer and the agent: models never
  compute a number. Every quantity comes from a deterministic, tested
  function; the model selects tools, explains results, and abstains when
  data or quality are insufficient.

## Dependencies

- Pin dependencies exactly (`==` in `pyproject.toml`, resolved in `uv.lock`).
- Keep Python >= 3.11 compatibility: if the newest release of a dependency
  requires >= 3.12, pin the newest release that still supports 3.11.

## Tests

- No network in CI: mock HTTP (or the client boundary); live tests are
  opt-in, gated by `SMOLF_LIVE=1`.
- For rare code paths, test the pure function directly or monkeypatch —
  don't build fragile synthetic scenes for paths that are nearly
  impossible to trigger naturally.

## Before finishing

- Run `ruff check .`, `ruff format --check .`, and `pytest -q`.
- Commits: create a commit only when the user asks; conventional commit
  messages; no trailers. All code in this repository is written with Muse
  Code during the hackathon, as disclosed in README.md and PRIOR_WORK.md.
