"""`smolf export series-csv`: a verified download bundle from a finished publish.

The main seam is the CLI end to end over a synthetic publish directory in
tmp_path, built from real `series_document`/`latest_document` output and a
real `publish.json` (no network, no scenes). The pure function covers the
rare rows (POOR, bed not applied, a quoted reason, fixed decimals).
"""

import csv
import hashlib
import io
import itertools
import json
import re
from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

import smol_frontier
from smol_frontier import paths, publish
from smol_frontier.cli import app
from smol_frontier.publish import export
from smol_frontier.publish.writer import publish_index

RUN_ID = "20240110T000000"
SERIES_SHA = "cd" * 32
CONTRACT_DOC = Path(__file__).resolve().parents[1] / "docs" / "contract.md"
BOUNDS = [[20.1, -99.3], [20.2, -99.2]]
GENERATED = "2024-01-11T00:00:00Z"


def _row(day: str, item_id: str, **overrides: object) -> dict:
    """Unrounded series row as `series build` stores it in a scene record."""
    row = {
        "date": day,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "light": "GOOD",
        "regime": "clean",
        "valid_frac": 0.987654321,
        "cloud_frac": 0.012345678,
        "dense_ha": 400.123456,
        "interface_ha": 230.987654,
        "hyacinth_raw_ha": 631.11111,
        "hyacinth_under_haze_ha": 6.29,
        "pct_reservoir_raw": 50.019871234,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2010.123456,
        "removed_ha": 0.555555,
        "dense_corrected_ha": 399.876543,
        "interface_corrected_ha": 230.678912,
        "hyacinth_corrected_ha": 630.555455,
        "pct_reservoir_corrected": 49.996,
    }
    row.update(overrides)
    return row


def _record(day: str, item_id: str, status: str, **row_overrides: object) -> dict:
    """Scene record (ok | skipped) as `series build` writes it."""
    record = {
        "date": day,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "row": None,
        "status": status,
        "valid_frac": 0.4321987,
    }
    if status == "ok":
        record["row"] = _row(day, item_id, **row_overrides)
        record["valid_frac"] = record["row"]["valid_frac"]
    return record


def _records() -> list[dict]:
    """GOOD, DOUBTFUL, bed not applied (reason with a comma), POOR, latest GOOD."""
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
        _record(
            "2024-01-03",
            "S2A_14QMH_20240103_0_L2A",
            "ok",
            bed_applied=False,
            bed_reason="no SIH level for the day, correction skipped",
            level_m=None,
        ),
        _record("2024-01-04", "S2B_14QMH_20240104_0_L2A", "skipped"),
        _record("2024-01-05", "S2A_14QMH_20240105_0_L2A", "ok", regime="EXPOSED_BED"),
    ]


def _manifest(run_id: str = RUN_ID, series_sha: str = SERIES_SHA) -> dict:
    records = _records()
    return {
        "run_id": run_id,
        "series_sha256": series_sha,
        "counts": {
            "ok": sum(r["status"] == "ok" for r in records),
            "skipped": sum(r["status"] == "skipped" for r in records),
            "errors": 0,
        },
    }


def _latest_doc(levels: Path, run_id: str = RUN_ID) -> dict:
    latest = publish.latest_document(_records(), _manifest(run_id), levels)
    latest["map"]["bounds"] = BOUNDS
    latest["image"]["bounds"] = BOUNDS
    publish.validate("latest", latest)
    return latest


def _write_index(pub: Path) -> None:
    """A real publish.json over the files in ``pub`` (not itself)."""
    (pub / "publish.json").unlink(missing_ok=True)
    index = publish_index(pub, RUN_ID, SERIES_SHA, GENERATED)
    publish.validate("publish", index)
    (pub / "publish.json").write_text(publish.canonical_json(index), encoding="utf-8")


def _rewrite(pub: Path, name: str, document: dict) -> None:
    """Replace one document and re-index, so its hash matches again."""
    (pub / name).write_text(publish.canonical_json(document), encoding="utf-8")
    _write_index(pub)


