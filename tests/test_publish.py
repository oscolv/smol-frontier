"""Tests for `smolf publish` (offline, fixture run directories).

The seam is the CLI command run on a small fixture run under a tmp series
directory; assertions cover what it writes (file set, schema validity,
values, bytes) and how it exits. Schema negatives validate known-bad
documents directly against the packaged schema.
"""

import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from smol_frontier import __version__, aoi, publish, series
from smol_frontier.cli import app
from smol_frontier.hydro import levels

RUN_ID = "20240110T120000"


def _row(day: str, item_id: str, **overrides: object) -> dict:
    """Unrounded series row as `series build` stores it in a scene record."""
    row = {
        "date": day,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "backend_used": "earth-search",
        "fallback": False,
        "tile_cloud_pct": 12.345678,
        "light": "GOOD",
        "regime": "clean",
        "valid_frac": 0.987654321,
        "cloud_frac": 0.012345678,
        "dense_ha": 400.123456,
        "interface_ha": 230.987654,
        "hyacinth_raw_ha": 631.11111,
        "water_ha": 600.5,
        "soil_ha": 3.25,
        "pct_reservoir_raw": 50.019871234,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2010.123456,
        "margin_m": 2010.55,
        "expected_bed_ha": 120.0,
        "removed_ha": 0.555555,
        "dense_corrected_ha": 399.876543,
        "interface_corrected_ha": 230.678912,
        "hyacinth_corrected_ha": 630.555455,
        "pct_reservoir_corrected": 49.996,
    }
    row.update(overrides)
    return row


def _record(day: str, item_id: str, status: str, **row_overrides: object) -> dict:
    """Scene record (ok | skipped | error) as `series build` writes it."""
    record = {
        "backend_used": "earth-search",
        "cache_sha256": "ab" * 32,
        "date": day,
        "error": "",
        "fallback": False,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "note": "",
        "row": None,
        "status": status,
        "tile_cloud_pct": 12.345678,
        "valid_frac": None,
    }
    if status == "ok":
        row = _row(day, item_id, **row_overrides)
        record["row"] = row
        record["valid_frac"] = row["valid_frac"]
    elif status == "skipped":
        record["valid_frac"] = 0.4321987
    else:
        record["error"] = "boom"
    return record


def _default_records() -> list[dict]:
    """GOOD, DOUBTFUL, error, latest-with-figures GOOD, newest POOR."""
    return [
        _record("2024-01-01", "S2A_14QMH_20240101_0_L2A", "ok"),
        _record(
            "2024-01-02",
            "S2B_14QMH_20240102_0_L2A",
            "ok",
            light="DOUBTFUL",
            valid_frac=0.9,
            pct_reservoir_corrected=12.0,
        ),
        _record("2024-01-03", "S2A_14QMH_20240103_0_L2A", "error"),
        _record("2024-01-04", "S2B_14QMH_20240104_0_L2A", "ok", regime="EXPOSED_BED"),
        _record("2024-01-05", "S2A_14QMH_20240105_0_L2A", "skipped"),
    ]


def _write_run(
    root: Path,
    run_id: str = RUN_ID,
    records: list[dict] | None = None,
    *,
    status: str = "complete",
    dirty: bool = False,
) -> Path:
    """Write a fixture run directory (manifest + scene JSONs)."""
    records = _default_records() if records is None else records
    run_dir = root / run_id
    scenes_dir = run_dir / "scenes"
    scenes_dir.mkdir(parents=True)
    for record in records:
        path = scenes_dir / f"{record['item_id_requested']}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    manifest = {
        "run_id": run_id,
        "status": status,
        "code": {"git_commit": "0" * 40, "dirty": dirty, "config_hash": "1" * 64},
        "counts": {
            "requested": len(records),
            "ok": sum(r["status"] == "ok" for r in records),
            "skipped": sum(r["status"] == "skipped" for r in records),
            "errors": sum(r["status"] == "error" for r in records),
        },
        "scenes": [{k: v for k, v in r.items() if k != "row"} for r in records],
        "series_csv": "series.csv",
        "series_sha256": "cd" * 32,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return run_dir


_LEVELS_HEADER = (
    "Header line\r\n\r\n"
    "Estacion,Fecha,Nivel(m),Precipitacion(mm),VolumenAlm(Mm3),"
    "ObraToma(m3/s),Vertedor(m3/s),Derrame(m3/s),Evaporacion(mm)\r\n"
)


def _write_levels(path: Path, rows: list[tuple[str, str]]) -> Path:
    """Write a minimal SIH level CSV with (date, level) rows."""
    body = "".join(f"ENDHD,{day},{level},,,,,,\r\n" for day, level in rows)
    path.write_bytes((_LEVELS_HEADER + body).encode("utf-8"))
    return path


