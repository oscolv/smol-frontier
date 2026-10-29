"""Tests for the resumable series builder (offline, tiny fakes).

No network: every test either monkeypatches the per-scene function or the
seams inside process_scene (resolve/ingest/classify/bed). The live run is
the `smolf series build` CLI, never a pytest test.
"""

import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

from smol_frontier import series
from smol_frontier.classify.pipeline import SceneResult
from smol_frontier.cli import app
from smol_frontier.config import settings
from smol_frontier.hydro.bed import BedCorrection
from smol_frontier.series import (
    SERIES_FIELDS,
    FingerprintMismatch,
    SeriesTarget,
    build_run,
    check_fingerprint,
)


def _targets(n: int = 5) -> list[SeriesTarget]:
    base = date(2024, 1, 1)
    return [
        SeriesTarget(
            day=base + timedelta(days=i),
            item_id=f"S2A_14QMH_2024010{i + 1}_0_L2A",
        )
        for i in range(n)
    ]


def _fake_row(target: SeriesTarget, index: int) -> dict:
    dense, interface = 100.0 + index, 50.25 + index
    return {
        "date": target.day.isoformat(),
        "item_id_requested": target.item_id,
        "item_id_used": target.item_id,
        "backend_used": "earth-search",
        "fallback": False,
        "tile_cloud_pct": 5.5 + index,
        "light": "GOOD",
        "regime": "clean",
        "valid_frac": 0.98,
        "cloud_frac": 0.015,
        "dense_ha": dense,
        "interface_ha": interface,
        "hyacinth_raw_ha": dense + interface,
        "water_ha": 900.0,
        "soil_ha": 10.0,
        "pct_reservoir_raw": 12.345,
        "bed_applied": True,
        "bed_reason": "",
        "level_m": 2010.12,
        "margin_m": 2010.55,
        "expected_bed_ha": 300.0,
        "removed_ha": 20.0,
        "dense_corrected_ha": dense - 5.0,
        "interface_corrected_ha": interface - 15.0,
        "hyacinth_corrected_ha": dense + interface - 20.0,
        "pct_reservoir_corrected": 10.76,
    }


def _fake_record(target: SeriesTarget) -> dict:
    index = int(target.item_id[14:16])
    return {
        "date": target.day.isoformat(),
        "item_id_requested": target.item_id,
        "item_id_used": target.item_id,
        "backend_used": "earth-search",
        "fallback": False,
        "tile_cloud_pct": 5.5 + index,
        "status": "ok",
        "valid_frac": 0.98,
        "error": "",
        "cache_sha256": "deadbeef",
        "row": _fake_row(target, index),
    }


def _make_fake(interrupt_at: int | None = None):
    """Deterministic per-scene fake, optionally raising once to interrupt."""
    calls = {"n": 0}

    def fake(target: SeriesTarget) -> dict:
        calls["n"] += 1
        if interrupt_at is not None and calls["n"] == interrupt_at:
            raise KeyboardInterrupt
        return _fake_record(target)

    return fake


def _read_manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    targets = _targets()
    first = build_run(
        run_id="run_a",
        targets=targets,
        process_fn=_make_fake(),
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
        workers=2,
    )
    second = build_run(
        run_id="run_b",
        targets=targets,
        process_fn=_make_fake(),
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
        workers=2,
    )
    csv_a = (tmp_path / "run_a" / "series.csv").read_bytes()
    csv_b = (tmp_path / "run_b" / "series.csv").read_bytes()
    assert csv_a == csv_b
    assert first["series_sha256"] == second["series_sha256"]
    assert first["series_sha256"] == hashlib.sha256(csv_a).hexdigest()
    assert list(csv.DictReader(csv_a.decode().splitlines()).fieldnames or []) == list(
        SERIES_FIELDS
    )


def test_interrupt_then_resume_matches_uninterrupted(tmp_path: Path) -> None:
    targets = _targets()
    with pytest.raises(KeyboardInterrupt):
        build_run(
            run_id="run_k",
            targets=targets,
            process_fn=_make_fake(interrupt_at=3),
            output_dir=tmp_path,
            command=["smolf", "series", "build"],
            workers=1,
        )
    assert _read_manifest(tmp_path / "run_k")["status"] == "running"
    resumed = build_run(
        run_id="run_k",
        resume=True,
        process_fn=_make_fake(),
        output_dir=tmp_path,
        workers=1,
    )
    assert resumed["status"] == "complete"
    build_run(
        run_id="run_full",
        targets=targets,
        process_fn=_make_fake(),
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
        workers=1,
    )
    assert (tmp_path / "run_k" / "series.csv").read_bytes() == (
        tmp_path / "run_full" / "series.csv"
    ).read_bytes()