@pytest.fixture
def levels(tmp_path: Path) -> Path:
    path = tmp_path / "ENDHD.csv"
    path.write_text(
        "Header line\r\n\r\n"
        "Estacion,Fecha,Nivel(m),Precipitacion(mm),VolumenAlm(Mm3),"
        "ObraToma(m3/s),Vertedor(m3/s),Derrame(m3/s),Evaporacion(mm)\r\n"
        "ENDHD,2024-01-09,2010.126,,,,,,\r\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def pub(tmp_path: Path, levels: Path) -> Path:
    """A synthetic publish directory: latest.json, series.json, publish.json."""
    pub = tmp_path / "publish"
    pub.mkdir()
    series_doc = publish.series_document(_records(), _manifest())
    publish.validate("series", series_doc)
    for name, document in (
        ("series.json", series_doc),
        ("latest.json", _latest_doc(levels)),
    ):
        (pub / name).write_text(publish.canonical_json(document), encoding="utf-8")
    _write_index(pub)
    return pub


def _export(pub: Path, out: Path):
    return CliRunner().invoke(
        app, ["export", "series-csv", "--from", str(pub), "--out", str(out)]
    )


def _series(pub: Path) -> dict:
    return json.loads((pub / "series.json").read_text(encoding="utf-8"))


def _rows(out: Path) -> list[dict[str, str]]:
    text = (out / "series.csv").read_bytes().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def _flat(scene: dict) -> dict:
    """A scene entry with display_ha flattened like the CSV columns."""
    flat = {k: v for k, v in scene.items() if k != "display_ha"}
    display = scene["display_ha"] or {}
    for part in ("total", "dense", "interface"):
        flat[f"display_ha_{part}"] = display.get(part)
    return flat


# --- column list ---------------------------------------------------------------


def _scene_schema_properties() -> set[str]:
    text = (
        resources.files("smol_frontier")
        .joinpath("schemas", "series.schema.json")
        .read_text(encoding="utf-8")
    )
    return set(json.loads(text)["$defs"]["scene"]["properties"])


def _unflatten(columns: tuple[str, ...]) -> list[str]:
    fields: list[str] = []
    for column in columns:
        name = "display_ha" if column.startswith("display_ha_") else column
        if name not in fields:
            fields.append(name)
    return fields


def test_scene_columns_cover_every_schema_property() -> None:
    assert set(_unflatten(export.SCENE_COLUMNS)) == _scene_schema_properties()


def test_scene_columns_follow_the_contract_per_scene_table() -> None:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    table = text.split("| Field | Type | Unit | Null when | Meaning |", 1)[1]
    fields = []
    for line in table.splitlines()[2:]:
        if not line.startswith("|"):
            break
        name = re.match(r"\| `([a-z_.]+)` \|", line).group(1)
        if "." not in name:
            fields.append(name)
    assert _unflatten(export.SCENE_COLUMNS) == fields


def test_series_csv_header_is_provenance_then_scene_fields(
    pub: Path, tmp_path: Path
) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    header = (out / "series.csv").read_bytes().split(b"\n", 1)[0].decode()
    assert header.split(",")[:7] == [
        "run_id",
        "series_sha256",
        "series_json_sha256",
        "schema_version",
        "smolf_version",
        "license",
        "rights_holder",
    ]
    assert tuple(header.split(",")[7:]) == export.SCENE_COLUMNS
    assert "display_ha_total,display_ha_dense,display_ha_interface" in header


# --- CLI end to end -----------------------------------------------------------


def test_series_csv_matches_series_json_field_by_field(
    pub: Path, tmp_path: Path
) -> None:
    out = tmp_path / "downloads"
    result = _export(pub, out)
    assert result.exit_code == 0, result.output
    scenes = _series(pub)["scenes"]
    rows = _rows(out)
    assert len(rows) == len(scenes) == 5
    assert [r["date"] for r in rows] == [s["date"] for s in scenes]
    assert any(r["quality_light"] == "POOR" for r in rows)
    for row, scene in zip(rows, scenes, strict=True):
        for field, value in _flat(scene).items():
            cell = row[field]
            if value is None:
                assert cell == "", field
            elif isinstance(value, bool):
                assert cell == ("true" if value else "false"), field
            elif isinstance(value, (int, float)):
                assert cell != "" and float(cell) == value, field
            else:
                assert cell == value, field
        assert set(row) == {
            *export.PROVENANCE_COLUMNS,
            *export.RIGHTS_COLUMNS,
            *_flat(scene),
        }


def test_series_csv_provenance_on_every_row(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    document = _series(pub)
    json_sha = hashlib.sha256((pub / "series.json").read_bytes()).hexdigest()
    for row in _rows(out):
        assert row["run_id"] == document["run_id"] == RUN_ID
        assert row["series_sha256"] == document["series_sha256"] == SERIES_SHA
        assert row["series_json_sha256"] == json_sha
        assert row["schema_version"] == document["schema_version"]
        assert row["smolf_version"] == document["smolf_version"]
        assert row["license"] == "CC-BY-4.0"
        assert row["rights_holder"] == "Oscar Olvera Neria (UAM Azcapotzalco)"


def test_series_csv_dialect(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    data = (out / "series.csv").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"\n")
    data.decode("ascii")
    lines = data.decode().splitlines()
    assert len(lines) == 6  # header + 5 scenes
    assert ",true," in lines[1]
    assert '"no SIH level for the day, correction skipped"' in data.decode()


def test_export_writes_the_bundle_byte_identically(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    assert _export(pub, out).exit_code == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(export.EXPORT_FILES)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == first
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ENDHD.csv",
        "downloads",
        "publish",
    ]


def test_export_creates_missing_parents(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "a" / "b" / "downloads"
    result = _export(pub, out)
    assert result.exit_code == 0
    assert result.stdout.strip() == f"exported: {out}"
    assert (out / "series.csv").is_file()


def test_export_defaults_follow_data_dir(
    pub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data))
    default_pub = data / "outputs/publish"
    default_pub.parent.mkdir(parents=True)
    pub.rename(default_pub)
    result = CliRunner().invoke(app, ["export", "series-csv"])
    assert result.exit_code == 0, result.output
    assert (data / "outputs/downloads/series.csv").is_file()


# --- refusals -------------------------------------------------------------------


def _no_index(pub: Path, levels: Path) -> None:
    (pub / "publish.json").unlink()


def _bad_index_json(pub: Path, levels: Path) -> None:
    (pub / "publish.json").write_text("{not json", encoding="utf-8")


def _schema_invalid_index(pub: Path, levels: Path) -> None:
    index = json.loads((pub / "publish.json").read_text(encoding="utf-8"))
    index["files"] = []
    (pub / "publish.json").write_text(json.dumps(index), encoding="utf-8")


def _series_tampered(pub: Path, levels: Path) -> None:
    path = pub / "series.json"
    path.write_bytes(path.read_bytes().replace(b'"GOOD"', b'"DOUBTFUL"', 1))


def _latest_tampered(pub: Path, levels: Path) -> None:
    path = pub / "latest.json"
    path.write_bytes(path.read_bytes() + b"\n")


def _series_missing(pub: Path, levels: Path) -> None:
    (pub / "series.json").unlink()


def _series_not_indexed(pub: Path, levels: Path) -> None:
    aside = pub.parent / "series.json.aside"
    (pub / "series.json").rename(aside)
    _write_index(pub)
    aside.rename(pub / "series.json")


def _series_schema_invalid(pub: Path, levels: Path) -> None:
    document = _series(pub)
    document["scenes"][0]["quality_light"] = "BUENA"
    _rewrite(pub, "series.json", document)


def _latest_other_run(pub: Path, levels: Path) -> None:
    _rewrite(pub, "latest.json", _latest_doc(levels, run_id="20230101T000000"))


def _latest_other_series(pub: Path, levels: Path) -> None:
    latest = _latest_doc(levels)
    latest["series_sha256"] = "ef" * 32
    _rewrite(pub, "latest.json", latest)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_no_index, "publish.json"),
        (_bad_index_json, "publish.json"),
        (_schema_invalid_index, "publish.json"),
        (_series_tampered, "series.json"),
        (_latest_tampered, "latest.json"),
        (_series_missing, "series.json"),
        (_series_not_indexed, "series.json"),
        (_series_schema_invalid, "series.json"),
        (
            _latest_other_run,
            (
                "latest.json does not belong to the run of series.json "
                f"(run_id: 20230101T000000 != {RUN_ID})"
            ),
        ),
        (_latest_other_series, "series_sha256"),
    ],
    ids=[
        "no-index",
        "index-not-json",
        "index-schema-invalid",
        "series-hash-mismatch",
        "latest-hash-mismatch",
        "series-missing",
        "series-not-indexed",
        "series-schema-invalid",
        "latest-other-run",
        "latest-other-series",
    ],
)
def test_export_refusals_exit_2_and_write_nothing(
    pub: Path, tmp_path: Path, levels: Path, damage, reason: str
) -> None:
    damage(pub, levels)
    out = tmp_path / "downloads"
    result = _export(pub, out)
    assert result.exit_code == 2
    assert result.stdout == ""
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("refused: ")
    assert reason in lines[0]
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ENDHD.csv", "publish"]


