"""Real-data regression for `smolf publish` (AGENTS.md control values).

Each test builds a one-scene run directory in tmp_path from a committed
scene record (tests/fixtures/publish_regression/) and publishes it for
real: hash-verified cache read, re-classification, class-5 mask, schema
validation, atomic write. The records are real: the 2026-10-12 one is
copied verbatim from series run 20261030T031055 (Earth Search); the
2024-06-09 one is the Planetary Computer copy of the dry-season control
scene, the backend the control values in AGENTS.md were measured on.

Skips with a clear reason when the scene is not cached (CI) or the local
cache file is not the one the record was built from (a re-fetch): a cache
miss would otherwise trigger a network re-ingest. No network.
"""

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import rasterio

from smol_frontier import publish
from smol_frontier.config import settings
from smol_frontier.hydro import flood_map
from smol_frontier.ingest import DEFAULT_CACHE_DIR, _cache_path

FIXTURES = Path(__file__).parent / "fixtures" / "publish_regression"
SCENE_2026_10_12 = "S2B_14QMH_20261012_0_L2A"
SCENE_2024_06_09 = "S2A_MSIL2A_20240609T165901_R069_T14QMH_20240610T011221"

# WHY: the committed hydrology inputs the records were built with (run
#   20261030T031055's manifest inputs). Publish refuses if the committed
#   flood-stage map or stage-area curve drift from these.
RUN_INPUTS = {
    "flood_stage_tif": (
        "3c718d58220811c70a3c06d036db11b432b9465a17861db7d16dc81eaaa1c78c"
    ),
    "stage_area_curve_csv": (
        "31d20fef788ac7829c1642a1dea4674be5e58f5a13f88f2d5bc8553ef6ebe9c1"
    ),
}


def _publish_fixture(tmp_path: Path, item_id: str) -> tuple[dict, Path]:
    """Publish a one-scene run built from the committed record; skip if uncached."""
    source = FIXTURES / f"{item_id}.json"
    record = json.loads(source.read_text(encoding="utf-8"))
    cached = _cache_path(DEFAULT_CACHE_DIR, item_id)
    if not cached.exists():
        pytest.skip(f"scene {item_id} not in data/cache (CI): run `smolf fetch`")
    digest = hashlib.sha256(cached.read_bytes()).hexdigest()
    if digest != record["cache_sha256"]:
        pytest.skip(f"cached {item_id} is not the file the record was built from")
    run_id = f"regression-{record['date']}"
    run_dir = tmp_path / "series" / run_id
    (run_dir / "scenes").mkdir(parents=True)
    shutil.copy(source, run_dir / "scenes" / source.name)
    manifest = {
        "run_id": run_id,
        "status": "complete",
        "code": {"dirty": False, "git_commit": "0" * 40, "config_hash": "0" * 64},
        "counts": {"requested": 1, "ok": 1, "skipped": 0, "errors": 0},
        "inputs": RUN_INPUTS,
        # a fixture run has no series.csv; the record's own hash stands in
        "series_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out_dir = publish.publish_run(
        run_id=run_id, out_dir=tmp_path / "pub", series_root=tmp_path / "series"
    )
    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    return latest, out_dir


def _class5_ha(out_dir: Path, latest: dict) -> float:
    with rasterio.open(out_dir / latest["map"]["geotiff"]) as src:
        classes = src.read(1)
    removed = int(np.count_nonzero(classes == settings.CLASS_BED_REMOVED))
    return round(removed * flood_map.PIXEL_HA, 2)


def test_publish_regression_2026_10_12(tmp_path: Path) -> None:
    """High-water control: 818.34 ha, 64.86 %, GOOD, CLEAN, RED, nothing removed."""
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2026_10_12)
    assert latest["date"] == "2026-10-12"
    assert latest["hyacinth_raw_ha"] == 818.34
    assert latest["hyacinth_corrected_ha"] == 818.34
    assert latest["dense_corrected_ha"] == 350.24
    assert latest["interface_corrected_ha"] == 468.1
    assert latest["pct_reservoir_corrected"] == 64.86
    assert latest["quality_light"] == "GOOD"
    assert latest["regime"] == "CLEAN"
    assert latest["cover_band"] == "RED"
    assert latest["bed_applied"] is True
    assert latest["removed_ha"] == 0.0
    assert latest["reservoir_area_ha"] == 1261.75
    assert latest["smolf_version"] == "0.2.0"
    assert latest["schema_version"] == "1.0.0"
    assert _class5_ha(out_dir, latest) == 0.0


def test_publish_regression_2024_06_09(tmp_path: Path) -> None:
    """Dry-season control: raw 432.25 → corrected 13.74, class 5 = 418.51 ha."""
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2024_06_09)
    assert latest["date"] == "2024-06-09"
    assert latest["hyacinth_raw_ha"] == 432.25
    assert latest["hyacinth_corrected_ha"] == 13.74
    assert latest["removed_ha"] == 418.51
    assert latest["level_m"] == 2004.6
    assert latest["regime"] == "EXPOSED_BED"
    assert latest["quality_light"] == "GOOD"
    assert latest["bed_applied"] is True
    assert latest["pct_reservoir_corrected"] == 1.09
    assert latest["cover_band"] == "GREEN"
    assert _class5_ha(out_dir, latest) == 418.51
