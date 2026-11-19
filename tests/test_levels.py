"""Tests for SIH water levels (HTTP mocked; no network in CI)."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import responses
from typer.testing import CliRunner

from smol_frontier import paths, publish, series
from smol_frontier.cli import app
from smol_frontier.hydro.levels import (
    SIH_URL,
    LevelsDownloadError,
    LevelsParseError,
    LevelsValidationError,
    StaleLevelsError,
    check_days_covered,
    check_regression,
    download_levels,
    last_day_with_level,
    level_on,
    load_levels,
    parse_levels_bytes,
    update_levels,
)

PREAMBLE = ["Sistema Nacional de Informacion del Agua", "Presa ENDHD"]
HEADER = "Estacion,Fecha,Nivel(m),VolumenAlm(Mm3)"
HTML = "<!DOCTYPE html>\n<html><body>challenge</body></html>"
HTML_LATE = "loading...\n<html><body>challenge</body></html>"


def _row(
    day: str, level: str = "100.00", volume: str = "50.000", station: str = "ENDHD"
) -> tuple[str, str, str, str]:
    return (station, day, level, volume)


def _csv(rows: list[tuple[str, str, str, str]]) -> bytes:
    lines = [*PREAMBLE, HEADER, *(",".join(row) for row in rows)]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def test_parse_ok_keeps_file_order_and_nan() -> None:
    data = _csv(
        [
            _row("2024-01-01", "100.00", "50.000"),
            _row("2024-01-02", "", "not-a-number"),
            _row("2024-01-03", "100.05", ""),
        ]
    )
    rows = parse_levels_bytes(data)
    assert [row.date.isoformat() for row in rows] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]
    assert rows[0].level_m == 100.00
    assert rows[0].volume_hm3 == 50.000
    assert rows[1].level_m != rows[1].level_m  # NaN
    assert rows[1].volume_hm3 != rows[1].volume_hm3  # NaN
    assert rows[2].volume_hm3 != rows[2].volume_hm3  # NaN


@pytest.mark.parametrize("body", [HTML, HTML_LATE])
def test_parse_html_challenge_says_so(body: str) -> None:
    with pytest.raises(LevelsParseError, match="challenge"):
        parse_levels_bytes(body.encode("utf-8"))


def test_parse_missing_header_not_html() -> None:
    with pytest.raises(LevelsParseError, match="header line"):
        parse_levels_bytes(b"nothing useful here\n" * 20)


def test_parse_wrong_station() -> None:
    data = _csv([_row("2024-01-01"), _row("2024-01-02", station="OTHER")])
    with pytest.raises(LevelsParseError, match="OTHER"):
        parse_levels_bytes(data)


def test_parse_missing_column() -> None:
    lines = [*PREAMBLE, "Estacion,Fecha,Nivel(m)", "ENDHD,2024-01-01,100.00"]
    with pytest.raises(LevelsParseError, match="VolumenAlm"):
        parse_levels_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def _callback_sequence(bodies: list[bytes]):
    calls: list = []

    def _callback(request):
        calls.append(request)
        body = bodies[0] if len(calls) == 1 else bodies[-1]
        return (200, {}, body)

    return _callback


@responses.activate
def test_download_challenge_then_csv_succeeds() -> None:
    csv_data = _csv([_row("2024-01-01")])
    responses.add_callback(
        responses.GET, SIH_URL, callback=_callback_sequence([HTML.encode(), csv_data])
    )
    assert download_levels() == csv_data
    assert len(responses.calls) == 2
    assert "Chrome" in responses.calls[0].request.headers["User-Agent"]


@responses.activate
def test_download_status_error_then_csv_succeeds() -> None:
    csv_data = _csv([_row("2024-01-01")])
    responses.add(responses.GET, SIH_URL, status=500)
    responses.add(responses.GET, SIH_URL, body=csv_data, status=200)
    assert download_levels() == csv_data
    assert len(responses.calls) == 2


@responses.activate
def test_download_always_html_fails_after_4_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    responses.add(responses.GET, SIH_URL, body=HTML, status=200)
    with pytest.raises(LevelsDownloadError, match="4 attempts"):
        download_levels()
    assert len(responses.calls) == 4
    assert waits == [5.0, 10.0, 15.0]


def test_reject_fewer_days() -> None:
    old = parse_levels_bytes(_csv([_row("2024-01-01"), _row("2024-01-02")]))
    new = parse_levels_bytes(_csv([_row("2024-01-01")]))
    with pytest.raises(LevelsValidationError, match="fewer days"):
        check_regression(new, old, force=False)


def test_reject_earlier_end() -> None:
    old = parse_levels_bytes(_csv([_row(f"2024-01-0{d}") for d in range(1, 6)]))
    new = parse_levels_bytes(
        _csv(
            [
                _row(day)
                for day in (
                    "2023-12-31",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                )
            ]
        )
    )
    with pytest.raises(LevelsValidationError, match="ends earlier"):
        check_regression(new, old, force=False)


def test_reject_missing_day() -> None:
    old = parse_levels_bytes(_csv([_row(f"2024-01-0{d}") for d in range(1, 6)]))
    new = parse_levels_bytes(_csv([_row(f"2024-01-0{d}") for d in (1, 2, 3, 4, 6)]))
    with pytest.raises(LevelsValidationError, match="misses 1 local day"):
        check_regression(new, old, force=False)


def test_reject_level_revision_over_tolerance() -> None:
    old = parse_levels_bytes(_csv([_row("2024-01-01", "100.00")]))
    new = parse_levels_bytes(_csv([_row("2024-01-01", "100.06")]))
    with pytest.raises(LevelsValidationError, match="revises Nivel"):
        check_regression(new, old, force=False)


def test_accept_level_revision_with_force() -> None:
    old = parse_levels_bytes(_csv([_row("2024-01-01", "100.00")]))
    new = parse_levels_bytes(_csv([_row("2024-01-01", "100.06")]))
    counts = check_regression(new, old, force=True)
    assert counts.level_revisions == 1


def test_accept_small_level_change_and_backfill() -> None:
    old = parse_levels_bytes(
        _csv([_row("2024-01-01", "100.00"), _row("2024-01-02", "")])
    )
    new = parse_levels_bytes(
        _csv([_row("2024-01-01", "100.04"), _row("2024-01-02", "100.10")])
    )
    counts = check_regression(new, old, force=False)
    assert counts.level_revisions == 0


def test_volume_revisions_accepted_and_counted() -> None:
    old = parse_levels_bytes(_csv([_row("2024-01-01", "100.00", "50.000")]))
    new = parse_levels_bytes(_csv([_row("2024-01-01", "100.00", "51.000")]))
    counts = check_regression(new, old, force=False)
    assert counts.volume_revisions == 1
    assert counts.level_revisions == 0


@responses.activate
def test_update_writes_csv_byte_for_byte_and_sidecar(tmp_path: Path) -> None:
    csv_data = _csv([_row("2024-01-01"), _row("2024-01-02"), _row("2024-01-03", "")])
    responses.add(responses.GET, SIH_URL, body=csv_data, status=200)
    target = tmp_path / "ENDHD.csv"
    report = update_levels(path=target)
    assert report.written is True
    assert report.n_days == 3
    assert target.read_bytes() == csv_data
    assert b"\r\n" in target.read_bytes()
    meta = json.loads((tmp_path / "ENDHD.csv.json").read_text())
    assert meta["url"] == SIH_URL
    assert meta["bytes"] == len(csv_data)
    assert meta["sha256"] == hashlib.sha256(csv_data).hexdigest()
    assert meta["n_days"] == 3
    assert meta["last_day_with_level"] == "2024-01-02"
    assert "downloaded_utc" in meta


@responses.activate
def test_update_check_does_not_write(tmp_path: Path) -> None:
    responses.add(responses.GET, SIH_URL, body=_csv([_row("2024-01-01")]), status=200)
    target = tmp_path / "ENDHD.csv"
    report = update_levels(check=True, path=target)
    assert report.written is False
    assert report.n_days == 1
    assert not target.exists()
    assert not (tmp_path / "ENDHD.csv.json").exists()


@responses.activate
def test_update_removes_owned_stale_temp_files_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    responses.add(responses.GET, SIH_URL, body=_csv([_row("2024-01-01")]), status=200)
    owned = [tmp_path / "ENDHD.csv.abc123.tmp", tmp_path / "ENDHD.csv.json.xyz.tmp"]
    unrelated = [tmp_path / "other.csv.abc.tmp", tmp_path / "notes.txt"]
    for f in owned + unrelated:
        f.write_text("x")
    with caplog.at_level(logging.INFO, logger="smol_frontier.hydro.levels"):
        update_levels(path=tmp_path / "ENDHD.csv")
    assert not any(f.exists() for f in owned)
    assert all(f.exists() for f in unrelated)
    removals = [
        r.getMessage() for r in caplog.records if "stale temp" in r.getMessage()
    ]
    assert len(removals) == len(owned)
    for f in owned:
        assert any(f.name in message for message in removals)


@responses.activate
def test_update_check_keeps_stale_temp_files(tmp_path: Path) -> None:
    responses.add(responses.GET, SIH_URL, body=_csv([_row("2024-01-01")]), status=200)
    stale = tmp_path / "ENDHD.csv.abc123.tmp"
    stale.write_text("x")
    update_levels(check=True, path=tmp_path / "ENDHD.csv")
    assert stale.exists()
    assert not (tmp_path / "ENDHD.csv").exists()


@responses.activate
def test_update_rejection_keeps_stale_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "ENDHD.csv"
    target.write_bytes(_csv([_row("2024-01-01"), _row("2024-01-02")]))
    responses.add(responses.GET, SIH_URL, body=_csv([_row("2024-01-01")]), status=200)
    stale = tmp_path / "ENDHD.csv.abc123.tmp"
    stale.write_text("x")
    with pytest.raises(LevelsValidationError):
        update_levels(path=target)
    assert stale.exists()


@responses.activate
def test_update_rejection_keeps_local_copy(tmp_path: Path) -> None:
    local = _csv([_row("2024-01-01"), _row("2024-01-02")])
    target = tmp_path / "ENDHD.csv"
    target.write_bytes(local)
    responses.add(responses.GET, SIH_URL, body=_csv([_row("2024-01-01")]), status=200)
    with pytest.raises(LevelsValidationError, match="fewer days"):
        update_levels(path=target)
    assert target.read_bytes() == local
    assert not (tmp_path / "ENDHD.csv.json").exists()


def test_level_on_exact_day_only(tmp_path: Path) -> None:
    target = tmp_path / "ENDHD.csv"
    target.write_bytes(_csv([_row("2024-01-01", "100.25"), _row("2024-01-03", "")]))
    assert level_on(date(2024, 1, 1), target) == 100.25
    assert level_on(date(2024, 1, 2), target) is None  # missing day, no_interp
    assert level_on(date(2024, 1, 3), target) is None  # present day, NaN level
    assert [row.date for row in load_levels(target)] == [
        date(2024, 1, 1),
        date(2024, 1, 3),
    ]


@pytest.mark.skipif(
    os.environ.get("SMOLF_LIVE") != "1",
    reason="opt-in live SIH test (SMOLF_LIVE=1)",
)
def test_live_download_endhd() -> None:
    data = download_levels()
    rows = parse_levels_bytes(data)
    print(f"\nSMOLF_LIVE: {len(rows)} day(s), {len(data)} bytes")
    print(f"  first: {rows[0].date} last: {rows[-1].date}")
    assert len(rows) > 20000
    assert all(row.date is not None for row in rows)


# --- SMOLF_DATA_DIR: write to the data folder, read it once it exists --------


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Empty data folder plus a tmp stand-in for the committed record."""
    committed = tmp_path / "repo/data/hydro/ENDHD.csv"
    committed.parent.mkdir(parents=True)
    committed.write_bytes(
        _csv([_row("2024-01-01", "100.00"), _row("2024-01-02", "101.00")])
    )
    monkeypatch.setattr(paths, "COMMITTED_LEVELS_PATH", committed)
    root = tmp_path / "data_root"
    root.mkdir()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(root))
    return SimpleNamespace(
        root=root, committed=committed, copy=root / "hydro/ENDHD.csv"
    )