def test_export_refuses_out_that_is_a_file(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    out.write_text("keep me", encoding="utf-8")
    result = _export(pub, out)
    assert result.exit_code == 2
    assert result.stderr.startswith("refused: ")
    assert "not a directory" in result.stderr
    assert out.read_text(encoding="utf-8") == "keep me"


def test_export_refuses_out_inside_the_publish_dir(pub: Path) -> None:
    before = sorted(p.relative_to(pub) for p in pub.rglob("*"))
    result = _export(pub, pub / "downloads")
    assert result.exit_code == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert result.stderr.startswith("refused: ")
    assert sorted(p.relative_to(pub) for p in pub.rglob("*")) == before
    assert not list(pub.parent.glob(".downloads.*"))


@pytest.mark.parametrize("foreign", ["notes.txt", "sub/series.csv", "latest.json"])
def test_export_refuses_out_holding_a_foreign_file(
    pub: Path, tmp_path: Path, foreign: str
) -> None:
    out = tmp_path / "downloads"
    (out / foreign).parent.mkdir(parents=True, exist_ok=True)
    (out / foreign).write_text("keep me", encoding="utf-8")
    (out / "series.csv").write_text("old", encoding="utf-8")
    result = _export(pub, out)
    assert result.exit_code == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert result.stderr.startswith("refused: ")
    assert (out / foreign).read_text(encoding="utf-8") == "keep me"
    assert (out / "series.csv").read_text(encoding="utf-8") == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ENDHD.csv",
        "downloads",
        "publish",
    ]


