"""The paths resolver returns today's repo-layout locations."""

from pathlib import Path

import pytest

from smol_frontier import paths, series
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


def _under(root: Path) -> dict[str, Path]:
    return {
        "cache_dir": root / "cache",
        "series_dir": root / "outputs/series",
        "publish_dir": root / "outputs/publish",
        "gate_dir": root / "outputs/identity_gate",
        "classify_dir": root / "outputs",
    }


def test_data_dir_unset_and_empty_keep_repo_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "cache_dir": PROJECT_ROOT / "data/cache",
        "series_dir": PROJECT_ROOT / "outputs/series",
        "publish_dir": PROJECT_ROOT / "outputs/publish",
        "gate_dir": PROJECT_ROOT / "outputs/identity_gate",
        "classify_dir": PROJECT_ROOT / "outputs",
    }
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert {name: getattr(paths, name)() for name in expected} == expected
    monkeypatch.setenv(paths.DATA_DIR_ENV, "")
    assert {name: getattr(paths, name)() for name in expected} == expected


def test_data_dir_set_moves_cache_and_every_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    expected = _under(tmp_path)
    assert {name: getattr(paths, name)() for name in expected} == expected


def test_data_dir_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "a"))
    assert paths.series_dir() == tmp_path / "a/outputs/series"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "b"))
    assert paths.series_dir() == tmp_path / "b/outputs/series"


def test_level_record_without_data_dir_is_the_committed_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert paths.levels_path() == paths.COMMITTED_LEVELS_PATH
    assert paths.levels_read_path() == paths.COMMITTED_LEVELS_PATH


def test_data_dir_moves_the_level_record_write_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert paths.levels_path() == tmp_path / "hydro/ENDHD.csv"


def test_level_read_path_falls_back_until_the_data_copy_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert paths.levels_read_path() == paths.COMMITTED_LEVELS_PATH
    copy = tmp_path / "hydro/ENDHD.csv"
    copy.parent.mkdir()
    copy.write_bytes(b"x")
    assert paths.levels_read_path() == copy


def test_data_dir_never_changes_the_config_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    without = series.config_hash()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert series.config_hash() == without


@pytest.mark.parametrize(
    "value",
    ["relative/data", "data", str(PROJECT_ROOT), str(PROJECT_ROOT / "data/daily")],
)
def test_data_dir_relative_or_inside_checkout_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, value)
    with pytest.raises(paths.DataDirError, match=paths.DATA_DIR_ENV):
        paths.cache_dir()
    with pytest.raises(paths.DataDirError):
        paths.check_data_dir()


def test_data_dir_absolute_outside_checkout_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    paths.check_data_dir()
    assert paths.cache_dir() == tmp_path / "cache"


def test_cli_refuses_relative_data_dir_with_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from smol_frontier.cli import app

    monkeypatch.setenv(paths.DATA_DIR_ENV, "relative/data")
    result = CliRunner().invoke(app, ["series", "build"])
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"error: {paths.DATA_DIR_ENV}=")