def test_resume_with_other_config_hash_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_run(
        run_id="run_fp",
        targets=_targets(2),
        process_fn=_make_fake(),
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
    )
    monkeypatch.setattr(series, "config_hash", lambda: "0" * 64)
    with pytest.raises(FingerprintMismatch, match="config_hash differs"):
        build_run(run_id="run_fp", resume=True, output_dir=tmp_path)
    # The CLI maps the refusal to exit code 2.
    monkeypatch.setattr(series, "OUTPUT_DIR", tmp_path)
    result = CliRunner().invoke(app, ["series", "build", "--resume", "run_fp"])
    assert result.exit_code == 2
    assert "config_hash differs" in result.output


def test_check_fingerprint_names_every_diff() -> None:
    current = {"git_commit": "aaa", "dirty": False, "config_hash": "111"}
    check_fingerprint(dict(current), current, "run_x")  # no raise
    with pytest.raises(FingerprintMismatch) as info:
        check_fingerprint(
            {"git_commit": "bbb", "dirty": True, "config_hash": "111"},
            current,
            "run_x",
        )
    assert "git_commit differs" in str(info.value)
    assert "dirty differs" in str(info.value)
    assert "config_hash" not in str(info.value)


def test_skipped_and_error_scenes_stay_out_of_csv(tmp_path: Path) -> None:
    def fake(target: SeriesTarget) -> dict:
        if "0102" in target.item_id:
            record = _fake_record(target)
            record["status"] = "skipped"
            record["valid_frac"] = 0.5
            record["row"] = None
            return record
        if "0103" in target.item_id:
            raise RuntimeError("boom")
        return _fake_record(target)

    manifest = build_run(
        run_id="run_mix",
        targets=_targets(3),
        process_fn=fake,
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
    )
    rows = list(csv.DictReader((tmp_path / "run_mix" / "series.csv").open()))
    assert len(rows) == 1
    assert manifest["counts"] == {"requested": 3, "ok": 1, "skipped": 1, "errors": 1}
    by_id = {entry["item_id_requested"]: entry for entry in manifest["scenes"]}
    skipped = by_id["S2A_14QMH_20240102_0_L2A"]
    assert skipped["status"] == "skipped"
    assert skipped["valid_frac"] == 0.5
    error = by_id["S2A_14QMH_20240103_0_L2A"]
    assert error["status"] == "error"
    assert error["error"] == "boom"


def test_bed_not_applied_leaves_corrected_columns_empty(tmp_path: Path) -> None:
    def fake(target: SeriesTarget) -> dict:
        record = _fake_record(target)
        assert isinstance(record["row"], dict)
        record["row"].update(
            {
                "bed_applied": False,
                "bed_reason": "no SIH level for 2024-01-01",
                "margin_m": None,
                "expected_bed_ha": None,
                "removed_ha": None,
                "dense_corrected_ha": None,
                "interface_corrected_ha": None,
                "hyacinth_corrected_ha": None,
                "pct_reservoir_corrected": None,
            }
        )
        return record

    build_run(
        run_id="run_bed",
        targets=_targets(1),
        process_fn=fake,
        output_dir=tmp_path,
        command=["smolf", "series", "build"],
    )
    (row,) = list(csv.DictReader((tmp_path / "run_bed" / "series.csv").open()))
    assert row["bed_applied"] == "false"
    assert row["bed_reason"] == "no SIH level for 2024-01-01"
    assert row["level_m"] == "2010.12"
    for column in (
        "margin_m",
        "expected_bed_ha",
        "removed_ha",
        "dense_corrected_ha",
        "interface_corrected_ha",
        "hyacinth_corrected_ha",
        "pct_reservoir_corrected",
    ):
        assert row[column] == "", column