def test_export_allowlist_is_the_five_bundle_files() -> None:
    assert export.EXPORT_FILES == frozenset(
        {"series.csv", "series_monthly.csv", "README.txt", "LEEME.txt", "SHA256SUMS"}
    )


def test_refused_export_leaves_stale_staging_alone(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    out.mkdir()
    (out / "notes.txt").write_text("keep me", encoding="utf-8")
    stale = tmp_path / ".downloads.staging-abc123"
    stale.mkdir()
    (stale / "series.csv").write_text("half", encoding="utf-8")
    result = _export(pub, out)
    assert result.exit_code == 2
    assert "notes.txt" in result.stderr
    assert (stale / "series.csv").read_text(encoding="utf-8") == "half"


def test_export_drops_stale_staging_on_success(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    stale = tmp_path / ".downloads.staging-abc123"
    stale.mkdir()
    (stale / "series.csv").write_text("half", encoding="utf-8")
    assert _export(pub, out).exit_code == 0
    assert not stale.exists()


def test_export_restores_a_lone_previous_before_checking(
    pub: Path, tmp_path: Path
) -> None:
    out = tmp_path / "downloads"
    previous = tmp_path / ".downloads.previous-.downloads.staging-abc123"
    previous.mkdir()
    (previous / "notes.txt").write_text("keep me", encoding="utf-8")
    result = _export(pub, out)
    assert result.exit_code == 2
    assert "notes.txt" in result.stderr
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert not previous.exists()


# --- pure function: the rare rows ------------------------------------------------


def _scene(**overrides: object) -> dict:
    scene = {
        "date": "2024-06-09",
        "item_id": "S2A_X",
        "quality_light": "GOOD",
        "regime": "EXPOSED_BED",
        "valid_frac": 0.95,
        "cloud_frac": 0.0,
        "dense_ha": 300.0,
        "interface_ha": 134.21,
        "hyacinth_raw_ha": 434.21,
        "hyacinth_under_haze_ha": 1.96,
        "pct_reservoir_raw": 34.41,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2004.6,
        "removed_ha": 418.51,
        "dense_corrected_ha": 10.0,
        "interface_corrected_ha": 5.7,
        "hyacinth_corrected_ha": 15.7,
        "pct_reservoir_corrected": 1.24,
        "cover_band": "GREEN",
        "display_status": "FULL",
        "display_ha": {"total": 16, "dense": 10, "interface": 6},
    }
    scene.update(overrides)
    return scene


_POOR = {
    "quality_light": "POOR",
    "valid_frac": 0.1,
    "display_status": "HIDDEN",
    "display_ha": None,
    **dict.fromkeys(
        (
            "regime",
            "cloud_frac",
            "dense_ha",
            "interface_ha",
            "hyacinth_raw_ha",
            "hyacinth_under_haze_ha",
            "pct_reservoir_raw",
            "bed_applied",
            "bed_reason",
            "level_m",
            "removed_ha",
            "dense_corrected_ha",
            "interface_corrected_ha",
            "hyacinth_corrected_ha",
            "pct_reservoir_corrected",
            "cover_band",
        )
    ),
}

_NO_BED = {
    "bed_applied": False,
    "bed_reason": 'no level, "SIH" gap',
    "level_m": None,
    "display_status": "HIDDEN",
    "display_ha": None,
    **dict.fromkeys(
        (
            "removed_ha",
            "dense_corrected_ha",
            "interface_corrected_ha",
            "hyacinth_corrected_ha",
            "pct_reservoir_corrected",
            "cover_band",
        )
    ),
}


# The fields of latest.json the export reads (besides the run identity).
_PURE_LATEST = {
    "run_id": RUN_ID,
    "series_sha256": SERIES_SHA,
    "date": "2031-03-04",
    "reservoir_area_ha": 1261.75,
    "constants": {
        "frac_publishable": 0.95,
        "green_max_pct": 30.0,
        "red_min_pct": 50.0,
        "green_max_ha": 378.53,
        "red_min_ha": 630.88,
    },
}


def _pure_files(*scenes: dict, monthly: list | None = None) -> dict[str, bytes]:
    document = {
        "schema_version": "3.3.0",
        "smolf_version": "0.8.1",
        "run_id": RUN_ID,
        "series_sha256": SERIES_SHA,
        "scenes": list(scenes),
        "monthly": monthly or [],
    }
    files = export.export_files(document, _PURE_LATEST, "ab" * 32)
    assert set(files) == export.EXPORT_FILES
    return files


def _csv_rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))