def _readers() -> dict[str, object]:
    return {
        "load_levels": [row.date for row in load_levels()],
        "level_on": level_on(date(2024, 1, 2)),
        "last_day_with_level": last_day_with_level(),
        "current_level": publish.current_level(),
        "ENDHD_csv": series.input_hashes()["ENDHD_csv"],
    }


@responses.activate
def test_readers_use_committed_record_until_the_data_copy_exists(
    data_dir: SimpleNamespace,
) -> None:
    sha = hashlib.sha256
    before = _readers()
    assert before["last_day_with_level"] == date(2024, 1, 2)
    assert before["level_on"] == 101.00
    assert before["ENDHD_csv"] == sha(data_dir.committed.read_bytes()).hexdigest()
    show = CliRunner().invoke(app, ["levels", "show", "2024-01-02"])
    assert show.exit_code == 0, show.output
    assert "level_m: 101.00" in show.output

    refreshed = _csv(
        [
            _row("2024-01-01", "100.00"),
            _row("2024-01-02", "101.00"),
            _row("2024-01-03", "102.50"),
        ]
    )
    responses.add(responses.GET, SIH_URL, body=refreshed, status=200)
    committed_bytes = data_dir.committed.read_bytes()
    update = CliRunner().invoke(app, ["levels", "update"])
    assert update.exit_code == 0, update.output
    assert f"output: {data_dir.copy}" in update.output
    assert data_dir.copy.read_bytes() == refreshed
    assert (data_dir.copy.parent / "ENDHD.csv.json").is_file()
    assert data_dir.committed.read_bytes() == committed_bytes

    after = _readers()
    assert after["last_day_with_level"] == date(2024, 1, 3)
    assert after["current_level"]["date"] == "2024-01-03"
    assert after["current_level"]["levels_sha256"] == sha(refreshed).hexdigest()
    assert after["ENDHD_csv"] == sha(refreshed).hexdigest()
    show = CliRunner().invoke(app, ["levels", "show", "2024-01-03"])
    assert show.exit_code == 0, show.output
    assert "level_m: 102.50" in show.output


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([_row("2024-01-01", "100.00")], "fewer days (1) than local (2)"),
        (
            [_row("2023-12-31", "99.00"), _row("2024-01-01", "100.00")],
            "ends earlier",
        ),
        (
            [_row("2024-01-01", "100.00"), _row("2024-01-02", "101.50")],
            "revises Nivel(m)",
        ),
    ],
    ids=["fewer-days", "earlier-end", "level-revision"],
)
@responses.activate
def test_first_refresh_into_empty_data_dir_is_validated_against_committed(
    data_dir: SimpleNamespace, rows: list[tuple[str, str, str, str]], reason: str
) -> None:
    responses.add(responses.GET, SIH_URL, body=_csv(rows), status=200)
    committed_bytes = data_dir.committed.read_bytes()
    result = CliRunner().invoke(app, ["levels", "update"])
    assert result.exit_code == 1
    assert reason in result.output
    assert data_dir.committed.read_bytes() == committed_bytes
    assert not data_dir.copy.exists()
    assert not (data_dir.copy.parent / "ENDHD.csv.json").exists()
    assert paths.levels_read_path() == data_dir.committed


