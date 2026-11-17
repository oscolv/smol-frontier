"""Default write locations and the level record's path.

Every function is evaluated at call time (never captured at import), so a
caller or test can redirect it. Call sites use ``paths.series_dir()`` through
the module attribute so monkeypatching the function takes effect everywhere.
"""

from __future__ import annotations

from pathlib import Path

from smol_frontier.config import PROJECT_ROOT

__all__ = [
    "COMMITTED_LEVELS_PATH",
    "cache_dir",
    "classify_dir",
    "gate_dir",
    "levels_path",
    "publish_dir",
    "series_dir",
]

# WHY: the SIH level record committed with the repo; the checkout's copy is
#   the baseline the pipeline reads levels from, offline and reproducibly.
# HOW: repo layout anchored at PROJECT_ROOT, stored byte-for-byte with a
#   .json provenance sidecar.
COMMITTED_LEVELS_PATH = PROJECT_ROOT / "data/hydro/ENDHD.csv"


def cache_dir() -> Path:
    """Shared on-disk cache of ingested scenes (gitignored)."""
    return PROJECT_ROOT / "data/cache"


def series_dir() -> Path:
    """Root of the series runs, one directory per run id."""
    return PROJECT_ROOT / "outputs/series"


def publish_dir() -> Path:
    """Default location of the published data contract."""
    return PROJECT_ROOT / "outputs/publish"


def gate_dir() -> Path:
    """Default location of the identity gate reports."""
    return PROJECT_ROOT / "outputs/identity_gate"


def classify_dir() -> Path:
    """Default location of the classified GeoTIFFs."""
    return PROJECT_ROOT / "outputs"


def levels_path() -> Path:
    """Path of the SIH level record the pipeline reads and `levels update` writes."""
    return COMMITTED_LEVELS_PATH