def _pure(*scenes: dict) -> list[dict[str, str]]:
    return _csv_rows(_pure_files(*scenes)["series.csv"])


def test_pure_fixed_decimals() -> None:
    (row,) = _pure(_scene())
    assert row["hyacinth_corrected_ha"] == "15.70"
    assert row["level_m"] == "2004.60"
    assert row["valid_frac"] == "0.9500"
    assert row["cloud_frac"] == "0.0000"
    assert row["dense_ha"] == "300.00"
    assert row["pct_reservoir_corrected"] == "1.24"
    assert row["display_ha_total"] == "16"
    assert row["display_ha_interface"] == "6"
    assert row["bed_applied"] == "true"
    assert row["bed_reason"] == ""
    assert row["series_json_sha256"] == "ab" * 32


def test_pure_poor_row_has_empty_figure_cells() -> None:
    (row,) = _pure(_scene(**_POOR))
    assert row["quality_light"] == "POOR"
    assert row["valid_frac"] == "0.1000"
    assert row["display_status"] == "HIDDEN"
    for field in (*_POOR, "display_ha_total", "display_ha_dense"):
        if field in ("quality_light", "valid_frac", "display_status", "display_ha"):
            continue
        assert row[field] == "", field


def test_pure_bed_not_applied_row() -> None:
    (row,) = _pure(_scene(**_NO_BED))
    assert row["bed_applied"] == "false"
    assert row["bed_reason"] == 'no level, "SIH" gap'
    assert row["hyacinth_raw_ha"] == "434.21"
    for field in (
        "level_m",
        "removed_ha",
        "hyacinth_corrected_ha",
        "cover_band",
        "display_ha_total",
        "display_ha_dense",
        "display_ha_interface",
    ):
        assert row[field] == "", field