@responses.activate
def test_explicit_path_skips_the_committed_baseline(
    data_dir: SimpleNamespace, tmp_path: Path
) -> None:
    responses.add(
        responses.GET, SIH_URL, body=_csv([_row("2024-01-01", "100.00")]), status=200
    )
    target = tmp_path / "elsewhere/ENDHD.csv"
    report = update_levels(path=target)
    assert report.written is True
    assert not data_dir.copy.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_covers_levels_temp_files() -> None:
    from smol_frontier.config import PROJECT_ROOT

    for name in ("data/hydro/ENDHD.csv.x.tmp", "data/hydro/tmpabc.tif.tmp"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=PROJECT_ROOT, check=False
        )
        assert result.returncode == 0, name


def test_agents_md_quotes_the_committed_level_record_sha256() -> None:
    """AGENTS.md pins the committed ENDHD.csv by a sha256 prefix; keep it true."""
    import re

    agents = (paths.COMMITTED_LEVELS_PATH.parents[2] / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    match = re.search(r"`data/hydro/ENDHD\.csv` sha256 `([0-9a-f]{8})…`", agents)
    assert match, "AGENTS.md no longer quotes the ENDHD.csv sha256"
    actual = hashlib.sha256(paths.COMMITTED_LEVELS_PATH.read_bytes()).hexdigest()
    assert actual.startswith(match.group(1))


def test_agents_md_quotes_the_committed_level_record_last_day() -> None:
    """AGENTS.md says through which day the committed ENDHD.csv runs; keep it true."""
    import re

    agents = (paths.COMMITTED_LEVELS_PATH.parents[2] / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    match = re.search(r"committed refresh through\s+(\d{4}-\d{2}-\d{2});", agents)
    assert match, "AGENTS.md no longer quotes the ENDHD.csv last day"
    quoted = date.fromisoformat(match.group(1))
    assert last_day_with_level(paths.COMMITTED_LEVELS_PATH) == quoted


# --- Rule 12 (levels before scenes) for `smolf classify` --------------------


def test_check_days_covered_accepts_days_up_to_the_last_level_day() -> None:
    check_days_covered([date(2024, 1, 1), date(2024, 1, 2)], date(2024, 1, 2))


def test_check_days_covered_names_last_day_uncovered_days_and_the_fix() -> None:
    days = [date(2024, 1, 4), date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 4)]
    with pytest.raises(StaleLevelsError) as info:
        check_days_covered(days, date(2024, 1, 2))
    message = str(info.value)
    assert "ends 2024-01-02; no level for 2024-01-03, 2024-01-04." in message
    assert "2024-01-01" not in message
    assert "smolf levels update" in message


def test_check_days_covered_without_a_record_refuses_with_none() -> None:
    with pytest.raises(StaleLevelsError, match="ends none; no level for 2024-01-01"):
        check_days_covered([date(2024, 1, 1)], None)


def test_series_and_levels_share_one_stale_levels_error() -> None:
    assert series.StaleLevelsError is StaleLevelsError


def _search_must_not_run(*args, **kwargs):
    raise AssertionError("classify searched the catalog before checking levels")


def _ingest_must_not_run(*args, **kwargs):
    raise AssertionError("classify ingested a scene before checking levels")


def test_classify_refuses_a_day_after_the_level_record(
    data_dir: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smol_frontier.cli.search_scenes", _search_must_not_run)
    monkeypatch.setattr("smol_frontier.cli.ingest_with_fallback", _ingest_must_not_run)
    result = CliRunner().invoke(app, ["classify", "2024-01-03"])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert "ends 2024-01-02; no level for 2024-01-03" in lines[0]
    assert "smolf levels update" in lines[0]
    assert not (data_dir.root / "outputs").exists()


def test_classify_refuses_when_no_level_record_is_readable(
    data_dir: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.committed.unlink()
    monkeypatch.setattr("smol_frontier.cli.search_scenes", _search_must_not_run)
    monkeypatch.setattr("smol_frontier.cli.ingest_with_fallback", _ingest_must_not_run)
    result = CliRunner().invoke(app, ["classify", "2024-01-01"])
    assert result.exit_code == 2, result.output
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert "ends none; no level for 2024-01-01" in lines[0]
    assert "smolf levels update" in lines[0]
    assert "Traceback" not in result.output


def test_series_build_refuses_when_no_level_record_is_readable(
    data_dir: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refusal as classify: exit 2, `ends none`, no run dir."""
    data_dir.committed.unlink()
    targets = [series.SeriesTarget(day=date(2024, 1, 1), item_id="S2A_x")]
    monkeypatch.setattr(series, "list_reference_targets", lambda: targets)
    result = CliRunner().invoke(app, ["series", "build"])
    assert result.exit_code == 2, result.output
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert "ends none; no level for 2024-01-01" in lines[0]
    assert "smolf levels update" in lines[0]
    assert not (data_dir.root / "outputs").exists()


def test_last_day_with_level_without_a_record_is_none(tmp_path: Path) -> None:
    assert last_day_with_level(tmp_path / "missing.csv") is None


@pytest.mark.parametrize("day", ["2024-01-01", "2024-01-02", "2024-01-03"])
def test_classify_on_a_covered_day_reaches_the_catalog_search(
    data_dir: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, day: str
) -> None:
    """Covered days pass the gate, including a NaN gap inside the record."""
    data_dir.committed.write_bytes(
        _csv(
            [
                _row("2024-01-01", "100.00"),
                _row("2024-01-02", "101.00"),
                _row("2024-01-03", ""),  # real SIH gap: NaN level
                _row("2024-01-04", "101.50"),
            ]
        )
    )
    searched: list[object] = []
    monkeypatch.setattr(
        "smol_frontier.cli.search_scenes", lambda *a, **k: searched.append(a) or []
    )
    result = CliRunner().invoke(app, ["classify", day])
    assert result.exit_code == 1, result.output
    assert searched
    assert f"no scenes found for {day}" in result.stderr
