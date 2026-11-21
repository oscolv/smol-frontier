"""Tests for `smolf publish` (offline, fixture run directories).

The seam is the CLI command run on a small fixture run under a tmp series
directory; assertions cover what it writes (file set, schema validity,
values, bytes) and how it exits. Schema negatives validate known-bad
documents directly against the packaged schema.
"""

import copy
import csv
import hashlib
import json
import os
import re
import shutil
import warnings
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from jsonschema import Draft202012Validator
from rasterio.features import rasterize
from rasterio.warp import (
    Resampling,
    calculate_default_transform,
    reproject,
    transform_bounds,
    transform_geom,
)
from shapely.geometry import LinearRing, Polygon, box, mapping, shape
from typer.testing import CliRunner

from smol_frontier import __version__, aoi, paths, publish, series
from smol_frontier.classify.display import (
    DISPLAY_CRS,
    DisplayImage,
    render_display_image,
    write_display_png,
)
from smol_frontier.classify.geotiff import PUBLISHED_COLORMAP, hex_to_rgba
from smol_frontier.cli import app
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.hydro import curve, flood_map, levels
from smol_frontier.hydro.bed import bed_margin, day_mirror
from smol_frontier.publish import truecolour
from smol_frontier.sources.stac import SceneMeta

RUN_ID = "20240110T120000"

# --- scene map fixture ------------------------------------------------------
# A 20x20 scene on a 10 m UTM grid. The ingest/classify boundary is faked
# (no network, no real reflectance), but the cache file, its sha256, the
# flood-stage map, the class-5 mask, the hectare checks and the GeoTIFF
# writer all run for real.
GRID_TRANSFORM = rasterio.Affine(10.0, 0.0, 460_000.0, 0.0, -10.0, 2_250_000.0)
GRID_CRS = "EPSG:32614"
DENSE = settings.CLASS_DENSE_HYACINTH
INTERFACE = settings.CLASS_INTERFACE
WATER = settings.CLASS_WATER
SOIL = settings.CLASS_BARE_SOIL
REMOVED = settings.CLASS_BED_REMOVED


def _class_map() -> np.ndarray:
    """Rows 0-2 dense, 3-4 interface, 5-18 water, row 19 soil | nodata."""
    classes = np.full((20, 20), WATER, dtype=np.uint8)
    classes[0:3] = DENSE
    classes[3:5] = INTERFACE
    classes[19, :10] = SOIL
    classes[19, 10:] = settings.CLASS_NODATA
    return classes


def _flood_stage() -> np.ndarray:
    """Mirror everywhere except: 56 dense px on safe bed (2011 m), 9 hyacinth
    px in the band between the row level 2010.12 and margin 2010.55
    (2010.3 m), soil on safe bed, and no stage under the nodata strip."""
    flood = np.full((20, 20), 2000.0, dtype=np.float32)
    flood.reshape(-1)[:56] = 2011.0
    # above the recomputed margin (2010.546) yet below the row's 2-dp
    # margin (2010.55): class 5 only when the full-precision margin is used
    flood[2, 15] = 2010.548
    flood[2, 16:] = 2010.3
    flood[3, :5] = 2010.3
    flood[19, :10] = 2011.0
    flood[19, 10:] = flood_map.FLOOD_NODATA
    return flood


SAFE_BED_HYACINTH_PX = 56
BAND_HYACINTH_PX = 9

# WHY: a straight stage-area curve with zero bootstrap width, so the cushion
#   is the model leg alone: at the row level 2010.123456 the margin is
#   2010.123456 + cushion / (100 * slope) = 2010.545984, published 2010.55.
CURVE_SLOPE_KM2_PER_M = 0.961
FULL_MARGIN_M = 2010.545984