def test_pure_quotes_a_reason_with_a_comma_rfc4180() -> None:
    files = _pure_files(_scene(**_NO_BED))
    assert b',"no level, ""SIH"" gap",' in files["series.csv"]
    assert b'"' not in files["series.csv"].split(b"\n")[0]


def test_pure_refuses_latest_from_another_run() -> None:
    document = {
        "schema_version": "3.3.0",
        "smolf_version": "0.8.1",
        "run_id": RUN_ID,
        "series_sha256": SERIES_SHA,
        "scenes": [],
        "monthly": [],
    }
    with pytest.raises(publish.PublishRefused, match="run"):
        export.export_files(
            document, {"run_id": "other", "series_sha256": SERIES_SHA}, "ab" * 32
        )


def test_pure_monthly_empty_month_has_empty_median() -> None:
    files = _pure_files(
        _scene(date="2024-01-09"),
        _scene(date="2024-03-02"),
        monthly=[
            {"month": "2024-01", "median_ha": 15.7, "n_scenes": 1},
            {"month": "2024-02", "median_ha": None, "n_scenes": 0},
            {"month": "2024-03", "median_ha": 0, "n_scenes": 1},
        ],
    )
    rows = _csv_rows(files["series_monthly.csv"])
    assert [(r["month"], r["median_ha"], r["n_scenes"]) for r in rows] == [
        ("2024-01", "15.70", "1"),
        ("2024-02", "", "0"),
        ("2024-03", "0.00", "1"),
    ]
    for row in rows:
        assert row["series_json_sha256"] == "ab" * 32
        assert row["run_id"] == RUN_ID


def test_pure_readme_year_comes_from_the_latest_scene() -> None:
    readme = _pure_files(_scene())["README.txt"].decode("ascii")
    assert "(C) 2031 Oscar Olvera Neria" in readme
    assert "Copernicus Sentinel data 2024" in readme


# --- monthly CSV, README, SHA256SUMS (CLI end to end) -------------------------


def test_series_monthly_csv_matches_series_json(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    data = (out / "series_monthly.csv").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"\n")
    header = data.split(b"\n", 1)[0].decode("ascii").split(",")
    assert header == [
        *export.PROVENANCE_COLUMNS,
        *export.RIGHTS_COLUMNS,
        "month",
        "median_ha",
        "n_scenes",
    ]
    document = _series(pub)
    json_sha = hashlib.sha256((pub / "series.json").read_bytes()).hexdigest()
    rows = _csv_rows(data)
    assert len(rows) == len(document["monthly"]) >= 1
    for row, month in zip(rows, document["monthly"], strict=True):
        assert row["month"] == month["month"]
        assert float(row["median_ha"]) == month["median_ha"]
        assert re.fullmatch(r"\d+\.\d\d", row["median_ha"])
        assert int(row["n_scenes"]) == month["n_scenes"]
        assert row["run_id"] == RUN_ID
        assert row["series_sha256"] == SERIES_SHA
        assert row["series_json_sha256"] == json_sha
        assert row["schema_version"] == document["schema_version"]
        assert row["smolf_version"] == document["smolf_version"]
        assert row["license"] == "CC-BY-4.0"
        assert row["rights_holder"] == "Oscar Olvera Neria (UAM Azcapotzalco)"


