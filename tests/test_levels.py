"""Tests for SIH water levels (HTTP mocked; no network in CI)."""

import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
import responses

from smol_frontier.hydro.levels import (
    SIH_URL,
    LevelsDownloadError,
    LevelsParseError,
    LevelsValidationError,
    check_regression,
    download_levels,
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
