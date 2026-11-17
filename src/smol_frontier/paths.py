"""Default write locations and the level record's paths.

Every function is evaluated at call time (never captured at import), so a
caller or test can redirect it. Call sites use ``paths.series_dir()`` through
the module attribute so monkeypatching the function takes effect everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from smol_frontier.config import PROJECT_ROOT

__all__ = [
    "COMMITTED_LEVELS_PATH",
    "DATA_DIR_ENV",
    "cache_dir",
    "classify_dir",
    "gate_dir",
    "levels_path",
    "levels_read_path",
    "publish_dir",
    "series_dir",
]

# WHY: the SIH level record committed with the repo; the checkout's copy is
#   the baseline the pipeline reads levels from, offline and reproducibly.
# HOW: repo layout anchored at PROJECT_ROOT, stored byte-for-byte with a
#   .json provenance sidecar.
COMMITTED_LEVELS_PATH = PROJECT_ROOT / "data/hydro/ENDHD.csv"

# WHY: a scheduled daily job runs from a read-only code checkout, so the
#   scene cache and every output must be redirectable to a writable folder
#   without touching code or `Settings` (which feeds the run's config hash).
# HOW: read from the environment at call time by `_data_root()`; unset or
#   empty keeps the repo layout. The root holds `cache/`, `outputs/` and
#   `hydro/ENDHD.csv[.json]` (the refreshed SIH level record).
DATA_DIR_ENV = "SMOLF_DATA_DIR"


def _data_root() -> Path | None:
    """The writable data root from `SMOLF_DATA_DIR`, or None (unset or empty)."""
    value = os.environ.get(DATA_DIR_ENV, "")
    return Path(value) if value else None


def cache_dir() -> Path:
    """Shared on-disk cache of ingested scenes (gitignored)."""
    root = _data_root()
    if root is not None:
        return root / "cache"
    return PROJECT_ROOT / "data/cache"


def series_dir() -> Path:
    """Root of the series runs, one directory per run id."""
    root = _data_root()
    if root is not None:
        return root / "outputs/series"
    return PROJECT_ROOT / "outputs/series"


def publish_dir() -> Path:
    """Default location of the published data contract."""
    root = _data_root()
    if root is not None:
        return root / "outputs/publish"
    return PROJECT_ROOT / "outputs/publish"


def gate_dir() -> Path:
    """Default location of the identity gate reports."""
    root = _data_root()
    if root is not None:
        return root / "outputs/identity_gate"
    return PROJECT_ROOT / "outputs/identity_gate"


def classify_dir() -> Path:
    """Default location of the classified GeoTIFFs."""
    root = _data_root()
    if root is not None:
        return root / "outputs"
    return PROJECT_ROOT / "outputs"


def levels_path() -> Path:
    """Path `levels update` writes the SIH level record (and its sidecar) to."""
    root = _data_root()
    if root is not None:
        return root / "hydro/ENDHD.csv"
    return COMMITTED_LEVELS_PATH


def levels_read_path() -> Path:
    """Path every reader takes the SIH level record from.

    The write target when that file exists, else the committed copy, so an
    empty data folder still reads the record shipped with the checkout.
    """
    target = levels_path()
    if target.exists():
        return target
    return COMMITTED_LEVELS_PATH