def test_column_dictionary_covers_every_column_of_both_csvs(
    pub: Path, tmp_path: Path
) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    columns = set()
    for name in ("series.csv", "series_monthly.csv"):
        header = (out / name).read_bytes().split(b"\n", 1)[0].decode("ascii")
        columns.update(header.split(","))
    assert set(export.COLUMN_DICTIONARY) == columns
    for name, entry in export.COLUMN_DICTIONARY.items():
        assert entry.unit and entry.meaning and entry.empty_when, name


def _sections(readme: str) -> list[str]:
    """Section titles: a line followed by a line of '=' of the same length."""
    lines = readme.splitlines()
    return [
        line
        for line, rule in itertools.pairwise(lines)
        if line and rule == "=" * len(line)
    ]


def test_readme_sections_in_order_with_provenance(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    readme = (out / "README.txt").read_bytes().decode("ascii")
    assert "\r" not in readme
    assert _sections(readme) == [
        "Water hyacinth on Endho Reservoir: Sentinel-2 series",
        "Licence",
        "Disclaimer",
        "Suggested citation",
        "Sources",
        "Provenance",
        "Column dictionary",
        "Full definitions",
    ]
    document = _series(pub)
    latest = json.loads((pub / "latest.json").read_text(encoding="utf-8"))
    json_sha = hashlib.sha256((pub / "series.json").read_bytes()).hexdigest()
    year = latest["date"][:4]
    assert f"(C) {year} Oscar Olvera Neria (UAM Azcapotzalco)" in readme
    assert "CC BY 4.0" in readme
    disclaimer = readme.split("\nDisclaimer\n", 1)[1].split("\nSuggested citation", 1)[
        0
    ]
    assert "not an official measurement" in disclaimer
    assert "CONAGUA" in disclaimer
    assert "without warranty" in disclaimer
    assert "https://creativecommons.org/licenses/by/4.0/" in readme
    assert "appropriate credit" in readme
    citation = readme.split("Suggested citation", 1)[1].split("Sources", 1)[0]
    assert RUN_ID in citation
    assert "https://github.com/oscolv/smol-frontier" in citation
    sources = readme.split("\nSources\n", 1)[1].split("\nProvenance\n", 1)[0]
    assert "modified Copernicus Sentinel data" in sources
    assert "SIH" in sources
    assert "JRC Global Surface Water v1.4" in sources
    provenance = readme.split("\nProvenance\n", 1)[1].split("\nColumn dictionary", 1)[0]
    assert f"run_id: {RUN_ID}" in provenance
    assert f"series_sha256: {SERIES_SHA}" in provenance
    assert "internal series.csv" in provenance
    assert "not the hash of the series.csv in this folder" in provenance
    assert f"series.json sha256: {json_sha}" in provenance
    assert f"schema_version: {document['schema_version']}" in provenance
    assert f"contract smolf_version: {document['smolf_version']}" in provenance
    assert f"exported by smolf: {smol_frontier.__version__}" in provenance
    assert f"reservoir_area_ha: {latest['reservoir_area_ha']:.2f}" in provenance
    for key, value in latest["constants"].items():
        assert f"{key}: {value}" in provenance
    dictionary = readme.split("\nColumn dictionary", 1)[1]
    for column in export.COLUMN_DICTIONARY:
        assert re.search(rf"^  {column}$", dictionary, re.MULTILINE), column
    pointer = readme.split("\nFull definitions\n", 1)[1]
    assert "docs/contract.md" in pointer
    assert "https://github.com/oscolv/smol-frontier" in pointer
    # Hashes are never wrapped; every other line fits a terminal.
    for line in readme.splitlines():
        assert len(line) <= 79 or re.search(r"[0-9a-f]{64}$", line), line


def test_sha256sums_verify_the_bundle(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    text = (out / "SHA256SUMS").read_bytes().decode("ascii")
    assert text.endswith("\n")
    lines = text.splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names == sorted(
        ["LEEME.txt", "README.txt", "series.csv", "series_monthly.csv"]
    )
    for line in lines:
        digest, name = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line).groups()
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


# --- LEEME.txt (Spanish README) -------------------------------------------------


def test_leeme_mirrors_the_readme_in_spanish(pub: Path, tmp_path: Path) -> None:
    out = tmp_path / "downloads"
    assert _export(pub, out).exit_code == 0
    readme = (out / "README.txt").read_bytes().decode("ascii")
    leeme = (out / "LEEME.txt").read_bytes().decode("utf-8")
    assert "\r" not in leeme
    assert "Endhó" in leeme
    assert _sections(leeme) == [
        "Lirio acuático en la presa Endhó: serie Sentinel-2",
        "Licencia",
        "Descargo de responsabilidad",
        "Cita sugerida",
        "Fuentes",
        "Procedencia",
        "Diccionario de columnas",
        "Definiciones completas",
    ]
    assert len(_sections(leeme)) == len(_sections(readme))
    latest = json.loads((pub / "latest.json").read_text(encoding="utf-8"))
    json_sha = hashlib.sha256((pub / "series.json").read_bytes()).hexdigest()
    year = latest["date"][:4]
    assert f"(C) {year} Oscar Olvera Neria (UAM Azcapotzalco)" in leeme
    assert "https://creativecommons.org/licenses/by/4.0/deed.es" in leeme
    assert "https://creativecommons.org/licenses/by/4.0/legalcode.es" in leeme
    disclaimer = leeme.split("\nDescargo de responsabilidad\n", 1)[1]
    disclaimer = " ".join(disclaimer.split("\nCita sugerida", 1)[0].split())
    assert "No son una medición oficial" in disclaimer
    assert "sin garantía" in disclaimer
    # One citation, byte-identical in both languages.
    citation_en = readme.split("\nSuggested citation\n", 1)[1].split("\nSources\n")[0]
    citation_es = leeme.split("\nCita sugerida\n", 1)[1].split("\nFuentes\n")[0]
    assert citation_en.split("\n\n")[-1].strip() in citation_es
    assert RUN_ID in citation_es
    # Provenance values are the same in both files.
    for value in (RUN_ID, SERIES_SHA, json_sha, smol_frontier.__version__):
        assert value in leeme.split("\nProcedencia\n", 1)[1]
    # Spanish prevails, and both files say so.
    assert "LEEME.txt" in readme and "prevails" in readme
    assert "README.txt" in leeme and "prevalece" in leeme
    # Codes stay verbatim, glossed in Spanish.
    for gloss in ("GOOD = BUENA", "POOR = MALA", "EXPOSED_BED = lecho expuesto"):
        assert gloss in " ".join(leeme.split()), gloss
    dictionary = leeme.split("\nDiccionario de columnas", 1)[1]
    for column in export.COLUMN_DICTIONARY:
        assert re.search(rf"^  {column}$", dictionary, re.MULTILINE), column
    for line in leeme.splitlines():
        assert len(line) <= 79 or re.search(r"[0-9a-f]{64}$", line), line


def test_spanish_dictionary_has_the_same_columns() -> None:
    assert set(export.COLUMN_DICTIONARY_ES) == set(export.COLUMN_DICTIONARY)
    for name, entry in export.COLUMN_DICTIONARY_ES.items():
        assert entry.unit and entry.meaning and entry.empty_when, name
