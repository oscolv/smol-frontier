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
- Reproduce the prior methodology exactly: an identity gate compares this
  pipeline against the reference series. When the brief and your instincts
  disagree, the brief wins — flag the discrepancy instead of "fixing" it.

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
- Create the commit only when asked; conventional commit messages;
  no co-author or tool-attribution trailers.