@pytest.fixture(autouse=True)
def levels_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Tmp level record (last level 2024-01-09), so no test reads data/hydro."""
    path = _write_levels(
        tmp_path_factory.mktemp("hydro") / "ENDHD.csv",
        [("2024-01-08", "2009.5"), ("2024-01-09", "2010.126"), ("2024-01-10", "")],
    )
    monkeypatch.setattr(levels, "DEFAULT_LEVELS_PATH", path)
    return path


@pytest.fixture
def series_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "series"
    root.mkdir()
    monkeypatch.setattr(series, "OUTPUT_DIR", root)
    return root


def _publish(out: Path, *args: str):
    return CliRunner().invoke(app, ["publish", *args, "--out", str(out)])


def _latest(out: Path) -> dict:
    return json.loads((out / "latest.json").read_text(encoding="utf-8"))


def test_publish_latest_golden_values(
    series_root: Path, tmp_path: Path, levels_file: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["latest.json", "series.json"]
    assert _latest(out) == {
        "date": "2024-01-04",
        "item_id": "S2B_14QMH_20240104_0_L2A",
        "quality_light": "GOOD",
        "regime": "EXPOSED_BED",
        "valid_frac": 0.9877,
        "cloud_frac": 0.0123,
        "dense_ha": 400.12,
        "interface_ha": 230.99,
        "hyacinth_raw_ha": 631.11,
        "pct_reservoir_raw": 50.02,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2010.12,
        "removed_ha": 0.56,
        "dense_corrected_ha": 399.88,
        "interface_corrected_ha": 230.68,
        "hyacinth_corrected_ha": 630.56,
        # 49.996 publishes as 50.00, so the band is RED, not YELLOW.
        "pct_reservoir_corrected": 50.0,
        "cover_band": "RED",
        "schema_version": "1.0.0",
        "smolf_version": __version__,
        "newest_acquisition": {
            "date": "2024-01-05",
            "item_id": "S2A_14QMH_20240105_0_L2A",
            "quality_light": "POOR",
            "valid_frac": 0.4322,
        },
        "run_counts": {"ok": 3, "skipped": 1, "errors": 1},
        "run_id": RUN_ID,
        "series_sha256": "cd" * 32,
        "reservoir_area_ha": round(aoi.reservoir_area_ha(), 2),
        "constants": {
            "frac_publishable": 0.95,
            "green_max_pct": 30.0,
            "red_min_pct": 50.0,
        },
        "current_level": {
            "date": "2024-01-09",
            "level_m": 2010.13,
            "levels_sha256": series.sha256_file(levels_file),
        },
    }


@pytest.mark.parametrize(
    ("pct", "band"),
    [(29.994, "GREEN"), (29.995, "YELLOW"), (49.994, "YELLOW"), (50.0, "RED")],
)
def test_publish_latest_cover_band_on_rounded_pct(
    series_root: Path, tmp_path: Path, pct: float, band: str
) -> None:
    _write_run(
        series_root,
        records=[
            _record("2024-01-01", "S2A_X", "ok", pct_reservoir_corrected=pct),
        ],
    )
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert _latest(out)["cover_band"] == band


def test_publish_latest_regime_clean_maps_to_upper(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, records=[_record("2024-01-01", "S2A_X", "ok")])
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert _latest(out)["regime"] == "CLEAN"


def test_publish_latest_bed_not_applied_nulls(
    series_root: Path, tmp_path: Path
) -> None:
    reason = "no SIH level for 2024-01-01"
    record = _record(
        "2024-01-01",
        "S2A_X",
        "ok",
        bed_applied=False,
        bed_reason=reason,
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
        dense_corrected_ha=None,
        interface_corrected_ha=None,
        hyacinth_corrected_ha=None,
        pct_reservoir_corrected=None,
    )
    _write_run(series_root, records=[record])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    latest = _latest(out)
    assert latest["bed_applied"] is False
    assert latest["bed_reason"] == reason
    for key in (
        "dense_corrected_ha",
        "interface_corrected_ha",
        "hyacinth_corrected_ha",
        "pct_reservoir_corrected",
        "cover_band",
    ):
        assert latest[key] is None
    assert latest["hyacinth_raw_ha"] == 631.11


def test_publish_latest_default_run_is_newest_complete(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, "20240101T000000", [_record("2024-01-01", "A", "ok")])
    _write_run(series_root, "20240102T000000", [_record("2024-01-02", "B", "ok")])
    _write_run(
        series_root,
        "20240103T000000",
        [_record("2024-01-03", "C", "ok")],
        status="running",
    )
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    assert _latest(out)["item_id"] == "B"


def test_publish_latest_is_canonical_and_byte_identical(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    first = (out / "latest.json").read_bytes()
    doc = json.loads(first)
    assert first == (
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    assert _publish(out).exit_code == 0
    assert (out / "latest.json").read_bytes() == first
    assert sorted(p.name for p in out.parent.iterdir()) == ["pub", "series"]


@pytest.mark.parametrize(
    ("kwargs", "records", "reason"),
    [
        ({"dirty": True}, None, "dirty"),
        ({"status": "running"}, None, "not complete"),
        ({}, [_record("2024-01-01", "A", "skipped")], "no scene with figures"),
    ],
)
def test_publish_latest_refusals_exit_2_and_keep_previous(
    series_root: Path,
    tmp_path: Path,
    kwargs: dict,
    records: list[dict] | None,
    reason: str,
) -> None:
    _write_run(series_root, "20240101T000000", [_record("2024-01-01", "A", "ok")])
    out = tmp_path / "pub"
    assert _publish(out, "--run", "20240101T000000").exit_code == 0
    before = (out / "latest.json").read_bytes()
    _write_run(series_root, RUN_ID, records, **kwargs)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert reason in lines[0]
    assert (out / "latest.json").read_bytes() == before
    assert sorted(p.name for p in out.parent.iterdir()) == ["pub", "series"]


def test_publish_latest_refusal_creates_no_output(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, dirty=True)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    assert not out.exists()


def test_publish_latest_unknown_run_exit_2(series_root: Path, tmp_path: Path) -> None:
    result = _publish(tmp_path / "pub", "--run", "nope")
    assert result.exit_code == 2
    assert "nope" in result.output


def test_publish_latest_no_complete_run_exit_2(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, status="running")
    result = _publish(tmp_path / "pub")
    assert result.exit_code == 2
    assert "no complete run" in result.output


def test_publish_latest_invalid_output_exit_1_writes_nothing(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, records=[_record("2024-01-01", "A", "ok", light="BUENA")])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 1
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["series"]


def test_publish_latest_newest_acquisition_ok_scene_with_row_light(
    series_root: Path, tmp_path: Path
) -> None:
    records = [
        _record("2024-01-01", "A", "ok"),
        _record("2024-01-02", "B", "ok", light="DOUBTFUL", valid_frac=0.91236),
        _record("2024-01-03", "C", "error"),
    ]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert _latest(out)["newest_acquisition"] == {
        "date": "2024-01-02",
        "item_id": "B",
        "quality_light": "DOUBTFUL",
        "valid_frac": 0.9124,
    }


def test_publish_latest_newest_acquisition_never_error(
    series_root: Path, tmp_path: Path
) -> None:
    records = [_record("2024-01-01", "A", "ok"), _record("2024-01-09", "Z", "error")]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert _latest(out)["newest_acquisition"]["item_id"] == "A"


def test_publish_latest_newest_poor_while_scene_stays_newest_ok(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    doc = _latest(out)
    assert doc["newest_acquisition"]["quality_light"] == "POOR"
    assert doc["newest_acquisition"]["date"] == "2024-01-05"
    assert doc["date"] == "2024-01-04"


def test_publish_latest_run_counts_and_ids_equal_manifest(
    series_root: Path, tmp_path: Path
) -> None:
    run_dir = _write_run(series_root)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    doc = _latest(out)
    expected = {k: manifest["counts"][k] for k in ("ok", "skipped", "errors")}
    assert doc["run_counts"] == expected
    assert doc["run_id"] == manifest["run_id"]
    assert doc["series_sha256"] == manifest["series_sha256"]


def test_publish_latest_constants_and_area_from_settings(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    monkeypatch.setattr(aoi, "reservoir_area_ha", lambda: 1000.126)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    doc = _latest(out)
    assert doc["reservoir_area_ha"] == 1000.13
    assert doc["constants"] == {
        "frac_publishable": publish.settings.FRAC_PUBLISHABLE,
        "green_max_pct": publish.settings.GREEN_MAX_PCT,
        "red_min_pct": publish.settings.RED_MIN_PCT,
    }


def test_publish_latest_level_is_last_day_with_level_not_wall_clock(
    series_root: Path, tmp_path: Path, levels_file: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert _latest(out)["current_level"] == {
        "date": "2024-01-09",
        "level_m": 2010.13,
        "levels_sha256": series.sha256_file(levels_file),
    }


def test_publish_latest_level_update_changes_only_current_level(
    series_root: Path, tmp_path: Path, levels_file: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    before = _latest(out)
    _write_levels(levels_file, [("2024-01-09", "2010.126"), ("2024-01-10", "2011.0")])
    assert _publish(out).exit_code == 0
    after = _latest(out)
    assert after["current_level"]["date"] == "2024-01-10"
    assert {k: v for k, v in after.items() if k != "current_level"} == {
        k: v for k, v in before.items() if k != "current_level"
    }


def test_publish_latest_level_empty_record_is_null_exit_0(
    series_root: Path, tmp_path: Path, levels_file: Path
) -> None:
    _write_run(series_root)
    _write_levels(levels_file, [("2024-01-09", ""), ("2024-01-10", "")])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    assert _latest(out)["current_level"] is None


def test_publish_latest_level_missing_file_is_null(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    result = CliRunner().invoke(app, ["publish", "--out", str(out)])
    assert result.exit_code == 0
    assert publish.current_level(tmp_path / "absent.csv") is None


def test_publish_current_level_no_level_rows(tmp_path: Path) -> None:
    empty = _write_levels(tmp_path / "empty.csv", [])
    nan_only = _write_levels(tmp_path / "nan.csv", [("2024-01-01", "")])
    assert publish.current_level(empty) is None
    assert publish.current_level(nan_only) is None


# --- schema negatives (pure, no command) ---------------------------------


def _valid_latest(series_root: Path, tmp_path: Path) -> dict:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    return _latest(out)


def test_publish_latest_schema_accepts_published_doc(
    series_root: Path, tmp_path: Path
) -> None:
    publish.validate("latest", _valid_latest(series_root, tmp_path))


def test_publish_latest_schema_tolerates_extra_fields(
    series_root: Path, tmp_path: Path
) -> None:
    doc = _valid_latest(series_root, tmp_path)
    doc["something_new"] = 1
    publish.validate("latest", doc)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pct_reservoir_corrected", publish),  # sentinel: delete the key
        ("quality_light", "BUENA"),
        ("cover_band", "ORANGE"),
        ("pct_reservoir_corrected", "58.22"),
        ("pct_reservoir_corrected", 120),
        ("schema_version", "0.9.0"),
        ("regime", "clean"),
        ("valid_frac", 1.5),
        ("dense_ha", -1.0),
        ("date", "2024-01-04T10:00:00Z"),
        ("newest_acquisition", publish),
        ("run_counts", publish),
        ("run_id", publish),
        ("series_sha256", publish),
        ("reservoir_area_ha", publish),
        ("constants", publish),
        ("current_level", publish),
        ("series_sha256", "abc"),
        ("reservoir_area_ha", "1261.75"),
        ("reservoir_area_ha", 0),
        ("run_counts", {"ok": 1, "skipped": 0}),
        ("run_counts", {"ok": 1, "skipped": 0, "errors": -1}),
        ("newest_acquisition", {"date": "2024-01-05", "item_id": "A"}),
        (
            "newest_acquisition",
            {
                "date": "2024-01-05",
                "item_id": "A",
                "quality_light": "BUENA",
                "valid_frac": 0.5,
            },
        ),
        ("constants", {"frac_publishable": 0.95, "green_max_pct": 30.0}),
        ("current_level", {"date": "2024-01-09", "level_m": 2010.1}),
        (
            "current_level",
            {"date": "2024-01-09", "level_m": "x", "levels_sha256": "ab" * 32},
        ),
    ],
)
def test_publish_latest_schema_rejects_bad_docs(
    series_root: Path, tmp_path: Path, key: str, value: object
) -> None:
    doc = copy.deepcopy(_valid_latest(series_root, tmp_path))
    if value is publish:
        del doc[key]
    else:
        doc[key] = value
    with pytest.raises(publish.ContractInvalid):
        publish.validate("latest", doc)


def test_publish_latest_schema_rejects_corrected_figure_without_bed(
    series_root: Path, tmp_path: Path
) -> None:
    doc = _valid_latest(series_root, tmp_path)
    doc["bed_applied"] = False
    with pytest.raises(publish.ContractInvalid):
        publish.validate("latest", doc)


def test_publish_latest_output_dir_is_world_readable(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert out.stat().st_mode & 0o755 == 0o755


# --- series.json ----------------------------------------------------------


def _series(out: Path) -> dict:
    return json.loads((out / "series.json").read_text(encoding="utf-8"))


def _series_run(series_root: Path, tmp_path: Path) -> dict:
    records = _default_records()
    records.append(
        _record("2024-01-06", "S2A_14QMH_20240106_0_L2A", "ok", bed_applied=False)
    )
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    return _series(out)


def test_publish_series_entries_and_order(series_root: Path, tmp_path: Path) -> None:
    doc = _series_run(series_root, tmp_path)
    scenes = doc["scenes"]
    assert [s["date"] for s in scenes] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-04",
        "2024-01-05",
        "2024-01-06",
    ]
    assert doc["run_id"] == RUN_ID
    assert doc["series_sha256"] == "cd" * 32
    assert doc["schema_version"] == "1.0.0"
    assert doc["smolf_version"] == __version__
    assert [s["quality_light"] for s in scenes] == [
        "GOOD",
        "DOUBTFUL",
        "GOOD",
        "POOR",
        "GOOD",
    ]


def test_publish_series_sorted_ascending_for_unsorted_records(
    series_root: Path, tmp_path: Path
) -> None:
    records = list(reversed(_default_records()))
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    dates = [s["date"] for s in _series(out)["scenes"]]
    assert dates == sorted(dates)


def test_publish_series_poor_entry_is_all_null(
    series_root: Path, tmp_path: Path
) -> None:
    poor = _series_run(series_root, tmp_path)["scenes"][3]
    assert poor["item_id"] == "S2A_14QMH_20240105_0_L2A"
    assert poor["valid_frac"] == 0.4322
    nulls = {k for k, v in poor.items() if v is None}
    assert nulls == set(poor) - {"date", "item_id", "quality_light", "valid_frac"}
    assert poor["hyacinth_raw_ha"] is None
    assert poor["bed_applied"] is None
    assert poor["regime"] is None


def test_publish_series_entry_matches_latest_fields(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    latest = _latest(out)
    entry = next(s for s in _series(out)["scenes"] if s["date"] == latest["date"])
    assert entry == {k: latest[k] for k in entry}
    assert entry["regime"] == "EXPOSED_BED"


def test_publish_series_bed_not_applied_nulls(
    series_root: Path, tmp_path: Path
) -> None:
    entry = _series_run(series_root, tmp_path)["scenes"][4]
    assert entry["bed_applied"] is False
    assert entry["hyacinth_corrected_ha"] is None
    assert entry["cover_band"] is None
    assert entry["hyacinth_raw_ha"] == 631.11


def test_publish_series_byte_identical_and_canonical(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    a, b = tmp_path / "a", tmp_path / "b"
    assert _publish(a).exit_code == 0
    assert _publish(b).exit_code == 0
    text = (a / "series.json").read_text(encoding="utf-8")
    assert text == (b / "series.json").read_text(encoding="utf-8")
    assert text == publish.canonical_json(json.loads(text))


def test_publish_series_in_allowlist(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    assert {p.name for p in out.iterdir()} == {"latest.json", "series.json"}


def test_publish_series_schema_accepts_and_tolerates_extras(
    series_root: Path, tmp_path: Path
) -> None:
    doc = _series_run(series_root, tmp_path)
    publish.validate("series", doc)
    doc["scenes"][0]["extra"] = 1
    doc["extra"] = 1
    publish.validate("series", doc)


def _mutate_scene(doc: dict, index: int, key: str, value: object) -> None:
    doc["scenes"][index][key] = value


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [
        (3, "hyacinth_raw_ha", 10.0),  # POOR with figures
        (3, "dense_ha", 1.0),
        (3, "regime", "CLEAN"),
        (3, "bed_applied", True),
        (3, "quality_light", "BUENA"),  # unknown light
        (0, "quality_light", "BUENA"),
        (0, "regime", "clean"),
        (0, "hyacinth_raw_ha", None),  # non-POOR must have figures
        (0, "valid_frac", 1.5),
        (0, "date", "2024-01-01T00:00:00Z"),
        (4, "hyacinth_corrected_ha", 5.0),  # bed not applied
    ],
)
def test_publish_series_schema_rejects_bad_entries(
    series_root: Path, tmp_path: Path, index: int, key: str, value: object
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    _mutate_scene(doc, index, key, value)
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


@pytest.mark.parametrize("key", ["scenes", "run_id", "series_sha256", "schema_version"])
def test_publish_series_schema_rejects_missing_top_level(
    series_root: Path, tmp_path: Path, key: str
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    del doc[key]
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


def test_publish_series_invalid_output_exit_1_writes_nothing(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    real = publish.series_document

    def bad(*args: object) -> dict:
        doc = real(*args)
        doc["scenes"][0]["quality_light"] = "BUENA"
        return doc

    monkeypatch.setattr(publish, "series_document", bad)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 1
    assert not out.exists()
