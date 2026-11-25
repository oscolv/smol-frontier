"""`smolf export maps`: one classified GeoTIFF per scene of a publish.

The scene rebuild is faked at the map-building boundary (``build_map``), so
these tests need no scene cache and no network. The fake returns a class map
whose pixel counts are the scene row's figures, as the real builder's are.
"""

import csv
import hashlib
import io
import json
import re
from pathlib import Path

import numpy as np
import pytest
import rasterio
from typer.testing import CliRunner

from smol_frontier import paths, publish, series
from smol_frontier.cli import app
from smol_frontier.hydro import curve
from smol_frontier.publish import map_export
from smol_frontier.publish.writer import publish_index

RUN_ID = "20240110T000000"
GENERATED = "2024-01-11T00:00:00Z"
BOUNDS = [[20.1, -99.3], [20.2, -99.2]]
SHAPE = (200, 400)
TRANSFORM = rasterio.Affine(10.0, 0.0, 460000.0, 0.0, -10.0, 2230000.0)
CRS = "EPSG:32614"


def _row(day: str, item_id: str, **overrides: object) -> dict:
    row = {
        "date": day,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "light": "GOOD",
        "regime": "clean",
        "valid_frac": 0.98,
        "cloud_frac": 0.01,
        "dense_ha": 40.0,
        "interface_ha": 30.0,
        "hyacinth_raw_ha": 70.0,
        "hyacinth_under_haze_ha": 1.5,
        "pct_reservoir_raw": 5.55,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2010.12,
        "removed_ha": 12.34,
        "dense_corrected_ha": 35.0,
        "interface_corrected_ha": 22.66,
        "hyacinth_corrected_ha": 57.66,
        "pct_reservoir_corrected": 4.57,
    }
    row.update(overrides)
    return row


def _record(day: str, item_id: str, status: str, **overrides: object) -> dict:
    record = {
        "date": day,
        "item_id_requested": item_id,
        "item_id_used": item_id,
        "backend_used": "earth-search",
        "row": None,
        "status": status,
        "valid_frac": 0.3,
    }
    if status == "ok":
        record["row"] = _row(day, item_id, **overrides)
        record["valid_frac"] = record["row"]["valid_frac"]
    return record


def _records() -> list[dict]:
    """GOOD, skipped (POOR), bed not applied, DOUBTFUL latest."""
    no_bed = {
        "bed_applied": False,
        "bed_reason": "no SIH level",
        "level_m": None,
        "removed_ha": None,
        "dense_corrected_ha": None,
        "interface_corrected_ha": None,
        "hyacinth_corrected_ha": None,
        "pct_reservoir_corrected": None,
    }
    return [
        _record("2024-01-01", "S2A_14QMH_20240101_0_L2A", "ok"),
        _record("2024-01-02", "S2B_14QMH_20240102_0_L2A", "skipped"),
        _record("2024-01-03", "S2A_14QMH_20240103_0_L2A", "ok", **no_bed),
        _record("2024-01-05", "S2A_14QMH_20240105_0_L2A", "ok", light="DOUBTFUL"),
    ]


OK_FILES = [
    "2024-01-01_S2A_14QMH_20240101_0_L2A.tif",
    "2024-01-03_S2A_14QMH_20240103_0_L2A.tif",
    "2024-01-05_S2A_14QMH_20240105_0_L2A.tif",
]


def _fake_class_map(row: dict) -> np.ndarray:
    """A class map whose counts are the row's figures (as build_map's are)."""
    applied = row["bed_applied"]
    counts = [
        (2, row["dense_corrected_ha"] if applied else row["dense_ha"]),
        (3, row["interface_corrected_ha"] if applied else row["interface_ha"]),
        (5, row["removed_ha"] if applied else 0.0),
    ]
    flat = np.ones(SHAPE[0] * SHAPE[1], dtype=np.uint8)  # water
    start = 0
    for code, hectares in counts:
        n = round(hectares * 100)
        flat[start : start + n] = code
        start += n
    flat[-10:] = 0  # some nodata
    return flat.reshape(SHAPE)


class _Layer:
    def __init__(self, class_map: np.ndarray) -> None:
        self.class_map = class_map
        self.transform = TRANSFORM
        self.crs = CRS


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the scene rebuild; return the item ids it was asked for."""
    calls: list[str] = []

    def build(record: dict, manifest: dict, committed: object) -> _Layer:
        calls.append(record["item_id_used"])
        return _Layer(_fake_class_map(record["row"]))

    monkeypatch.setattr(map_export, "build_map", build)
    return calls


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SMOLF_DATA_DIR with a complete run and its publish."""
    root = tmp_path / "data"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(root))
    publish_run(tmp_path, RUN_ID, _records())
    return root


