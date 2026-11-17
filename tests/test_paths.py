"""The paths resolver returns today's repo-layout locations."""

from smol_frontier import paths
from smol_frontier.config import PROJECT_ROOT


def test_resolvers_match_repo_layout() -> None:
    assert paths.cache_dir() == PROJECT_ROOT / "data/cache"
    assert paths.series_dir() == PROJECT_ROOT / "outputs/series"
    assert paths.publish_dir() == PROJECT_ROOT / "outputs/publish"
    assert paths.gate_dir() == PROJECT_ROOT / "outputs/identity_gate"
    assert paths.classify_dir() == PROJECT_ROOT / "outputs"
    assert paths.levels_path() == PROJECT_ROOT / "data/hydro/ENDHD.csv"


def test_committed_levels_path() -> None:
    assert paths.COMMITTED_LEVELS_PATH == PROJECT_ROOT / "data/hydro/ENDHD.csv"