def _write_curve(path: Path) -> Path:
    rows = ["stage_m,volume_hm3,area_km2,area_km2_p2_5,area_km2_p97_5"]
    for stage in (2000.0, 2020.0):
        area = (stage - 2000.0) * CURVE_SLOPE_KM2_PER_M
        rows.append(f"{stage},0.0,{area},{area},{area}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# WHY: the classifier hectares the fake classify reports — the default row's
#   unrounded figures, so the hundredth check passes unless a test edits them.
FAKE_HA = {
    settings.CLASS_NODATA: 0.0,
    WATER: 600.5,
    DENSE: 400.123456,
    INTERFACE: 230.987654,
    SOIL: 3.25,
}
# WHY: the default row's unrounded hectares rescued under haze (6.29 at 2 dp).
FAKE_UNDER_HAZE_HA = 6.28777


def _fake_reflectance(shape: tuple, blue: float = 0.05) -> np.ndarray:
    rows, cols = np.indices(shape, dtype=np.float64)
    ramp = (rows + cols) / max(sum(shape) - 2, 1)
    reflectance = np.stack([0.30 * ramp, 0.20 * (1 - ramp), np.full(shape, blue)])
    reflectance[:, 0, 0] = np.nan
    return reflectance


def _cache_bytes(item_id: str) -> bytes:
    return f"cached scene {item_id}\n".encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def scene_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Tmp scene cache + flood-stage map; fake ingest, STAC lookup, classify."""
    base = tmp_path_factory.mktemp("scene_env")
    cache_dir = base / "cache"
    cache_dir.mkdir()
    flood_path = base / "flood_stage.tif"
    flood = _flood_stage()
    with rasterio.open(
        flood_path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs=GRID_CRS,
        transform=GRID_TRANSFORM,
        nodata=flood_map.FLOOD_NODATA,
    ) as dst:
        dst.write(flood, 1)
    monkeypatch.setattr(paths, "cache_dir", lambda: cache_dir)
    monkeypatch.setattr(flood_map, "DEFAULT_FLOOD_PATH", flood_path)
    curve_path = _write_curve(base / "stage_area_curve.csv")
    monkeypatch.setattr(curve, "DEFAULT_CURVE_CSV_PATH", curve_path)
    env = SimpleNamespace(
        cache_dir=cache_dir,
        flood_path=flood_path,
        curve_path=curve_path,
        stac_calls=[],
        ingest_calls=[],
        download=_cache_bytes,
        ha=dict(FAKE_HA),
        under_haze_ha=FAKE_UNDER_HAZE_HA,
        classify_mirrors=[],
        photo_calls=[],
        photo_collection="sentinel-2-l2a",
        photo_blue={},  # item id -> blue reflectance of its fake photo
    )

    def fake_get_scene(backend: str, item_id: str) -> SceneMeta:
        env.stac_calls.append((backend, item_id))
        return SceneMeta(
            id=item_id,
            datetime=datetime(2024, 1, 1),
            source=backend,
            processing_baseline="05.11",
            boa_offset_applied=True,
            assets={"B04": "https://example.invalid/B04.tif"},
        )

    def fake_ingest_scene(scene: SceneMeta, *args: object, **kwargs: object):
        cache = Path(kwargs.get("cache_dir") or paths.cache_dir())
        path = cache / f"{scene.id}.tif"
        env.ingest_calls.append((scene.id, path.exists()))
        if not path.exists():  # a cache miss "downloads" the scene
            path.write_bytes(env.download(scene.id))
        return SimpleNamespace(
            scene=scene,
            transform=GRID_TRANSFORM,
            crs=GRID_CRS,
            inside=np.ones((20, 20), dtype=bool),
        )

    def fake_classify(ing: object, mirror: np.ndarray | None = None):
        env.classify_mirrors.append(mirror)
        return SimpleNamespace(
            class_map=_class_map(),
            ha_per_class=dict(env.ha),
            hyacinth_under_haze_ha=env.under_haze_ha,
        )

    def fake_read_true_colour(
        backend: str, item_id: str, transform: rasterio.Affine, shape: tuple
    ) -> truecolour.TrueColour:
        env.photo_calls.append((backend, item_id, transform, shape))
        blue = env.photo_blue.get(item_id, 0.05)
        return truecolour.TrueColour(
            _fake_reflectance(shape, blue), env.photo_collection
        )

    monkeypatch.setattr(truecolour, "read_true_colour", fake_read_true_colour)
    monkeypatch.setattr(publish.map, "get_scene", fake_get_scene)
    monkeypatch.setattr(publish.map, "ingest_scene", fake_ingest_scene)
    monkeypatch.setattr(publish.map, "classify_scene", fake_classify)
    return env


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
        "hyacinth_under_haze_ha": FAKE_UNDER_HAZE_HA,
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
    for record in records:  # cache the scene; the record pins its sha256
        if record["status"] == "ok":
            data = _cache_bytes(record["item_id_used"])
            (paths.cache_dir() / f"{record['item_id_used']}.tif").write_bytes(data)
            record["cache_sha256"] = _sha(data)
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
        "inputs": {
            "flood_stage_tif": series.sha256_file(flood_map.DEFAULT_FLOOD_PATH),
            "stage_area_curve_csv": series.sha256_file(curve.DEFAULT_CURVE_CSV_PATH),
        },
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
    monkeypatch.setattr(paths, "levels_path", lambda: path)
    return path


@pytest.fixture
def series_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "series"
    root.mkdir()
    monkeypatch.setattr(paths, "series_dir", lambda: root)
    return root


def _publish(out: Path, *args: str):
    """`smolf publish`; without --run it asks for the newest run (--latest)."""
    select = [] if "--run" in args else ["--latest"]
    return CliRunner().invoke(app, ["publish", *select, *args, "--out", str(out)])


def _latest(out: Path) -> dict:
    return json.loads((out / "latest.json").read_text(encoding="utf-8"))


def test_publish_latest_golden_values(
    series_root: Path, tmp_path: Path, levels_file: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "image",
        "latest.json",
        "map",
        "publish.json",
        "series.json",
    ]
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
        "hyacinth_under_haze_ha": 6.29,
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
        "display_status": "FULL",
        "display_ha": {"total": 631, "dense": 400, "interface": 231},
        "schema_version": "3.4.0",
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
        "reservoir_outline": publish.reservoir_outline(),
        "constants": {
            "frac_publishable": 0.95,
            "green_max_pct": 30.0,
            "red_min_pct": 50.0,
            "green_max_ha": 378.53,
            "red_min_ha": 630.88,
        },
        "current_level": {
            "date": "2024-01-09",
            "level_m": 2010.13,
            "levels_sha256": series.sha256_file(levels_file),
        },
        "map": {
            "geotiff": "map/2024-01-04_S2B_14QMH_20240104_0_L2A.tif",
            "png": "map/2024-01-04_S2B_14QMH_20240104_0_L2A.png",
            "bounds": _latest(out)["map"]["bounds"],
        },
        "image": {
            "path": "image/2024-01-04_S2B_14QMH_20240104_0_L2A.webp",
            "bounds": _latest(out)["map"]["bounds"],
            "attribution": "Copernicus Sentinel data 2024",
        },
        # two FULL January scenes tie on hectares: the earlier date wins
        "same_month": {
            "month": 1,
            "years": [
                {
                    "year": 2024,
                    "n_scenes": 2,
                    "scene": {
                        "item_id": "S2A_14QMH_20240101_0_L2A",
                        "date": "2024-01-01",
                        "hyacinth_corrected_ha": 630.56,
                        "display_status": "FULL",
                    },
                    "featured": True,
                    "image": {
                        "path": "image/clipped/2024-01-01_S2A_14QMH_20240101_0_L2A.webp",
                        "attribution": "Copernicus Sentinel data 2024",
                    },
                }
            ],
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
        # a stale number in the row: the publisher must still null it
        removed_ha=0.0,
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
        "removed_ha",
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


def test_publish_latest_matches_last_series_scene_on_same_date(
    series_root: Path, tmp_path: Path
) -> None:
    # Same-date scenes whose requested and used ids sort in opposite orders.
    first = _record("2024-01-04", "U_B", "ok")
    first["item_id_requested"] = "R_A"
    second = _record("2024-01-04", "U_A", "ok")
    second["item_id_requested"] = "R_B"
    _write_run(series_root, records=[first, second])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    scenes = json.loads((out / "series.json").read_text(encoding="utf-8"))["scenes"]
    latest = _latest(out)
    assert latest["item_id"] == scenes[-1]["item_id"] == "U_A"
    for key in ("date", "item_id", "dense_ha", "level_m", "cover_band"):
        assert latest[key] == scenes[-1][key]


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


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "pass --run <run_id> or --latest"),
        (["--run", RUN_ID, "--latest"], "not both"),
    ],
    ids=["neither", "both"],
)
def test_publish_requires_an_explicit_run_choice(
    series_root: Path, tmp_path: Path, args: list[str], message: str
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    result = CliRunner().invoke(app, ["publish", *args, "--out", str(out)])
    assert result.exit_code == 2
    assert message in result.stderr
    assert result.stdout == ""
    assert not out.exists()


def test_publish_refusal_goes_to_stderr(series_root: Path, tmp_path: Path) -> None:
    result = _publish(tmp_path / "pub", "--run", "nope")
    assert result.exit_code == 2
    assert result.stderr.startswith("refused: unknown run: nope")
    assert result.stdout == ""


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
        "green_max_ha": 300.04,
        "red_min_ha": 500.07,
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
    result = CliRunner().invoke(app, ["publish", "--latest", "--out", str(out)])
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
    "field",
    [
        "reservoir_outline",
        "display_status",
        "display_ha",
        "image",
        "hyacinth_under_haze_ha",
    ],
)
def test_publish_latest_schema_requires_3_x_fields(
    series_root: Path, tmp_path: Path, field: str
) -> None:
    doc = _valid_latest(series_root, tmp_path)
    del doc[field]
    with pytest.raises(publish.ContractInvalid):
        publish.validate("latest", doc)


def test_reservoir_outline_is_rfc7946_multipolygon_of_the_aoi() -> None:
    outline = publish.reservoir_outline()
    assert outline["type"] == "MultiPolygon"
    for polygon in outline["coordinates"]:
        exterior, *holes = polygon
        assert LinearRing(exterior).is_ccw
        assert not any(LinearRing(hole).is_ccw for hole in holes)
        for ring in polygon:
            assert ring[0] == ring[-1]
            for lon, lat in ring:
                assert (round(lon, 6), round(lat, 6)) == (lon, lat)
                assert -180 <= lon <= 180 and -90 <= lat <= 90
    geometry = shape(outline)
    assert geometry.is_valid
    utm = gpd.GeoSeries([geometry], crs="EPSG:4326").to_crs(settings.UTM_CRS)
    # Same basin as reservoir_area_ha: rounding to 6 decimals moves it < 0.01 ha.
    assert utm.area.iloc[0] / 10_000 == pytest.approx(aoi.reservoir_area_ha(), abs=0.01)


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
        ("reservoir_outline", {"type": "Polygon", "coordinates": [[]]}),
        ("reservoir_outline", {"type": "MultiPolygon"}),
        ("reservoir_outline", {"type": "MultiPolygon", "coordinates": []}),
        (
            "reservoir_outline",
            {
                "type": "MultiPolygon",
                "coordinates": [[[[-99.3, 20.1], [-99.2, 20.1], [-99.3, 20.1]]]],
            },
        ),
        (
            "reservoir_outline",
            {
                "type": "MultiPolygon",
                # latitude first: -99.3 is not a latitude
                "coordinates": [[[[20.1, -99.3], [20.2, -99.3], [20.2, -99.2]] * 2]],
            },
        ),
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
        ("hyacinth_under_haze_ha", -0.01),
        ("hyacinth_under_haze_ha", None),
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


@pytest.mark.parametrize("name", ["latest", "series"])
def test_publish_schema_removed_ha_follows_bed_applied(
    series_root: Path, tmp_path: Path, name: str
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    doc = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    scenes = [doc] if name == "latest" else doc["scenes"]
    scene = next(s for s in scenes if s["bed_applied"] is True)
    scene["removed_ha"] = None
    with pytest.raises(publish.ContractInvalid):
        publish.validate(name, doc)
    scene["bed_applied"] = False
    for key in (
        "dense_corrected_ha",
        "interface_corrected_ha",
        "hyacinth_corrected_ha",
        "pct_reservoir_corrected",
        "cover_band",
        "display_ha",
    ):
        scene[key] = None
    publish.validate(name, doc)
    scene["removed_ha"] = 0.0
    with pytest.raises(publish.ContractInvalid):
        publish.validate(name, doc)


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
    assert doc["schema_version"] == "3.4.0"
    assert doc["smolf_version"] == __version__
    assert [
        (s["quality_light"], s["bed_applied"], s["display_status"]) for s in scenes
    ] == [
        ("GOOD", True, "FULL"),
        ("DOUBTFUL", True, "HIDDEN"),
        ("GOOD", True, "FULL"),
        ("POOR", None, "HIDDEN"),
        ("GOOD", False, "HIDDEN"),
    ]


def test_publish_series_lower_bound_on_published_valid_frac(
    series_root: Path, tmp_path: Path
) -> None:
    records = [
        _record("2024-01-01", "S2A_14QMH_20240101_0_L2A", "ok", valid_frac=0.94996),
        _record("2024-01-02", "S2B_14QMH_20240102_0_L2A", "ok", valid_frac=0.94994),
    ]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    scenes = _series(out)["scenes"]
    assert [(s["valid_frac"], s["display_status"]) for s in scenes] == [
        (0.95, "FULL"),
        (0.9499, "LOWER_BOUND"),
    ]
    assert _latest(out)["display_status"] == "LOWER_BOUND"


def test_publish_display_ha_parts_sum_to_total_on_every_scene(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    scenes = [*_series(out)["scenes"], _latest(out)]
    with_figures = [s for s in scenes if s["hyacinth_corrected_ha"] is not None]
    assert len(with_figures) >= 3
    for scene in scenes:
        shown = scene["display_ha"]
        if scene["hyacinth_corrected_ha"] is None:
            assert shown is None, scene["date"]
            continue
        assert shown["dense"] + shown["interface"] == shown["total"], scene["date"]
        assert abs(shown["total"] - scene["hyacinth_corrected_ha"]) <= 0.5


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
    assert nulls == set(poor) - {
        "date",
        "item_id",
        "quality_light",
        "valid_frac",
        "display_status",
    }
    assert poor["display_status"] == "HIDDEN"
    assert poor["hyacinth_raw_ha"] is None
    assert poor["hyacinth_under_haze_ha"] is None
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
    assert entry["removed_ha"] is None
    assert entry["hyacinth_corrected_ha"] is None
    assert entry["cover_band"] is None
    assert entry["hyacinth_raw_ha"] == 631.11
    assert entry["hyacinth_under_haze_ha"] == 6.29


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
    assert {p.name for p in out.iterdir()} == {
        "image",
        "latest.json",
        "map",
        "publish.json",
        "series.json",
    }


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
        (3, "hyacinth_under_haze_ha", 1.0),
        (0, "hyacinth_under_haze_ha", None),
        (0, "hyacinth_under_haze_ha", -0.01),
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
        (3, "display_status", "FULL"),  # POOR is always HIDDEN
        (0, "display_status", "SHOWN"),
        (0, "display_status", None),
        (3, "display_ha", {"total": 0, "dense": 0, "interface": 0}),
        (4, "display_ha", {"total": 0, "dense": 0, "interface": 0}),
        (0, "display_ha", None),
        (0, "display_ha", {"total": 631.5, "dense": 400, "interface": 231}),
        (0, "display_ha", {"total": 631, "dense": 400}),
        (0, "display_ha", {"total": -1, "dense": 0, "interface": -1}),
    ],
)
def test_publish_series_schema_rejects_bad_entries(
    series_root: Path, tmp_path: Path, index: int, key: str, value: object
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    _mutate_scene(doc, index, key, value)
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


@pytest.mark.parametrize(
    "field", ["display_status", "display_ha", "hyacinth_under_haze_ha"]
)
@pytest.mark.parametrize("index", [0, 3])
def test_publish_series_schema_requires_per_scene_fields(
    series_root: Path, tmp_path: Path, index: int, field: str
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    del doc["scenes"][index][field]
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


def test_publish_series_monthly_mixes_full_lower_bound_and_hidden(
    series_root: Path, tmp_path: Path
) -> None:
    records = [
        _record(
            "2024-01-01",
            "S2A_14QMH_20240101_0_L2A",
            "ok",
            hyacinth_corrected_ha=630.56,
            dense_corrected_ha=400.0,
            interface_corrected_ha=230.56,
        ),
        _record(
            "2024-01-11",
            "S2B_14QMH_20240111_0_L2A",
            "ok",
            valid_frac=0.9,
            hyacinth_corrected_ha=100.004,
            dense_corrected_ha=60.0,
            interface_corrected_ha=40.004,
        ),
        _record(
            "2024-01-21",
            "S2A_14QMH_20240121_0_L2A",
            "ok",
            light="DOUBTFUL",
            hyacinth_corrected_ha=999.0,
            dense_corrected_ha=600.0,
            interface_corrected_ha=399.0,
        ),
        _record("2024-01-26", "S2B_14QMH_20240126_0_L2A", "error"),
        _record("2024-03-02", "S2A_14QMH_20240302_0_L2A", "skipped"),
    ]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 0
    doc = _series(out)
    statuses = [s["display_status"] for s in doc["scenes"]]
    assert statuses == ["FULL", "LOWER_BOUND", "HIDDEN", "HIDDEN"]
    assert doc["monthly"] == [
        {"month": "2024-01", "median_ha": 365.28, "n_scenes": 2},
        {"month": "2024-02", "median_ha": None, "n_scenes": 0},
        {"month": "2024-03", "median_ha": None, "n_scenes": 0},
    ]


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [
        (0, "median_ha", None),
        (1, "median_ha", 12.0),
        (0, "n_scenes", -1),
        (0, "month", "2024-13"),
        (0, "month", "2024-1"),
    ],
)
def test_publish_series_schema_rejects_bad_monthly(
    series_root: Path, tmp_path: Path, index: int, key: str, value: object
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    doc["monthly"].append({"month": "2024-02", "median_ha": None, "n_scenes": 0})
    publish.validate("series", doc)
    doc["monthly"][index][key] = value
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


@pytest.mark.parametrize(
    "key",
    [
        "scenes",
        "monthly",
        "run_id",
        "series_sha256",
        "schema_version",
        "stage_area_curve",
    ],
)
def test_publish_series_schema_rejects_missing_top_level(
    series_root: Path, tmp_path: Path, key: str
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    del doc[key]
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


# --- stage-area curve block (series.json) ---------------------------------

# WHY: the 3.3.0 series schema as tagged v0.9.0 (git show), committed so the
#   additivity check needs no git and no network.
SCHEMA_3_3_0 = Path(__file__).parent / "fixtures/schemas_3_3_0/series.schema.json"
COMMITTED_CURVE = PROJECT_ROOT / "data/hydro/stage_area_curve.csv"


def _curve_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _expected_curve_block(path: Path) -> dict:
    rows = _curve_rows(path)
    return {
        "stage_m": [round(float(r["stage_m"]), 3) for r in rows],
        "area_ha": [round(float(r["area_km2"]) * 100, 2) for r in rows],
        "area_ha_p2_5": [round(float(r["area_km2_p2_5"]) * 100, 2) for r in rows],
        "area_ha_p97_5": [round(float(r["area_km2_p97_5"]) * 100, 2) for r in rows],
        "stage_min_m": round(float(rows[0]["stage_m"]), 3),
        "stage_max_m": round(float(rows[-1]["stage_m"]), 3),
        "anchors": [
            {
                "stage_m": stage,
                "volume_hm3": volume,
                "source": publish.CURVE_ANCHOR_SOURCE,
            }
            for stage, volume in settings.CURVE_ANCHORS
        ],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_publish_series_stage_area_curve_is_the_fixture_curve(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    doc = _series_run(series_root, tmp_path)
    block = doc["stage_area_curve"]
    assert block == _expected_curve_block(scene_env.curve_path)
    assert block["area_ha"] == [0.0, 1922.0]
    assert block["sha256"] == series.sha256_file(scene_env.curve_path)
    assert "stage_area_curve" not in _latest(tmp_path / "pub")


def test_publish_series_stage_area_curve_is_the_committed_curve(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(curve, "DEFAULT_CURVE_CSV_PATH", COMMITTED_CURVE)
    record = _record(
        "2024-01-04",
        LATEST_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-01-04",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    _write_run(series_root, records=[record])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    block = _series(out)["stage_area_curve"]
    assert block == _expected_curve_block(COMMITTED_CURVE)
    assert len(block["stage_m"]) == len(_curve_rows(COMMITTED_CURVE)) == 400
    assert block["stage_max_m"] == settings.CURVE_GRID_TOP
    assert [a["stage_m"] for a in block["anchors"]] == [2003.16, 2018.0, 2020.0]
    assert [a["volume_hm3"] for a in block["anchors"]] == [52.1, 182.0, 208.0]


def test_publish_curve_hash_mismatch_exit_2_without_bed_correction(
    series_root: Path, tmp_path: Path
) -> None:
    record = _record(
        "2024-01-04",
        LATEST_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-01-04",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    run_dir = _write_run(series_root, records=[record])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["inputs"]["stage_area_curve_csv"] = "0" * 64
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "stage-area curve" in lines[0]
    assert not out.exists()


def test_publish_series_3_3_0_schema_accepts_the_curve_block(
    series_root: Path, tmp_path: Path
) -> None:
    doc = _series_run(series_root, tmp_path)
    assert doc["schema_version"] == "3.4.0"
    schema = json.loads(SCHEMA_3_3_0.read_text(encoding="utf-8"))
    assert "stage_area_curve" not in schema["properties"]
    Draft202012Validator(schema).validate(doc)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stage_m", "2000"),
        ("area_ha", [-1.0, 0.0]),
        ("sha256", "XYZ"),
        ("anchors", [{"stage_m": 2003.16, "volume_hm3": 52.1}]),
        ("stage_min_m", None),
    ],
)
def test_publish_series_schema_rejects_bad_curve_block(
    series_root: Path, tmp_path: Path, key: str, value: object
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    doc["stage_area_curve"][key] = value
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

    monkeypatch.setattr(publish.writer, "series_document", bad)
    out = tmp_path / "pub"
    assert _publish(out).exit_code == 1
    assert not out.exists()


# --- map GeoTIFF ------------------------------------------------------------

LATEST_ITEM = "S2B_14QMH_20240104_0_L2A"
MAP_REL = f"map/2024-01-04_{LATEST_ITEM}.tif"
PNG_REL = f"map/2024-01-04_{LATEST_ITEM}.png"


def _map(out: Path) -> tuple[np.ndarray, dict]:
    with rasterio.open(out / MAP_REL) as src:
        return src.read(1), {
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
            "crs": src.crs,
            "transform": src.transform,
            "colormap": src.colormap(1),
            "count": src.count,
        }


def test_publish_map_geotiff_written_with_class_5(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (out / "map").iterdir()) == sorted(
        [MAP_REL.split("/")[1], PNG_REL.split("/")[1]]
    )
    assert _latest(out)["map"]["geotiff"] == MAP_REL
    array, meta = _map(out)
    assert meta["dtype"] == "uint8"
    assert meta["count"] == 1
    assert meta["nodata"] == 0
    assert meta["crs"] == rasterio.CRS.from_string(GRID_CRS)
    assert meta["transform"] == GRID_TRANSFORM
    assert set(np.unique(array)) <= {0, 1, 2, 3, 4, 5}
    assert int((array == REMOVED).sum()) == SAFE_BED_HYACINTH_PX
    assert meta["colormap"][REMOVED] == hex_to_rgba(settings.COLOR_BED_REMOVED)
    # every other pixel keeps the classifier's class
    expected = _class_map()
    expected[array == REMOVED] = REMOVED
    assert np.array_equal(array, expected)
    assert array[0, 0] == REMOVED and expected[0, 0] == REMOVED


def test_publish_map_fixture_margin_is_consistent() -> None:
    grid = curve.load_curve(curve.DEFAULT_CURVE_CSV_PATH)
    margin = bed_margin(
        2010.123456, grid.stage_m, grid.area_km2, grid.area_p2_5, grid.area_p97_5
    )
    assert margin == pytest.approx(FULL_MARGIN_M, abs=1e-6)
    assert round(margin, 2) == _row("2024-01-04", LATEST_ITEM)["margin_m"]


def test_publish_map_uses_recomputed_full_precision_margin(
    series_root: Path, tmp_path: Path
) -> None:
    # The pixel at 2010.548 m lies between the full margin and the row's
    # 2-dp margin: the run removed it, so the map must too.
    _write_run(series_root, records=[_record("2024-01-04", LATEST_ITEM, "ok")])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    array, _ = _map(out)
    assert array[2, 15] == REMOVED
    assert int((array == REMOVED).sum()) == SAFE_BED_HYACINTH_PX


def test_publish_map_row_margin_inconsistent_exit_2(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(
        series_root,
        records=[_record("2024-01-04", LATEST_ITEM, "ok", margin_m=2010.2)],
    )
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "margin" in lines[0] and "2010.2" in lines[0]
    assert not out.exists()


def test_publish_map_stage_area_curve_hash_mismatch_exit_2(
    series_root: Path, tmp_path: Path
) -> None:
    run_dir = _write_run(series_root)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["inputs"]["stage_area_curve_csv"] = "0" * 64
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "stage-area curve" in lines[0]
    assert not out.exists()


def test_publish_map_bed_not_applied_has_no_class_5(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    record = _record(
        "2024-01-04",
        LATEST_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-01-04",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    _write_run(series_root, records=[record])
    # the flood-stage checks do not apply: a changed flood map is no refusal
    scene_env.flood_path.write_bytes(b"not a raster")
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    array, _ = _map(out)
    assert not (array == REMOVED).any()
    assert np.array_equal(array, _class_map())


def test_publish_map_cache_hash_mismatch_exit_2(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root)
    cached = scene_env.cache_dir / f"{LATEST_ITEM}.tif"
    data = bytearray(cached.read_bytes())
    data[0] ^= 0x01  # tamper one byte
    cached.write_bytes(bytes(data))
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "cache sha256" in lines[0] and LATEST_ITEM in lines[0]
    assert not out.exists()


def test_publish_map_missing_cache_reingests_by_exact_item_id(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    records = [
        _record(
            "2024-01-04",
            LATEST_ITEM,
            "ok",
        )
    ]
    records[0]["backend_used"] = "planetary-computer"
    _write_run(series_root, records=records)
    (scene_env.cache_dir / f"{LATEST_ITEM}.tif").unlink()
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    assert scene_env.stac_calls == [("planetary-computer", LATEST_ITEM)]
    assert (scene_env.cache_dir / f"{LATEST_ITEM}.tif").exists()
    assert (out / MAP_REL).exists()


def test_publish_map_reingested_cache_hash_mismatch_exit_2(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root)
    (scene_env.cache_dir / f"{LATEST_ITEM}.tif").unlink()
    scene_env.download = lambda item_id: b"a different product"
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    assert "cache sha256" in result.output
    assert not out.exists()


@pytest.mark.parametrize("field", ["dense_ha", "interface_ha", "water_ha", "soil_ha"])
def test_publish_map_hectare_mismatch_exit_2(
    series_root: Path, tmp_path: Path, field: str
) -> None:
    value = _row("2024-01-04", LATEST_ITEM)[field] + 0.01
    _write_run(
        series_root,
        records=[_record("2024-01-04", LATEST_ITEM, "ok", **{field: value})],
    )
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert field in lines[0]
    assert not out.exists()


def test_publish_map_hectares_compared_to_the_hundredth(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    scene_env.ha[DENSE] = 400.1249  # still 400.12 at 2 dp
    _write_run(series_root, records=[_record("2024-01-04", LATEST_ITEM, "ok")])
    assert _publish(tmp_path / "pub").exit_code == 0


def test_publish_map_reclassifies_with_the_run_row_mirror(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root, records=[_record("2024-01-04", LATEST_ITEM, "ok")])
    assert _publish(tmp_path / "pub").exit_code == 0
    (mirror,) = scene_env.classify_mirrors
    expected = day_mirror(_flood_stage(), FULL_MARGIN_M)
    assert mirror.dtype == bool
    assert np.array_equal(mirror, expected)
    # the full-precision margin, not the row's 2-dp one (pixel at 2010.548)
    assert not mirror[2, 15]


def test_publish_map_no_mirror_when_bed_not_applied(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    record = _record(
        "2024-01-04",
        LATEST_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-01-04",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    _write_run(series_root, records=[record])
    assert _publish(tmp_path / "pub").exit_code == 0
    assert scene_env.classify_mirrors == [None]


def test_publish_map_under_haze_mismatch_exit_2(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    scene_env.under_haze_ha = FAKE_UNDER_HAZE_HA + 0.01
    _write_run(series_root, records=[_record("2024-01-04", LATEST_ITEM, "ok")])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "hyacinth_under_haze_ha" in lines[0]
    assert not out.exists()


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_publish_run_without_under_haze_column_refused(
    series_root: Path, tmp_path: Path, missing: str
) -> None:
    records = _default_records()
    row = records[0]["row"]
    if missing == "absent":
        del row["hyacinth_under_haze_ha"]
    else:
        row["hyacinth_under_haze_ha"] = None
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "hyacinth_under_haze_ha" in lines[0]
    assert "S2A_14QMH_20240101_0_L2A" in lines[0]
    assert not out.exists()


def test_publish_map_removed_ha_mismatch_exit_2(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(
        series_root,
        records=[_record("2024-01-04", LATEST_ITEM, "ok", removed_ha=0.57)],
    )
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    assert "removed_ha" in result.output
    assert not out.exists()


def test_publish_map_flood_stage_hash_mismatch_exit_2(
    series_root: Path, tmp_path: Path
) -> None:
    run_dir = _write_run(series_root)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["inputs"]["flood_stage_tif"] = "0" * 64
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 2
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "flood-stage" in lines[0]
    assert not out.exists()


def test_publish_map_refusal_keeps_previous_output(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, "20240101T000000", [_record("2024-01-01", "A", "ok")])
    out = tmp_path / "pub"
    assert _publish(out, "--run", "20240101T000000").exit_code == 0
    before = sorted(p.relative_to(out).as_posix() for p in out.rglob("*"))
    _write_run(
        series_root,
        records=[_record("2024-01-04", LATEST_ITEM, "ok", removed_ha=0.57)],
    )
    assert _publish(out, "--run", RUN_ID).exit_code == 2
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == before
    assert before == [
        "image",
        "image/2024-01-01_A.webp",
        "image/clipped",
        "image/clipped/2024-01-01_A.webp",
        "latest.json",
        "map",
        "map/2024-01-01_A.png",
        "map/2024-01-01_A.tif",
        "publish.json",
        "series.json",
    ]


def test_publish_map_allowlist_rejects_other_names(tmp_path: Path) -> None:
    for name in ("map/2024-01-01_A.png.exe", "map/../x.tif", "B04.tif", "map/x.tif"):
        assert not publish.is_allowed(name), name
    assert publish.is_allowed("map/2024-01-01_S2B_14QMH_20240101_0_L2A.tif")
    assert publish.is_allowed("map/2024-01-01_S2B_14QMH_20240101_0_L2A.png")
    assert not publish.is_allowed("map/2024-01-01_A.png.aux.xml")
    assert not publish.is_allowed("map/2024-01-01_A.pgw")
    assert publish.is_allowed("latest.json")


# --- display PNG ------------------------------------------------------------


def _png(out: Path) -> tuple[np.ndarray, dict]:
    # the PNG is deliberately not georeferenced (bounds live in latest.json)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        src = rasterio.open(out / PNG_REL)
    with src:
        return src.read(), {
            "driver": src.driver,
            "count": src.count,
            "dtype": src.dtypes[0],
            "shape": src.shape,
        }


def test_publish_png_rgba_palette_and_transparent_nodata(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    rgba, meta = _png(out)
    assert meta["driver"] == "PNG" and meta["count"] == 4 and meta["dtype"] == "uint8"
    pixels = {
        tuple(rgba[:, r, c]) for r in range(rgba.shape[1]) for c in range(rgba.shape[2])
    }
    for code in (WATER, DENSE, INTERFACE, SOIL, REMOVED):
        assert PUBLISHED_COLORMAP[code] in pixels, code
    # every pixel is a palette colour or fully transparent
    transparent = {p for p in pixels if p[3] == 0}
    assert all(p[3] == 255 for p in pixels - transparent)
    assert pixels - transparent <= {PUBLISHED_COLORMAP[c] for c in range(1, 6)}
    assert transparent  # nodata strip and warped corners


def test_publish_png_warped_to_web_mercator_with_bounds(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    bounds = _latest(out)["map"]["bounds"]
    (south, west), (north, east) = bounds
    assert south < north and west < east
    assert -90 <= south and north <= 90 and -180 <= west and east <= 180
    assert all(round(v, 6) == v for pair in bounds for v in pair)
    left, top = GRID_TRANSFORM @ (0, 0)
    right, bottom = GRID_TRANSFORM @ (20, 20)
    w, s, e, n = transform_bounds(GRID_CRS, "EPSG:4326", left, bottom, right, top)
    # the warped extent encloses the native footprint, within about a pixel
    assert west <= w + 1e-4 and east >= e - 1e-4
    assert south <= s + 1e-4 and north >= n - 1e-4
    assert abs(west - w) < 3e-4 and abs(north - n) < 3e-4
    _, meta = _png(out)
    assert meta["shape"][0] >= 19 and meta["shape"][1] >= 19


def _png_class_counts(rgba: np.ndarray) -> dict[int, int]:
    """Pixels per class in the PNG: nodata is alpha 0, 1-5 their colour."""
    counts = {settings.CLASS_NODATA: int((rgba[3] == 0).sum())}
    for code in (WATER, DENSE, INTERFACE, SOIL, REMOVED):
        colour = np.array(PUBLISHED_COLORMAP[code])[:, None, None]
        counts[code] = int((rgba == colour).all(axis=0).sum())
    return counts


def _reference_warp(classes: np.ndarray, resampling: Resampling) -> dict[int, int]:
    """Class counts of a class map on the fixture grid warped to the display
    grid (the renderer's own grid recipe) with the given resampling."""
    height, width = classes.shape
    left, top = GRID_TRANSFORM @ (0, 0)
    right, bottom = GRID_TRANSFORM @ (width, height)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PendingDeprecationWarning)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            GRID_CRS, DISPLAY_CRS, width, height, left, bottom, right, top
        )
    warped = np.full((dst_height, dst_width), settings.CLASS_NODATA, dtype=np.uint8)
    reproject(
        classes,
        warped,
        src_transform=GRID_TRANSFORM,
        src_crs=GRID_CRS,
        dst_transform=dst_transform,
        dst_crs=DISPLAY_CRS,
        resampling=resampling,
        src_nodata=settings.CLASS_NODATA,
        dst_nodata=settings.CLASS_NODATA,
    )
    return {code: int((warped == code).sum()) for code in range(6)}