def test_manifest_carries_run_provenance(tmp_path: Path) -> None:
    manifest = build_run(
        run_id="run_prov",
        targets=_targets(2),
        process_fn=_make_fake(),
        output_dir=tmp_path,
        command=["smolf", "series", "build", "--workers", "8"],
    )
    assert manifest["run_id"] == "run_prov"
    assert manifest["status"] == "complete"
    assert manifest["started_utc"] and manifest["finished_utc"]
    assert manifest["command"] == ["smolf", "series", "build", "--workers", "8"]
    assert manifest["mode"] == "reference"
    assert set(manifest["code"]) == {"git_commit", "dirty", "config_hash"}
    assert len(manifest["code"]["git_commit"]) == 40
    assert isinstance(manifest["code"]["dirty"], bool)
    assert set(manifest["environment"]) == {
        "package_version",
        "python_version",
        "uv_lock_sha256",
    }
    assert set(manifest["inputs"]) == {
        "aoi_polygon",
        "ENDHD_csv",
        "stage_area_curve_csv",
        "flood_stage_tif",
        "reference_csv",
    }
    assert len(manifest["requested"]) == 2
    (entry,) = [e for e in manifest["scenes"] if e["status"] == "ok"][:1]
    assert set(entry) >= {
        "item_id_requested",
        "item_id_used",
        "backend_used",
        "status",
        "cache_sha256",
    }


def _scene_result(valid_fraction: float) -> SceneResult:
    ha = {
        settings.CLASS_NODATA: 0.0,
        settings.CLASS_WATER: 900.0,
        settings.CLASS_DENSE_HYACINTH: 100.0,
        settings.CLASS_INTERFACE: 50.0,
        settings.CLASS_BARE_SOIL: 10.0,
    }
    return SceneResult(
        scene_id="fake",
        class_map=np.zeros((2, 2), dtype=np.uint8),
        ha_per_class=ha,
        hyacinth_total_ha=150.0,
        pct_reservoir=12.0,
        reservoir_area_ha=1261.75,
        valid_fraction=valid_fraction,
        cloud_fraction=0.01,
        gmm1_sep=2.0,
        gmm2_sep=2.0,
        nir_thr_used=None,
        nir_water_frac=0.1,
        n_rescued=0,
        n_substrate=0,
    )


def _fake_scene(item_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=item_id, source="earth-search", cloud_cover=7.5)


def test_process_scene_skips_poor_before_bed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real skip rule: valid_frac < MIN_VALID_FRACTION → skipped, no bed."""
    target = SeriesTarget(day=date(2025, 7, 29), item_id="S2A_14QMH_20250729_0_L2A")
    monkeypatch.setattr(
        series, "resolve_scene", lambda day, item_id: (_fake_scene(item_id), "")
    )
    monkeypatch.setattr(series, "ingest_scene", lambda scene: SimpleNamespace())
    monkeypatch.setattr(series, "classify_scene", lambda ing: _scene_result(0.5))

    def _forbidden(*args, **kwargs):
        msg = "bed correction must not run for a skipped scene"
        raise AssertionError(msg)

    monkeypatch.setattr(series, "correct_bed", _forbidden)
    record = series.process_scene(target)
    assert record["status"] == "skipped"
    assert record["valid_frac"] == 0.5
    assert record["row"] is None


def test_process_scene_ok_applies_real_quality_and_bed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ok path: quality light/regime plus the bed verdict in the row."""
    target = SeriesTarget(day=date(2024, 6, 9), item_id="S2A_14QMH_20240609_0_L2A")
    monkeypatch.setattr(
        series, "resolve_scene", lambda day, item_id: (_fake_scene(item_id), "")
    )
    monkeypatch.setattr(
        series,
        "ingest_scene",
        lambda scene: SimpleNamespace(transform="t", crs="c"),
    )
    monkeypatch.setattr(series, "classify_scene", lambda ing: _scene_result(0.98))
    bed = BedCorrection(
        applied=True,
        reason="",
        level_m=2010.12,
        margin_m=2010.55,
        expected_bed_ha=300.0,
        removed_ha=20.0,
        hyacinth_corrected_ha=130.0,
        dense_corrected_ha=95.0,
        interface_corrected_ha=35.0,
        pct_corrected=10.3,
    )
    monkeypatch.setattr(series, "correct_bed", lambda *args: bed)
    record = series.process_scene(target)
    assert record["status"] == "ok"
    row = record["row"]
    assert isinstance(row, dict)
    assert row["light"] == "GOOD"
    assert row["regime"] == "clean"
    assert row["tile_cloud_pct"] == 7.5
    assert row["bed_applied"] is True
    assert row["hyacinth_corrected_ha"] == 130.0


def test_process_scene_resolve_failure_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SeriesTarget(day=date(2024, 1, 1), item_id="missing-item")
    monkeypatch.setattr(
        series,
        "resolve_scene",
        lambda day, item_id: (_ for _ in ()).throw(RuntimeError("no such item")),
    )
    record = series.process_scene(target)
    assert record["status"] == "error"
    assert record["error"] == "no such item"
    assert record["item_id_used"] == ""