def publish_run(tmp_path: Path, run_id: str, records: list[dict]) -> None:
    """Write a complete run of ``records`` and publish it (replacing any
    earlier publish)."""
    run_dir = paths.series_dir() / run_id
    (run_dir / "scenes").mkdir(parents=True)
    for record in records:
        name = f"{record['item_id_requested']}.json"
        (run_dir / "scenes" / name).write_text(json.dumps(record), encoding="utf-8")
    series.write_series_csv(run_dir / "series.csv", [r for r in records if r["row"]])
    series_sha = series.sha256_file(run_dir / "series.csv")
    manifest = {
        "run_id": run_id,
        "status": "complete",
        "series_sha256": series_sha,
        "code": {"dirty": False},
        "counts": {
            "ok": sum(r["status"] == "ok" for r in records),
            "skipped": sum(r["status"] == "skipped" for r in records),
            "errors": 0,
        },
        "inputs": {
            "stage_area_curve_csv": series.sha256_file(curve.DEFAULT_CURVE_CSV_PATH)
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    levels = tmp_path / "ENDHD.csv"
    levels.write_text(
        "Header line\r\n\r\n"
        "Estacion,Fecha,Nivel(m),Precipitacion(mm),VolumenAlm(Mm3),"
        "ObraToma(m3/s),Vertedor(m3/s),Derrame(m3/s),Evaporacion(mm)\r\n"
        "ENDHD,2024-01-09,2010.126,,,,,,\r\n",
        encoding="utf-8",
    )
    pub = paths.publish_dir()
    pub.mkdir(parents=True, exist_ok=True)
    series_doc = publish.series_document(
        records, manifest, publish.committed_curve(manifest)
    )
    latest = publish.latest_document(records, manifest, levels)
    latest["map"]["bounds"] = BOUNDS
    latest["image"]["bounds"] = BOUNDS
    for name, document in (("series.json", series_doc), ("latest.json", latest)):
        publish.validate(name.removesuffix(".json"), document)
        (pub / name).write_text(publish.canonical_json(document), encoding="utf-8")
    _write_index(pub, series_sha, run_id)


def _write_index(pub: Path, series_sha: str, run_id: str = RUN_ID) -> None:
    (pub / "publish.json").unlink(missing_ok=True)
    index = publish_index(pub, run_id, series_sha, GENERATED)
    (pub / "publish.json").write_text(publish.canonical_json(index), encoding="utf-8")


def _export(*args: str):
    return CliRunner().invoke(app, ["export", "maps", *args])


def test_export_writes_one_map_per_scene_with_figures(
    data_dir: Path, fake_build: list[str]
) -> None:
    result = _export()
    assert result.exit_code == 0, result.output
    out = data_dir / "outputs/maps"
    assert result.stdout.strip() == f"exported: {paths.maps_dir()}"
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [*OK_FILES, "index.csv", "README.txt", "SHA256SUMS"]
    )
    assert "S2B_14QMH_20240102_0_L2A" not in fake_build


def test_maps_have_the_published_map_format(
    data_dir: Path, fake_build: list[str]
) -> None:
    assert _export().exit_code == 0
    path = data_dir / "outputs/maps" / OK_FILES[0]
    with rasterio.open(path) as src:
        assert src.count == 1
        assert src.dtypes == ("uint8",)
        assert src.nodata == 0
        assert src.crs.to_epsg() == 32614
        assert src.res == (10.0, 10.0)
        assert src.transform == TRANSFORM
        assert src.profile["tiled"] is True
        assert src.block_shapes[0] == (256, 256)
        assert src.compression.name.lower() == "deflate"
        assert set(src.colormap(1)) >= {0, 1, 2, 3, 4, 5}
        assert set(src.tags(1)) >= {f"class_{code}" for code in range(6)}
        counts = np.bincount(src.read(1).ravel(), minlength=6)
    assert round(counts[5] * 0.01, 2) == 12.34
    assert round(counts[2] * 0.01, 2) == 35.0


def test_index_ties_every_map_to_its_scene_and_the_run(
    data_dir: Path, fake_build: list[str]
) -> None:
    assert _export().exit_code == 0
    out = data_dir / "outputs/maps"
    pub = data_dir / "outputs/publish"
    series_doc = json.loads((pub / "series.json").read_text(encoding="utf-8"))
    series_json_sha = hashlib.sha256((pub / "series.json").read_bytes()).hexdigest()
    text = (out / "index.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert tuple(rows[0]) == map_export.INDEX_COLUMNS
    assert [row["file"] for row in rows] == OK_FILES
    for row in rows:
        data = (out / row["file"]).read_bytes()
        assert row["file"] == f"{row['date']}_{row['item_id']}.tif"
        assert row["sha256"] == hashlib.sha256(data).hexdigest()
        assert row["bytes"] == str(len(data))
        assert row["run_id"] == RUN_ID
        assert row["series_sha256"] == series_doc["series_sha256"]
        assert row["series_json_sha256"] == series_json_sha
    assert series_json_sha in (out / "README.txt").read_text(encoding="ascii")


def test_sha256sums_verify_the_maps_and_index(
    data_dir: Path, fake_build: list[str]
) -> None:
    assert _export().exit_code == 0
    out = data_dir / "outputs/maps"
    lines = (out / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names == sorted([*OK_FILES, "README.txt", "index.csv"])
    for line in lines:
        digest, name = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line).groups()
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_export_is_byte_identical_across_runs(
    data_dir: Path, fake_build: list[str]
) -> None:
    out = data_dir / "outputs/maps"
    assert _export().exit_code == 0
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    assert _export().exit_code == 0
    assert {p.name: p.read_bytes() for p in out.iterdir()} == first


def _previous(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def _map_disagrees(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def build(record: dict, manifest: dict, committed: object) -> _Layer:
        class_map = _fake_class_map(record["row"])
        class_map[class_map == 5] = 1
        return _Layer(class_map)

    monkeypatch.setattr(map_export, "build_map", build)


def _run_missing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (data_dir / "outputs/series" / RUN_ID / "manifest.json").unlink()


def _run_other_series(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = data_dir / "outputs/series" / RUN_ID / "series.csv"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")


def _run_incomplete(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = data_dir / "outputs/series" / RUN_ID / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _series_tampered(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = data_dir / "outputs/publish/series.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")


@pytest.mark.parametrize(
    ("breaks", "reason"),
    [
        (_map_disagrees, "differs from series.json"),
        (_run_missing, "is not under"),
        (_run_other_series, "series.csv sha256"),
        (_run_incomplete, "is not complete"),
        (_series_tampered, "does not match its publish.json entry"),
    ],
)
def test_refusals_exit_2_and_keep_the_previous_export(
    data_dir: Path,
    fake_build: list[str],
    monkeypatch: pytest.MonkeyPatch,
    breaks,
    reason: str,
) -> None:
    out = data_dir / "outputs/maps"
    assert _export().exit_code == 0
    before = _previous(out)
    breaks(data_dir, monkeypatch)
    result = _export()
    assert result.exit_code == 2
    assert result.stderr.startswith("refused: ")
    assert reason in result.stderr
    assert _previous(out) == before


@pytest.mark.parametrize("foreign", ["notes.txt", "series.csv", "sub/x.tif"])
def test_export_refuses_out_holding_a_foreign_file(
    data_dir: Path, fake_build: list[str], tmp_path: Path, foreign: str
) -> None:
    out = tmp_path / "maps"
    (out / foreign).parent.mkdir(parents=True, exist_ok=True)
    (out / foreign).write_text("keep me", encoding="utf-8")
    result = _export("--out", str(out))
    assert result.exit_code == 2
    assert "does not own" in result.stderr
    assert (out / foreign).read_text(encoding="utf-8") == "keep me"


def test_export_refuses_out_inside_the_publish_dir(
    data_dir: Path, fake_build: list[str]
) -> None:
    result = _export("--out", str(data_dir / "outputs/publish/maps"))
    assert result.exit_code == 2
    assert "inside the publish directory" in result.stderr


def test_explicit_from_and_out_win_over_the_defaults(
    data_dir: Path, fake_build: list[str], tmp_path: Path
) -> None:
    out = tmp_path / "elsewhere" / "maps"
    result = _export("--from", str(data_dir / "outputs/publish"), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir() if p.suffix == ".tif") == OK_FILES
    assert not (data_dir / "outputs/maps").exists()