def test_publish_png_nearest_neighbour_matches_reference_warp(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    rgba, _ = _png(out)
    png_counts = _png_class_counts(rgba)
    assert sum(png_counts.values()) == rgba.shape[1] * rgba.shape[2]
    published, _ = _map(out)
    assert png_counts == _reference_warp(published, Resampling.nearest)
    assert png_counts[REMOVED] >= SAFE_BED_HYACINTH_PX


def test_publish_png_nearest_neighbour_not_bilinear(tmp_path: Path) -> None:
    # a water/class-5 checkerboard: bilinear blends 1 and 5 into codes 2-4
    #   at every pixel edge, so its counts cannot match a nearest warp
    #   (the fixture map's straight row edges survive bilinear unchanged)
    rows, cols = np.indices((20, 20))
    classes = np.where((rows + cols) % 2 == 0, WATER, REMOVED).astype(np.uint8)
    classes[19, 10:] = settings.CLASS_NODATA
    target = tmp_path / "x.png"
    write_display_png(target, render_display_image(classes, GRID_TRANSFORM, GRID_CRS))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(target) as src:
            png_counts = _png_class_counts(src.read())
    nearest = _reference_warp(classes, Resampling.nearest)
    bilinear = _reference_warp(classes, Resampling.bilinear)
    assert png_counts == nearest
    assert bilinear != nearest
    assert png_counts[DENSE] == png_counts[INTERFACE] == png_counts[SOIL] == 0


def test_publish_png_has_no_sidecar_files(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    files = sorted(p.suffix for p in (out / "map").iterdir())
    assert files == [".png", ".tif"]
    assert all(
        publish.is_allowed(p.relative_to(out).as_posix())
        for p in (out / "map").iterdir()
    )


def test_publish_png_only_latest_scene_maps_remain(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, "20240101T000000", [_record("2024-01-01", "A", "ok")])
    out = tmp_path / "pub"
    assert _publish(out, "--run", "20240101T000000").exit_code == 0
    assert sorted(p.name for p in (out / "map").iterdir()) == [
        "2024-01-01_A.png",
        "2024-01-01_A.tif",
    ]
    _write_run(series_root)
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert sorted(p.name for p in (out / "map").iterdir()) == [
        f"2024-01-04_{LATEST_ITEM}.png",
        f"2024-01-04_{LATEST_ITEM}.tif",
    ]


def test_publish_png_guard_rejects_float_and_class_6() -> None:
    with pytest.raises(ValueError, match="uint8"):
        render_display_image(
            np.zeros((4, 4), dtype=np.float32), GRID_TRANSFORM, GRID_CRS
        )
    bad = np.full((4, 4), 6, dtype=np.uint8)
    with pytest.raises(ValueError, match="outside classes"):
        render_display_image(bad, GRID_TRANSFORM, GRID_CRS)


@pytest.mark.parametrize(
    ("classes", "match"),
    [
        (np.zeros((4, 4), dtype=np.float32), "uint8"),
        (np.full((4, 4), 6, dtype=np.uint8), "outside classes"),
        (np.zeros((4, 4, 4), dtype=np.uint8), "2-D"),
    ],
)
def test_publish_png_writer_guards_hand_built_image(
    tmp_path: Path, classes: np.ndarray, match: str
) -> None:
    image = DisplayImage(classes, GRID_TRANSFORM, [[0.0, 0.0], [1.0, 1.0]])
    target = tmp_path / "x.png"
    with pytest.raises(ValueError, match=match):
        write_display_png(target, image)
    assert not target.exists()


def test_publish_png_schema_requires_png_and_bounds(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    good = _latest(out)
    publish.validate("latest", good)
    for key in ("geotiff", "png", "bounds"):
        broken = {**good, "map": {k: v for k, v in good["map"].items() if k != key}}
        with pytest.raises(publish.ContractInvalid):
            publish.validate("latest", broken)
    for bounds in ([[91, 0], [92, 1]], [[0, 0], [1]], [[0, 181], [1, 182]]):
        with pytest.raises(publish.ContractInvalid):
            publish.validate(
                "latest", {**good, "map": {**good["map"], "bounds": bounds}}
            )


# --- exposed-bed example ----------------------------------------------------

EXAMPLE_ITEM = "S2A_14QMH_20240609_0_L2A"
EXAMPLE_PNG = f"map/2024-06-09_{EXAMPLE_ITEM}.png"
EXAMPLE_RAW_PNG = f"map/2024-06-09_{EXAMPLE_ITEM}.raw.png"
AFTER_EXAMPLE_ITEM = "S2B_14QMH_20240701_0_L2A"


def _example_records() -> list[dict]:
    """The default run plus a 2024-06-09 record that applied the correction
    and a later scene, so the example is not the latest scene."""
    return [
        *_default_records(),
        _record("2024-06-09", EXAMPLE_ITEM, "ok", regime="EXPOSED_BED"),
        _record("2024-07-01", AFTER_EXAMPLE_ITEM, "ok"),
    ]


def _publish_records(series_root: Path, tmp_path: Path, records: list[dict]) -> Path:
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    return out


def _read_png(path: Path) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(path) as src:
            return src.read()


def test_publish_exposed_bed_example_is_the_record_entry_plus_maps(
    series_root: Path, tmp_path: Path
) -> None:
    out = _publish_records(series_root, tmp_path, _example_records())
    doc = _series(out)
    block = doc["exposed_bed_example"]
    [entry] = [s for s in doc["scenes"] if s["date"] == "2024-06-09"]
    bounds = _latest(out)["map"]["bounds"]
    assert block == {
        **entry,
        "margin_m": 2010.55,
        "expected_bed_ha": 120.0,
        "map_raw": {"path": EXAMPLE_RAW_PNG, "bounds": bounds},
        "map_corrected": {"path": EXAMPLE_PNG, "bounds": bounds},
    }
    assert block["item_id"] == EXAMPLE_ITEM
    assert block["bed_applied"] is True
    assert block["removed_ha"] == 0.56
    assert "exposed_bed_example" not in _latest(out)
    assert all(
        "margin_m" not in s and "expected_bed_ha" not in s for s in doc["scenes"]
    )
    assert _latest(out)["item_id"] == AFTER_EXAMPLE_ITEM


def test_publish_exposed_bed_example_pngs_raw_and_corrected(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    out = _publish_records(series_root, tmp_path, _example_records())
    block = _series(out)["exposed_bed_example"]
    raw = _read_png(out / block["map_raw"]["path"])
    corrected = _read_png(out / block["map_corrected"]["path"])
    latest_png = _read_png(out / _latest(out)["map"]["png"])
    assert raw.shape == corrected.shape == latest_png.shape
    raw_counts = _png_class_counts(raw)
    counts = _png_class_counts(corrected)
    assert raw_counts[REMOVED] == 0
    assert round(counts[REMOVED] * flood_map.PIXEL_HA, 2) == block["removed_ha"]
    # the raw map shows as hyacinth exactly what the corrected one removed
    raw_hyacinth = raw_counts[DENSE] + raw_counts[INTERFACE]
    assert raw_hyacinth == counts[DENSE] + counts[INTERFACE] + counts[REMOVED]
    for code in (settings.CLASS_NODATA, WATER, SOIL):
        assert raw_counts[code] == counts[code]
    # the example and the latest scene are each classified once
    assert len(scene_env.classify_mirrors) == 2


def test_publish_exposed_bed_example_pngs_listed_with_sha256(
    series_root: Path, tmp_path: Path
) -> None:
    out = _publish_records(series_root, tmp_path, _example_records())
    listed = {f["path"]: f["sha256"] for f in _index(out)["files"]}
    for rel in (EXAMPLE_PNG, EXAMPLE_RAW_PNG):
        assert listed[rel] == hashlib.sha256((out / rel).read_bytes()).hexdigest()
    # PNG only: no GeoTIFF of the example scene
    assert sorted(p.name for p in (out / "map").iterdir()) == sorted(
        [
            EXAMPLE_PNG.split("/")[1],
            EXAMPLE_RAW_PNG.split("/")[1],
            f"2024-07-01_{AFTER_EXAMPLE_ITEM}.png",
            f"2024-07-01_{AFTER_EXAMPLE_ITEM}.tif",
        ]
    )


def test_publish_exposed_bed_example_null_without_the_control_date(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert _series(out)["exposed_bed_example"] is None
    assert sorted(p.name for p in (out / "map").iterdir()) == sorted(
        [MAP_REL.split("/")[1], PNG_REL.split("/")[1]]
    )
    assert len(scene_env.classify_mirrors) == 1


def test_publish_exposed_bed_example_null_when_correction_not_applied(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    records = _example_records()
    records[-2] = _record(
        "2024-06-09",
        EXAMPLE_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-06-09",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    out = _publish_records(series_root, tmp_path, records)
    assert _series(out)["exposed_bed_example"] is None
    assert not (out / EXAMPLE_PNG).exists()
    assert not (out / EXAMPLE_RAW_PNG).exists()
    assert len(scene_env.classify_mirrors) == 1


def test_publish_exposed_bed_example_first_ok_record_of_the_date(
    series_root: Path, tmp_path: Path
) -> None:
    records = [
        _record("2024-06-09", "S2A_14QMH_20240609_0_L2A", "skipped"),
        _record("2024-06-09", "S2B_14QMH_20240609_0_L2A", "ok"),
        _record("2024-06-09", "S2C_14QMH_20240609_0_L2A", "ok", bed_applied=False),
        _record("2024-07-01", AFTER_EXAMPLE_ITEM, "ok"),
    ]
    out = _publish_records(series_root, tmp_path, records)
    block = _series(out)["exposed_bed_example"]
    assert block["item_id"] == "S2B_14QMH_20240609_0_L2A"
    assert block["map_raw"]["path"] == (
        "map/2024-06-09_S2B_14QMH_20240609_0_L2A.raw.png"
    )


def test_publish_exposed_bed_example_is_the_latest_scene_written_once(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    records = _example_records()[:-1]
    out = _publish_records(series_root, tmp_path, records)
    block = _series(out)["exposed_bed_example"]
    latest_map = _latest(out)["map"]
    assert block["map_corrected"] == {
        "path": latest_map["png"],
        "bounds": latest_map["bounds"],
    }
    assert sorted(p.name for p in (out / "map").iterdir()) == sorted(
        [
            EXAMPLE_PNG.split("/")[1],
            EXAMPLE_RAW_PNG.split("/")[1],
            f"2024-06-09_{EXAMPLE_ITEM}.tif",
        ]
    )
    listed = [f["path"] for f in _index(out)["files"]]
    assert listed.count(EXAMPLE_PNG) == 1
    assert len(scene_env.classify_mirrors) == 1


def test_publish_exposed_bed_example_off_the_latest_grid_exit_2(
    series_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scene_env: SimpleNamespace,
) -> None:
    records = _example_records()
    # a latest scene without the correction is not pinned to the flood grid
    records[-1] = _record(
        "2024-07-01",
        AFTER_EXAMPLE_ITEM,
        "ok",
        bed_applied=False,
        bed_reason="no SIH level for 2024-07-01",
        level_m=None,
        margin_m=None,
        expected_bed_ha=None,
        removed_ha=None,
    )
    _write_run(series_root, records=records)
    real = publish.map.ingest_scene

    def shifted(scene: SceneMeta, *args: object, **kwargs: object):
        ing = real(scene, *args, **kwargs)
        if scene.id == AFTER_EXAMPLE_ITEM:
            ing.transform = rasterio.Affine(
                10.0, 0.0, 460_005.0, 0.0, -10.0, 2_250_000.0
            )
        return ing

    monkeypatch.setattr(publish.map, "ingest_scene", shifted)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    assert "not on the grid of the latest map" in result.output
    assert not out.exists()
    assert scene_env.photo_calls == []  # refused before any photo read


def test_publish_exposed_bed_example_allowlist() -> None:
    assert publish.is_allowed(EXAMPLE_RAW_PNG)
    assert publish.is_allowed(EXAMPLE_PNG)
    assert publish.raw_png_path({"date": "2024-06-09", "item_id_used": "X"}) == (
        "map/2024-06-09_X.raw.png"
    )


def test_publish_exposed_bed_example_schema(series_root: Path, tmp_path: Path) -> None:
    good = _series(_publish_records(series_root, tmp_path, _example_records()))
    publish.validate("series", good)
    publish.validate("series", {**good, "exposed_bed_example": None})
    missing = {k: v for k, v in good.items() if k != "exposed_bed_example"}
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", missing)
    block = good["exposed_bed_example"]
    raw_path = block["map_raw"]["path"]
    for bad in (
        {**block, "map_raw": {"path": raw_path}},
        {**block, "map_raw": {**block["map_raw"], "path": EXAMPLE_PNG}},
        {**block, "map_corrected": {**block["map_raw"]}},
        {**block, "map_corrected": {**block["map_corrected"], "bounds": [[0, 0]]}},
        {k: v for k, v in block.items() if k != "map_corrected"},
        {k: v for k, v in block.items() if k != "removed_ha"},
        {k: v for k, v in block.items() if k != "margin_m"},
        {k: v for k, v in block.items() if k != "expected_bed_ha"},
        {**block, "margin_m": None},
        {**block, "expected_bed_ha": None},
        {**block, "bed_applied": False},
    ):
        with pytest.raises(publish.ContractInvalid):
            publish.validate("series", {**good, "exposed_bed_example": bad})
    schema = json.loads(SCHEMA_3_3_0.read_text(encoding="utf-8"))
    assert "exposed_bed_example" not in schema["properties"]
    Draft202012Validator(schema).validate(good)


IMAGE_REL = f"image/2024-01-04_{LATEST_ITEM}.webp"


def _webp(path: Path) -> tuple[np.ndarray, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(path) as src:
            return src.read(), src.driver


def _photo_byte(reflectance: float) -> int:
    """The published byte of a reflectance: fixed window, then gamma."""
    low, high = settings.TRUE_COLOUR_MIN, settings.TRUE_COLOUR_MAX
    scaled = (reflectance - low) / (high - low)
    return round(scaled ** (1 / settings.TRUE_COLOUR_GAMMA) * 255)


def _reservoir_on_png(reservoir: Polygon, png_shape: tuple[int, int]) -> np.ndarray:
    display = render_display_image(_class_map(), GRID_TRANSFORM, GRID_CRS)
    geom = transform_geom(GRID_CRS, "EPSG:3857", mapping(reservoir))
    return rasterize(
        [(geom, 1)], out_shape=png_shape, transform=display.transform, fill=0
    ).astype(bool)


# WHY a stand-in polygon: the fixture grid lies outside the real reservoir;
#   this one covers the grid's top half.
STAND_IN_RESERVOIR = box(460_000.0, 2_249_900.0, 460_200.0, 2_250_000.0)


def test_publish_image_is_rgba_webp_clipped_on_the_map_png_grid(
    series_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scene_env: SimpleNamespace,
) -> None:
    monkeypatch.setattr(aoi, "utm_geometry", lambda: STAND_IN_RESERVOIR)
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    latest = _latest(out)
    assert latest["image"] == {
        "path": IMAGE_REL,
        "bounds": latest["map"]["bounds"],
        "attribution": "Copernicus Sentinel data 2024",
    }
    pixels, driver = _webp(out / IMAGE_REL)
    png, png_meta = _png(out)
    assert driver == "WEBP"
    assert pixels.dtype == np.uint8
    assert pixels.shape == (4, *png_meta["shape"])
    assert (out / IMAGE_REL).stat().st_size < 300_000
    inside = _reservoir_on_png(STAND_IN_RESERVOIR, png_meta["shape"])
    assert 0 < inside.sum() < inside.size
    # the fake photo's no-data pixel (0, 0) is transparent, not black
    opaque = inside.copy()
    opaque[0, 0] = False
    assert inside[0, 0]
    np.testing.assert_array_equal(pixels[3], np.where(opaque, 255, 0))
    [(backend, item_id, transform, shape)] = [
        call for call in scene_env.photo_calls if call[1] == LATEST_ITEM
    ]
    assert (backend, item_id, shape) == ("earth-search", LATEST_ITEM, png.shape[1:])
    expected = render_display_image(_class_map(), GRID_TRANSFORM, GRID_CRS)
    assert transform == expected.transform
    assert abs(int(np.median(pixels[2][inside])) - _photo_byte(0.05)) <= 2


def test_publish_image_listed_with_sha256_and_byte_identical(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    first, second = tmp_path / "a", tmp_path / "b"
    assert _publish(first, "--run", RUN_ID).exit_code == 0
    assert _publish(second, "--run", RUN_ID).exit_code == 0
    data = (first / IMAGE_REL).read_bytes()
    assert data == (second / IMAGE_REL).read_bytes()
    listed = {f["path"]: f["sha256"] for f in _index(first)["files"]}
    assert listed[IMAGE_REL] == hashlib.sha256(data).hexdigest()


def test_publish_image_reader_failure_exit_2_keeps_previous(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    before = _tree(out, hashed=True)

    def unreachable(*args: object) -> None:
        msg = "HTTP 503 from earth-search"
        raise RuntimeError(msg)

    monkeypatch.setattr(truecolour, "read_true_colour", unreachable)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    assert "refused: true-colour image" in result.output
    assert "HTTP 503" in result.output
    assert _tree(out, hashed=True) == before
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_image_non_sentinel2_collection_never_published(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    before = _tree(out, hashed=True)
    scene_env.photo_collection = "planetscope"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 1
    assert isinstance(result.exception, ValueError)
    assert _tree(out, hashed=True) == before
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_image_allowlist() -> None:
    for name in (
        "image/2024-01-01_S2B_14QMH_20240101_0_L2A.webp",
        "image/clipped/2024-01-01_S2B_14QMH_20240101_0_L2A.webp",
    ):
        assert publish.is_allowed(name), name
    for name in (
        "image/2024-01-01_A.png",
        "image/2024-01-01_A.webp.aux.xml",
        "image/other/2024-01-01_A.webp",
        "image/clipped/clipped/2024-01-01_A.webp",
        "image/A.webp",
        "map/2024-01-01_A.webp",
    ):
        assert not publish.is_allowed(name), name


def test_publish_out_with_clipped_image_is_a_previous_contract(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    stale = out / "image" / "clipped" / "2023-01-04_A.webp"
    stale.write_bytes(b"old")
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert not stale.exists()
    assert (out / "image" / "clipped").is_dir()


def test_publish_image_schema_rejects_bad_image(
    series_root: Path, tmp_path: Path
) -> None:
    good = _valid_latest(series_root, tmp_path)
    image = good["image"]
    for bad in (
        {**image, "path": "image/clipped/2024-01-04_A.webp"},
        {**image, "path": "map/2024-01-04_A.png"},
        {**image, "attribution": "Copernicus Sentinel data"},
        {**image, "attribution": "PlanetScope 2024"},
        {key: value for key, value in image.items() if key != "bounds"},
        {**image, "bounds": [[0, 0]]},
    ):
        with pytest.raises(publish.ContractInvalid):
            publish.validate("latest", {**good, "image": bad})


# --- same_month block ----------------------------------------------------


def _ha_record(day: str, item_id: str, ha: float, **overrides: object) -> dict:
    """ok record whose corrected hectares are ``ha`` (all dense)."""
    return _record(
        day,
        item_id,
        "ok",
        hyacinth_corrected_ha=ha,
        dense_corrected_ha=ha,
        interface_corrected_ha=0.0,
        **overrides,
    )


def _same_month_records() -> list[dict]:
    """Feb 2023 FULL+LOWER_BOUND, no Feb 2024, Feb 2025 FULL+DOUBTFUL, then a
    LOWER_BOUND and a DOUBTFUL March 2025: latest.json is the DOUBTFUL one,
    the month comes from the latest FULL scene (February)."""
    return [
        _record("2023-01-05", "S2A_POOR_20230105", "skipped"),
        _ha_record("2023-02-03", "S2A_A_20230203", 300.0),
        _ha_record("2023-02-13", "S2B_B_20230213", 100.0, valid_frac=0.9),
        _ha_record("2024-07-01", "S2A_C_20240701", 50.0),
        _ha_record("2025-02-10", "S2B_D_20250210", 500.0),
        _ha_record("2025-02-20", "S2A_E_20250220", 10.0, light="DOUBTFUL"),
        _ha_record("2025-03-01", "S2B_F_20250301", 20.0, valid_frac=0.9),
        _ha_record("2025-03-15", "S2A_G_20250315", 30.0, light="DOUBTFUL"),
    ]


def _clipped(day: str, item_id: str) -> str:
    return f"image/clipped/{day}_{item_id}.webp"


@pytest.mark.parametrize(
    ("newest", "item_id", "status"),
    [
        ("2025-03-15", "S2A_G_20250315", "HIDDEN"),
        ("2025-03-01", "S2B_F_20250301", "LOWER_BOUND"),
    ],
)
def test_publish_same_month_from_latest_full_scene_with_empty_year(
    series_root: Path,
    tmp_path: Path,
    scene_env: SimpleNamespace,
    newest: str,
    item_id: str,
    status: str,
) -> None:
    records = [r for r in _same_month_records() if r["date"] <= newest]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    latest = _latest(out)
    assert (latest["date"], latest["display_status"]) == (newest, status)
    assert latest["same_month"] == {
        "month": 2,
        "years": [
            {
                "year": 2023,
                "n_scenes": 2,
                "scene": {
                    "item_id": "S2B_B_20230213",
                    "date": "2023-02-13",
                    "hyacinth_corrected_ha": 100.0,
                    "display_status": "LOWER_BOUND",
                },
                "featured": True,
                "image": {
                    "path": _clipped("2023-02-13", "S2B_B_20230213"),
                    "attribution": "Copernicus Sentinel data 2023",
                },
            },
            {
                "year": 2024,
                "n_scenes": 0,
                "scene": None,
                "featured": False,
                "image": None,
            },
            {
                "year": 2025,
                "n_scenes": 1,
                "scene": {
                    "item_id": "S2B_D_20250210",
                    "date": "2025-02-10",
                    "hyacinth_corrected_ha": 500.0,
                    "display_status": "FULL",
                },
                "featured": True,
                "image": {
                    "path": _clipped("2025-02-10", "S2B_D_20250210"),
                    "attribution": "Copernicus Sentinel data 2025",
                },
            },
        ],
    }
    assert sorted(p.name for p in (out / "image" / "clipped").iterdir()) == [
        "2023-02-13_S2B_B_20230213.webp",
        "2025-02-10_S2B_D_20250210.webp",
    ]
    read = sorted(item_id for _, item_id, _, _ in scene_env.photo_calls)
    assert read == sorted([item_id, "S2B_B_20230213", "S2B_D_20250210"])


def test_publish_same_month_latest_scene_photo_read_once(
    series_root: Path, tmp_path: Path, scene_env: SimpleNamespace
) -> None:
    _write_run(series_root, records=[_ha_record("2024-01-04", LATEST_ITEM, 10.0)])
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert [call[1] for call in scene_env.photo_calls] == [LATEST_ITEM]
    assert (out / IMAGE_REL).is_file()
    assert (out / _clipped("2024-01-04", LATEST_ITEM)).is_file()


def test_publish_same_month_clipped_images_are_rgba_transparent_outside(
    series_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scene_env: SimpleNamespace,
) -> None:
    blue = {"S2B_B_20230213": 0.10, "S2B_D_20250210": 0.25}
    scene_env.photo_blue.update(blue)
    monkeypatch.setattr(aoi, "utm_geometry", lambda: STAND_IN_RESERVOIR)
    _write_run(series_root, records=_same_month_records())
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    png_path = out / _latest(out)["map"]["png"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(png_path) as src:
            png_shape = src.shape
    inside = _reservoir_on_png(STAND_IN_RESERVOIR, png_shape)
    assert 0 < inside.sum() < inside.size
    for year in _latest(out)["same_month"]["years"]:
        if year["image"] is None:
            continue
        pixels, driver = _webp(out / year["image"]["path"])
        assert driver == "WEBP"
        assert pixels.dtype == np.uint8
        assert pixels.shape == (4, *png_shape)
        opaque = inside.copy()
        opaque[0, 0] = False  # the fake photo's no-data pixel
        np.testing.assert_array_equal(pixels[3], np.where(opaque, 255, 0))
        # the photo under each path is that year's scene's
        expected = _photo_byte(blue[year["scene"]["item_id"]])
        assert abs(int(np.median(pixels[2][inside])) - expected) <= 2


def test_publish_same_month_images_listed_with_sha256_and_byte_identical(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, records=_same_month_records())
    first, second = tmp_path / "a", tmp_path / "b"
    assert _publish(first, "--run", RUN_ID).exit_code == 0
    assert _publish(second, "--run", RUN_ID).exit_code == 0
    listed = {f["path"]: f["sha256"] for f in _index(first)["files"]}
    names = [
        _clipped("2023-02-13", "S2B_B_20230213"),
        _clipped("2025-02-10", "S2B_D_20250210"),
    ]
    for name in names:
        data = (first / name).read_bytes()
        assert data == (second / name).read_bytes()
        assert listed[name] == hashlib.sha256(data).hexdigest()


def test_publish_same_month_null_without_full_scene(
    series_root: Path, tmp_path: Path
) -> None:
    records = [
        _ha_record("2024-01-01", "S2A_A_20240101", 10.0, valid_frac=0.9),
        _ha_record("2024-01-04", LATEST_ITEM, 20.0, light="DOUBTFUL"),
    ]
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert _latest(out)["same_month"] is None
    assert not (out / "image" / "clipped").exists()


def test_publish_same_month_reader_failure_exit_2_keeps_previous(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root, records=_same_month_records())
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    before = _tree(out, hashed=True)
    reader = truecolour.read_true_colour

    def fails_for_2023(backend: str, item_id: str, *args: object):
        if item_id == "S2B_B_20230213":
            msg = "HTTP 503 from earth-search"
            raise RuntimeError(msg)
        return reader(backend, item_id, *args)

    monkeypatch.setattr(truecolour, "read_true_colour", fails_for_2023)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2
    assert "refused: true-colour image of S2B_B_20230213" in result.output
    assert _tree(out, hashed=True) == before
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_same_month_schema_rejects_bad_blocks(
    series_root: Path, tmp_path: Path
) -> None:
    good = _valid_latest(series_root, tmp_path)
    publish.validate("latest", {**good, "same_month": None})
    block = good["same_month"]
    year = block["years"][0]
    empty = {"year": 2023, "n_scenes": 0, "scene": None, "image": None}
    publish.validate("latest", {**good, "same_month": {**block, "years": [empty]}})
    not_featured = {**empty, "featured": False}
    publish.validate(
        "latest", {**good, "same_month": {**block, "years": [not_featured]}}
    )
    assert all(isinstance(y["featured"], bool) for y in block["years"])
    for bad in (
        {**block, "month": 13},
        {**block, "month": 0},
        {**block, "years": []},
        {key: value for key, value in block.items() if key != "years"},
        {**block, "years": [{**year, "scene": None}]},
        {**block, "years": [{**year, "image": None}]},
        {**block, "years": [{**empty, "image": year["image"]}]},
        {**block, "years": [{**empty, "featured": True}]},
        {**block, "years": [{**year, "featured": True, "scene": None}]},
        {**block, "years": [{**year, "featured": True, "image": None}]},
        {**block, "years": [{**year, "featured": "yes"}]},
        {**block, "years": [{**year, "n_scenes": -1}]},
        {
            **block,
            "years": [{**year, "scene": {**year["scene"], "display_status": "HIDDEN"}}],
        },
        {**block, "years": [{**year, "image": {**year["image"], "path": IMAGE_REL}}]},
        {**block, "years": [{**year, "image": {**year["image"], "attribution": "x"}}]},
    ):
        with pytest.raises(publish.ContractInvalid):
            publish.validate("latest", {**good, "same_month": bad})
    without = {key: value for key, value in good.items() if key != "same_month"}
    with pytest.raises(publish.ContractInvalid):
        publish.validate("latest", without)


# --- publish.json integrity index ---------------------------------------


def _index(out: Path) -> dict:
    return json.loads((out / "publish.json").read_text(encoding="utf-8"))


def _valid_index() -> dict:
    return {
        "files": [{"path": "latest.json", "sha256": "ab" * 32}],
        "generated_utc": "2026-11-16T10:00:00Z",
        "run_id": RUN_ID,
        "schema_version": "3.4.0",
        "series_sha256": "cd" * 32,
        "smolf_version": "0.5.0",
    }


def test_publish_index_lists_every_other_file_with_sha256(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    index = _index(out)
    on_disk = sorted(
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and p.name != "publish.json"
    )
    assert [f["path"] for f in index["files"]] == on_disk
    assert len(on_disk) == 6
    for entry in index["files"]:
        data = (out / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert publish.is_allowed(entry["path"])
    assert "publish.json" not in [f["path"] for f in index["files"]]


def test_publish_index_header_fields(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    index = _index(out)
    series_doc = json.loads((out / "series.json").read_text(encoding="utf-8"))
    assert index["run_id"] == RUN_ID
    assert index["series_sha256"] == series_doc["series_sha256"]
    assert index["schema_version"] == publish.SCHEMA_VERSION
    assert index["smolf_version"] == __version__
    stamp = datetime.strptime(index["generated_utc"], "%Y-%m-%dT%H:%M:%SZ")
    assert stamp.year >= 2026
    assert (out / "publish.json").read_text(encoding="utf-8") == publish.canonical_json(
        index
    )


def test_publish_index_only_nondeterministic_file(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    first = {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in out.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(series, "utc_now", lambda: "2030-01-01T00:00:00Z")
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    second = {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in out.rglob("*")
        if p.is_file()
    }
    changed = {k for k in first if first[k] != second[k]}
    assert changed == {"publish.json"}
    assert _index(out)["generated_utc"] == "2030-01-01T00:00:00Z"


def test_publish_index_schema_accepts_valid() -> None:
    publish.validate("publish", _valid_index())
    doc = _valid_index()
    doc["extra"] = 1
    publish.validate("publish", doc)


def _drop_sha(doc: dict) -> None:
    del doc["files"][0]["sha256"]


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_sha,
        lambda d: d["files"][0].update(sha256="ab" * 31),
        lambda d: d["files"][0].update(sha256="AB" * 32),
        lambda d: d["files"][0].update(sha256="zz" * 32),
        lambda d: d["files"][0].update(path="publish.json"),
        lambda d: d["files"][0].update(path="../latest.json"),
        lambda d: d["files"][0].update(path="map/B04.tif"),
        lambda d: d.update(files=[]),
        lambda d: d.update(generated_utc="2026-11-16 10:00:00"),
        lambda d: d.update(generated_utc="2026-11-16T10:00:00+00:00"),
        lambda d: d.update(schema_version="1.0.0"),
        lambda d: d.update(schema_version="2.0.0\n"),
        lambda d: d.update(series_sha256="short"),
        lambda d: d.pop("run_id"),
        lambda d: d.pop("smolf_version"),
    ],
)
def test_publish_index_schema_rejects_bad_docs(mutate) -> None:
    doc = _valid_index()
    mutate(doc)
    with pytest.raises(publish.ContractInvalid):
        publish.validate("publish", doc)


def test_publish_index_invalid_exit_1_nothing_swapped(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    monkeypatch.setattr(series, "utc_now", lambda: "not-a-date")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 1
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["series"]


def test_publish_index_replaces_previous_output(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    first = _index(out)
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert _index(out)["files"] == first["files"]


# --- --out safety -----------------------------------------------------------


def test_publish_out_foreign_dir_refused_and_untouched(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "precious"
    out.mkdir()
    (out / "data.txt").write_text("keep me")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "not a previous publish" in lines[0]
    assert (out / "data.txt").read_text() == "keep me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["precious", "series"]


def test_publish_out_contract_plus_foreign_file_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    (out / "map" / "notes.txt").write_text("keep me")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "map/notes.txt" in result.output
    assert (out / "map" / "notes.txt").read_text() == "keep me"


def test_publish_out_allowlisted_files_without_index_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    out.mkdir()
    (out / "latest.json").write_text("{}")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert (out / "latest.json").read_text() == "{}"


def test_publish_out_empty_dir_allowed(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    out.mkdir()
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    assert (out / "publish.json").is_file()


def test_publish_out_file_refused(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    out.write_text("keep me")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert out.read_text() == "keep me"


def test_publish_out_contract_plus_foreign_empty_dir_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    (out / "backup").mkdir()
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert (out / "backup").is_dir()


def test_publish_out_foreign_dir_refused_without_walking_it(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "home"
    (out / "deep" / "tree").mkdir(parents=True)
    (out / "deep" / "tree" / "data.txt").write_text("keep me")

    def no_walk(self: Path, pattern: str, *args: object, **kwargs: object):
        raise AssertionError(f"walked {self} for {pattern}")

    monkeypatch.setattr(Path, "rglob", no_walk)
    monkeypatch.setattr(Path, "walk", no_walk, raising=False)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "not a previous publish" in result.output
    assert (out / "deep" / "tree" / "data.txt").read_text() == "keep me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "series"]


def test_publish_out_walk_stops_at_first_foreign_entry(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    (out / "backup").mkdir()
    for i in range(200):
        (out / "backup" / f"f{i}.txt").write_text("keep me")
    seen: list[Path] = []
    real_rglob = Path.rglob

    def counting_rglob(self: Path, pattern: str, *args: object, **kwargs: object):
        for path in real_rglob(self, pattern, *args, **kwargs):
            seen.append(path)
            yield path

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "(foreign: backup)" in result.output
    # top level holds 5 entries; the walk must not descend into backup/
    assert len(seen) <= 10, len(seen)
    assert len(list((out / "backup").iterdir())) == 200


def _siblings(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_publish_out_symlink_to_contract_publishes_through(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real = real_parent / "pub"
    assert _publish(real, "--run", RUN_ID).exit_code == 0
    (real / "latest.json").write_text("{}")  # proves the swap replaced it
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link = link_parent / "pub"
    link.symlink_to(real, target_is_directory=True)
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert _latest(link)["run_id"] == RUN_ID
    assert (real / "publish.json").is_file()
    assert _siblings(link_parent) == ["pub"]
    assert _siblings(real_parent) == ["pub"]


def test_publish_out_relative_symlink_to_contract_publishes_through(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    real = tmp_path / "real" / "pub"
    real.parent.mkdir()
    assert _publish(real, "--run", RUN_ID).exit_code == 0
    link = tmp_path / "pub-link"
    link.symlink_to(Path("real") / "pub", target_is_directory=True)
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert os.readlink(link) == str(Path("real") / "pub")
    assert _siblings(tmp_path) == ["pub-link", "real", "series"]
    assert _siblings(real.parent) == ["pub"]


def test_publish_out_symlink_to_foreign_dir_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    foreign = tmp_path / "precious"
    foreign.mkdir()
    (foreign / "data.txt").write_text("keep me")
    link = tmp_path / "pub"
    link.symlink_to(foreign, target_is_directory=True)
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "not a previous publish" in result.output
    assert f"--out {link} (-> {foreign})" in " ".join(result.output.split())
    assert link.is_symlink()
    assert _siblings(foreign) == ["data.txt"]
    assert _siblings(tmp_path) == ["precious", "pub", "series"]


def test_publish_out_broken_symlink_refused(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    link = tmp_path / "pub"
    link.symlink_to(tmp_path / "missing", target_is_directory=True)
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "broken symlink" in result.output
    assert link.is_symlink()
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_out_broken_symlink_ancestor_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    par = tmp_path / "par"  # e.g. outputs/ linked to an unmounted drive
    par.symlink_to(tmp_path / "gone" / "deeper", target_is_directory=True)
    result = _publish(par / "pub", "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "broken symlink" in result.output
    assert par.is_symlink()
    assert _siblings(tmp_path) == ["par", "series"]


def test_publish_out_missing_parents_created(series_root: Path, tmp_path: Path) -> None:
    _write_run(series_root)
    out = tmp_path / "a" / "b" / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert (out / "publish.json").is_file()
    assert _siblings(out.parent) == ["pub"]


# --- crash recovery -----------------------------------------------------------


def _crash_mid_swap(
    out: Path, monkeypatch: pytest.MonkeyPatch, real: Path | None = None
) -> None:
    """Publish once more into an existing ``out`` and die between the renames.

    WHY: a killed process runs neither the swap's restore nor the staging
    cleanup, so both are disabled: every rename onto ``out`` (the second
    rename and the restore) raises, and ``rmtree`` does nothing.
    """
    real_rename = Path.rename

    def crashing_rename(self: Path, target: object) -> Path:
        if Path(str(target)) == (out if real is None else real):
            raise OSError("killed between the two renames")
        return real_rename(self, target)

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", crashing_rename)
        m.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
        with pytest.raises(OSError, match="killed"):
            publish.publish_run(RUN_ID, out)


def _debris(directory: Path, name: str) -> list[str]:
    return sorted(
        p.name
        for p in directory.iterdir()
        if p.name.startswith((f".{name}.staging-", f".{name}.previous-"))
    )


def test_publish_recovers_from_crash_mid_swap(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    _crash_mid_swap(out, monkeypatch)
    debris = _debris(tmp_path, "pub")
    assert not out.exists()
    assert [d.split("-")[0] for d in debris] == [".pub.previous", ".pub.staging"]
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert _index(out)["run_id"] == RUN_ID
    assert _debris(tmp_path, "pub") == []
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_restores_lone_previous_before_proceeding(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    before = (out / "latest.json").read_bytes()
    out.rename(tmp_path / ".pub.previous-.pub.staging-abc123")
    # a refused publish still keeps the restored contract at --out
    refused = _publish(out, "--run", "19990101T000000")
    assert refused.exit_code == 2, refused.output
    assert (out / "latest.json").read_bytes() == before
    assert _siblings(tmp_path) == ["pub", "series"]
    out.rename(tmp_path / ".pub.previous-.pub.staging-def456")
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert (out / "latest.json").read_bytes() == before
    assert _siblings(tmp_path) == ["pub", "series"]


def test_publish_cleanup_leaves_unrelated_siblings_untouched(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    contract = tmp_path / ".pub.previous-.pub.staging-ok"
    shutil.copytree(out, contract)
    unrelated = [
        ".pub.previousX",  # no dash: not the pattern
        ".pub.stagingX",
        ".other.staging-1",  # another --out's debris
        ".other.previous-1",
        "pub.staging-1",  # no leading dot
        ".pub.previous-foreign",  # old prefix, not the writer's shape
        ".pub.previous-.pub.staging-foreign",  # pattern, but not a contract
        ".pub.staging-foreign",  # pattern, but holds a foreign file
    ]
    for name in unrelated:
        shutil.copytree(contract, tmp_path / name)
        (tmp_path / name / "data.txt").write_text("keep me")
    target = tmp_path / "elsewhere"
    shutil.copytree(contract, target)
    (tmp_path / ".pub.previous-.pub.staging-link").symlink_to(
        target, target_is_directory=True
    )
    (tmp_path / ".pub.staging-link").symlink_to(target, target_is_directory=True)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert not contract.exists()  # the one real previous contract is removed
    for name in unrelated:
        assert (tmp_path / name / "data.txt").read_text() == "keep me", name
    assert (tmp_path / ".pub.previous-.pub.staging-link").is_symlink()
    assert (tmp_path / ".pub.staging-link").is_symlink()
    assert (target / publish.INDEX_FILE).is_file()
    assert _siblings(tmp_path) == sorted(
        [
            "pub",
            "series",
            "elsewhere",
            ".pub.previous-.pub.staging-link",
            ".pub.staging-link",
        ]
        + unrelated
    )


def test_publish_several_previous_without_out_refused_untouched(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    shutil.copytree(out, tmp_path / ".pub.previous-.pub.staging-a")
    out.rename(tmp_path / ".pub.previous-.pub.staging-b")
    (tmp_path / ".pub.staging-c").mkdir()
    before = _siblings(tmp_path)
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "several" in lines[0]
    assert _siblings(tmp_path) == before
    for name in (".pub.previous-.pub.staging-a", ".pub.previous-.pub.staging-b"):
        assert (tmp_path / name / publish.INDEX_FILE).is_file()


def test_publish_cleanup_ignores_debris_of_a_similarly_named_out(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    other = tmp_path / "a.staging-b"  # another --out whose name extends "a"
    assert _publish(other, "--run", RUN_ID).exit_code == 0
    # its previous contract and a stale staging dir of its own
    names = [
        ".a.staging-b.previous-.a.staging-b.staging-xyz",
        ".a.staging-b.staging-xyz",
    ]
    for name in names:
        shutil.copytree(other, tmp_path / name)
    out = tmp_path / "a"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    for name in names:
        assert (tmp_path / name / publish.INDEX_FILE).is_file(), name
    assert _siblings(tmp_path) == sorted(["a", "a.staging-b", "series", *names])


def test_publish_symlinked_out_recovers_from_crash_mid_swap(
    series_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(series_root)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real = real_parent / "pub"
    assert _publish(real, "--run", RUN_ID).exit_code == 0
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link = link_parent / "pub"
    link.symlink_to(Path("..") / "real" / "pub", target_is_directory=True)
    _crash_mid_swap(link, monkeypatch, real=real.resolve())
    assert link.is_symlink() and not link.exists()  # the link now dangles
    assert len(_debris(real_parent, "pub")) == 2
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert _index(link)["run_id"] == RUN_ID
    assert _siblings(real_parent) == ["pub"]
    assert _siblings(link_parent) == ["pub"]


def test_publish_dangling_out_link_with_several_previous_refused(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    real = tmp_path / "real" / "pub"
    real.parent.mkdir()
    assert _publish(real, "--run", RUN_ID).exit_code == 0
    shutil.copytree(real, real.parent / ".pub.previous-.pub.staging-a")
    real.rename(real.parent / ".pub.previous-.pub.staging-b")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    before = _siblings(real.parent)
    result = _publish(link, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    assert "several" in result.output
    assert link.is_symlink()
    assert _siblings(real.parent) == before


# --- schema versioning --------------------------------------------------------


def _schema(name: str) -> dict:
    return publish._validator(name).schema


@pytest.mark.parametrize("name", ["latest", "series", "publish"])
def test_publish_schema_version_pattern_major_matches_code(name: str) -> None:
    rule = _schema(name)["properties"]["schema_version"]
    assert rule["type"] == "string"
    major = publish.SCHEMA_VERSION.split(".")[0]
    assert rule["pattern"] == rf"^{major}\.[0-9]+\.[0-9]+$"
    assert rule["not"] == {"pattern": r"\n"}


@pytest.mark.parametrize("name", ["latest", "series", "publish"])
def test_publish_schema_accepts_later_minor_rejects_other_major(
    series_root: Path, tmp_path: Path, name: str
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    doc = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    for later in ("3.0.1", "3.7.0", "3.12.3"):
        publish.validate(name, {**doc, "schema_version": later})
    for other in (
        "2.1.0",
        "4.0.0",
        "3.0",
        "v3.0.0",
        "3.0.0-rc1",
        "3.0.0\n",
        "3.0.0\n\n",
        " 3.0.0",
        1,
    ):
        with pytest.raises(publish.ContractInvalid):
            publish.validate(name, {**doc, "schema_version": other})


def test_publish_writes_exactly_the_code_schema_version(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    for name in ("latest.json", "series.json", "publish.json"):
        doc = json.loads((out / name).read_text(encoding="utf-8"))
        assert doc["schema_version"] == publish.SCHEMA_VERSION, name


# --- latest/series per-scene schema parity --------------------------------------


def _resolve(node: object, defs: dict) -> object:
    """``node`` with every local ``#/$defs/<name>`` reference inlined."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _resolve(defs[ref.removeprefix("#/$defs/")], defs)
        return {key: _resolve(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(value, defs) for value in node]
    return node


def test_publish_series_non_poor_scene_matches_latest_definitions() -> None:
    latest = _schema("latest")
    series_schema = _schema("series")
    scene = series_schema["$defs"]["scene"]
    assert scene["if"] == {"properties": {"quality_light": {"const": "POOR"}}}
    non_poor = _resolve(scene["else"], series_schema["$defs"])
    top = _resolve(latest, latest["$defs"])
    fields = scene["required"]
    assert set(fields) <= set(latest["required"])
    assert set(non_poor["properties"]) == set(fields)
    for field in fields:
        assert non_poor["properties"][field] == top["properties"][field], field
    # the bed_applied conditional is the same in both
    (bed_rule,) = non_poor["allOf"]
    for key in ("if", "then", "else"):
        assert bed_rule[key] == top[key], key


def test_publish_latest_schema_rejects_poor_light(
    series_root: Path, tmp_path: Path
) -> None:
    doc = _valid_latest(series_root, tmp_path)
    doc["quality_light"] = "POOR"
    with pytest.raises(publish.ContractInvalid):
        publish.validate("latest", doc)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("hyacinth_corrected_ha", None),
        ("pct_reservoir_corrected", None),
        ("cover_band", None),
    ],
)
def test_publish_series_schema_bed_applied_requires_corrected(
    series_root: Path, tmp_path: Path, key: str, value: object
) -> None:
    doc = copy.deepcopy(_series_run(series_root, tmp_path))
    index = next(
        i
        for i, scene in enumerate(doc["scenes"])
        if scene["quality_light"] != "POOR" and scene["bed_applied"]
    )
    publish.validate("series", doc)
    _mutate_scene(doc, index, key, value)
    with pytest.raises(publish.ContractInvalid):
        publish.validate("series", doc)


# --- NaN, corrupt manifests -----------------------------------------------------


def test_publish_canonical_json_rejects_nan() -> None:
    for bad in (float("nan"), float("inf")):
        with pytest.raises(publish.ContractInvalid, match="not valid JSON"):
            publish.canonical_json({"scenes": [{"level_m": bad}]})


def test_publish_nan_in_document_exit_1_writes_nothing(
    series_root: Path, tmp_path: Path
) -> None:
    records = _default_records()
    records[0] = _record(
        "2024-01-01", "S2A_14QMH_20240101_0_L2A", "ok", level_m=float("nan")
    )
    _write_run(series_root, records=records)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 1, result.output
    assert result.output.startswith("error:")
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["series"]


def test_publish_default_run_skips_corrupt_manifest(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root, "20240101T000000", [_record("2024-01-01", "A", "ok")])
    broken = _write_run(
        series_root, "20240102T000000", [_record("2024-01-02", "B", "ok")]
    )
    text = (broken / "manifest.json").read_text()
    (broken / "manifest.json").write_text(text[: len(text) // 2])
    out = tmp_path / "pub"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    assert _latest(out)["item_id"] == "A"


@pytest.mark.parametrize("content", [b'{"run_id": "trunc', b"\xff\xfe", b"[1, 2]"])
def test_publish_explicit_run_corrupt_manifest_exit_2(
    series_root: Path, tmp_path: Path, content: bytes
) -> None:
    run_dir = _write_run(series_root)
    (run_dir / "manifest.json").write_bytes(content)
    out = tmp_path / "pub"
    result = _publish(out, "--run", RUN_ID)
    assert result.exit_code == 2, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "manifest" in lines[0]
    assert not out.exists()


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user (root ignores directory permissions)",
)
def test_publish_undeletable_previous_does_not_fail(
    series_root: Path, tmp_path: Path
) -> None:
    _write_run(series_root)
    out = tmp_path / "pub"
    assert _publish(out, "--run", RUN_ID).exit_code == 0
    (out / "map").chmod(0o555)  # its files cannot be unlinked
    try:
        result = _publish(out, "--run", RUN_ID)
        assert result.exit_code == 0, result.output
        assert (out / "publish.json").is_file()
    finally:
        for path in tmp_path.glob(".pub.previous-*/map"):
            path.chmod(0o755)


def test_publish_malformed_row_is_contract_invalid() -> None:
    record = _record("2024-01-01", "A", "ok")
    record["row"] = ["not", "a", "row"]
    manifest = {"run_id": RUN_ID, "counts": {"ok": 1, "skipped": 0, "errors": 0}}
    committed = publish.CommittedCurve(curve.load_curve(), "ab" * 32)
    with pytest.raises(publish.ContractInvalid, match="row of A"):
        publish.latest_document([record], manifest)
    with pytest.raises(publish.ContractInvalid, match="row of A"):
        publish.series_document([record], manifest, committed)


# --- map-name patterns ------------------------------------------------------------


def test_publish_map_patterns_agree_across_code_and_schemas() -> None:
    allowed = publish.ALLOWED_MAP_PATTERN.pattern
    suffixes = r"\.(?:tif|png)"
    assert allowed.endswith(suffixes)
    stem = allowed.removesuffix(suffixes)
    latest_map = _schema("latest")["properties"]["map"]["properties"]
    assert latest_map["geotiff"]["pattern"] == rf"^{stem}\.tif$"
    assert latest_map["png"]["pattern"] == rf"^{stem}\.png$"
    documents = sorted(publish.ALLOWED_FILES - {publish.INDEX_FILE})
    images = publish.ALLOWED_IMAGE_PATTERN.pattern
    index_path = _schema("publish")["properties"]["files"]["items"]["properties"]
    expected = "|".join([*(re.escape(name) for name in documents), allowed, images])
    assert index_path["path"]["pattern"] == f"^(?:{expected})$"
    unclipped = images.replace("(?:clipped/)?", "")
    image_schema = _schema("latest")["properties"]["image"]["properties"]
    assert image_schema["path"]["pattern"] == f"^{unclipped}$"


def test_publish_map_and_png_paths_share_one_stem() -> None:
    record = {"date": "2024-01-04", "item_id_used": "S2B_X"}
    assert publish.map_path(record) == "map/2024-01-04_S2B_X.tif"
    assert publish.png_path(record) == "map/2024-01-04_S2B_X.png"
    for path in (publish.map_path(record), publish.png_path(record)):
        assert publish.is_allowed(path)


# --- SMOLF_DATA_DIR: series build then publish under one data root -----------
_REAL_CACHE_DIR = paths.cache_dir  # scene_env replaces it with a tmp cache
_REAL_LEVELS_PATH = paths.levels_path  # levels_file replaces it with a tmp record


def _tree(root: Path, *, hashed: bool = False) -> set[tuple[str, str]]:
    """(relative name, sha256 when `hashed` and a file, else "") per entry."""
    return {
        (
            p.relative_to(root).as_posix(),
            _sha(p.read_bytes()) if hashed and p.is_file() else "",
        )
        for p in root.rglob("*")
    }


@pytest.fixture
def data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scene_env: SimpleNamespace
) -> Path:
    """`series build` runs with fakes; every default path comes from the env."""
    root = tmp_path / "data_root"
    root.mkdir()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(root))
    monkeypatch.setattr(paths, "cache_dir", _REAL_CACHE_DIR)
    records = {r["item_id_requested"]: r for r in _default_records()}
    targets = [
        series.SeriesTarget(date.fromisoformat(r["date"]), item_id)
        for item_id, r in records.items()
    ]

    def fake_process(target: series.SeriesTarget) -> dict:
        record = copy.deepcopy(records[target.item_id])
        if record["status"] == "ok":  # cache the scene; the record pins its sha256
            data = _cache_bytes(target.item_id)
            paths.cache_dir().mkdir(parents=True, exist_ok=True)
            (paths.cache_dir() / f"{target.item_id}.tif").write_bytes(data)
            record["cache_sha256"] = _sha(data)
        return record

    monkeypatch.setattr(series, "process_scene", fake_process)
    monkeypatch.setattr(series, "list_reference_targets", lambda: targets)
    monkeypatch.setattr(series, "last_day_with_level", lambda: targets[-1].day)
    monkeypatch.setattr(series, "DEFAULT_CURVE_CSV_PATH", scene_env.curve_path)
    monkeypatch.setattr(series, "DEFAULT_FLOOD_PATH", scene_env.flood_path)
    monkeypatch.setattr(
        series,
        "current_fingerprint",
        lambda: {"git_commit": "0" * 40, "dirty": False, "config_hash": "1" * 64},
    )
    return root


def test_data_dir_holds_cache_series_and_publish(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_outputs = _tree(PROJECT_ROOT / "outputs")
    repo_cache = _tree(PROJECT_ROOT / "data/cache")
    build = CliRunner().invoke(app, ["series", "build"])
    assert build.exit_code == 0, build.output
    result = CliRunner().invoke(app, ["publish", "--latest"])
    assert result.exit_code == 0, result.output
    assert len(list((data_root / "outputs/series").glob("*/series.csv"))) == 1
    assert (data_root / "cache" / f"{LATEST_ITEM}.tif").is_file()
    published = data_root / "outputs/publish"
    assert {"latest.json", "series.json", "publish.json"} <= {
        p.name for p in published.iterdir()
    }
    assert _tree(PROJECT_ROOT / "outputs") == repo_outputs
    assert _tree(PROJECT_ROOT / "data/cache") == repo_cache


def test_data_dir_levels_update_series_build_and_publish(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily job: refresh levels, build, publish; the repo stays untouched."""
    (tmp_path / "committed").mkdir()
    committed = _write_levels(
        tmp_path / "committed/ENDHD.csv",
        [("2024-01-01", "2009.0"), ("2024-01-02", "2009.1")],
    )
    refreshed = _write_levels(
        tmp_path / "download.csv",
        [
            ("2024-01-01", "2009.0"),
            ("2024-01-02", "2009.1"),
            ("2024-01-03", "2009.2"),
            ("2024-01-04", "2009.3"),
            ("2024-01-05", "2009.4"),
        ],
    ).read_bytes()
    monkeypatch.setattr(paths, "COMMITTED_LEVELS_PATH", committed)
    monkeypatch.setattr(paths, "levels_path", _REAL_LEVELS_PATH)
    monkeypatch.setattr(levels, "download_levels", lambda: refreshed)
    monkeypatch.setattr(series, "last_day_with_level", levels.last_day_with_level)

    def repo_state() -> dict[str, set[tuple[str, str]]]:
        # data/hydro is small and committed: compare bytes, not just names.
        return {
            rel: _tree(PROJECT_ROOT / rel, hashed=rel == "data/hydro")
            for rel in ("outputs", "data/cache", "data/hydro")
        }

    repo = repo_state()

    update = CliRunner().invoke(app, ["levels", "update"])
    assert update.exit_code == 0, update.output
    copy = data_root / "hydro/ENDHD.csv"
    assert copy.read_bytes() == refreshed
    assert (data_root / "hydro/ENDHD.csv.json").is_file()
    build = CliRunner().invoke(app, ["series", "build"])
    assert build.exit_code == 0, build.output
    result = CliRunner().invoke(app, ["publish", "--latest"])
    assert result.exit_code == 0, result.output

    (manifest_path,) = (data_root / "outputs/series").glob("*/manifest.json")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["inputs"]["ENDHD_csv"] == _sha(refreshed)
    latest = json.loads((data_root / "outputs/publish/latest.json").read_text())
    assert latest["current_level"]["date"] == "2024-01-05"
    assert latest["current_level"]["levels_sha256"] == _sha(refreshed)
    assert repo_state() == repo


def test_publish_out_wins_over_data_dir(data_root: Path, tmp_path: Path) -> None:
    assert CliRunner().invoke(app, ["series", "build"]).exit_code == 0
    out = tmp_path / "explicit"
    result = _publish(out)
    assert result.exit_code == 0, result.output
    assert (out / "latest.json").is_file()
    assert not (data_root / "outputs/publish").exists()


def test_publish_refusal_prints_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**_kwargs: object) -> Path:
        raise publish.PublishRefused("line one\n  line two\n")

    monkeypatch.setattr("smol_frontier.cli.publish_run", refuse)
    result = CliRunner().invoke(app, ["publish", "--latest"])
    assert result.exit_code == 2
    assert result.stderr == "refused: line one line two\n"


def test_publish_contract_invalid_prints_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid(**_kwargs: object) -> Path:
        raise publish.ContractInvalid("line one\n  line two\n")

    monkeypatch.setattr("smol_frontier.cli.publish_run", invalid)
    result = CliRunner().invoke(app, ["publish", "--latest"])
    assert result.exit_code == 1
    assert result.stderr == "error: line one line two\n"
