"""Real-data regression for `smolf export maps` (AGENTS.md control values).

Builds a two-scene run from the committed publish-regression records (the
Earth Search 2024-06-09 dry-season scene and the 2026-10-12 high-water
scene), publishes it for real, then exports its maps for real: the scene
cache is read and hash-checked, each scene re-classified and checked against
the run and against series.json. The photos are faked (no network).

Skips when a scene is not cached (CI) or the local cache file is not the
one the record was built from, as test_publish_regression does.
"""

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import rasterio

from smol_frontier import paths, publish, series
from smol_frontier.config import settings
from smol_frontier.hydro import flood_map
from smol_frontier.ingest import cache_path
from smol_frontier.publish import truecolour
from smol_frontier.publish.map_export import export_maps

FIXTURES = Path(__file__).parent / "fixtures" / "publish_regression"
SCENES = ("S2A_14QMH_20240609_0_L2A", "S2B_14QMH_20261012_0_L2A")
# WHY: the hydrology inputs the records were built with (as in
#   test_publish_regression); build_map refuses if the committed files drift.
RUN_INPUTS = {
    "flood_stage_tif": (
        "3c718d58220811c70a3c06d036db11b432b9465a17861db7d16dc81eaaa1c78c"
    ),
    "stage_area_curve_csv": (
        "31d20fef788ac7829c1642a1dea4674be5e58f5a13f88f2d5bc8553ef6ebe9c1"
    ),
}


@pytest.fixture(autouse=True)
def offline_true_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(
        backend: str, item_id: str, transform: rasterio.Affine, shape: tuple
    ) -> truecolour.TrueColour:
        return truecolour.TrueColour(np.full((3, *shape), 0.1), truecolour.COLLECTION)

    monkeypatch.setattr(truecolour, "read_true_colour", fake)


def _records() -> list[dict]:
    """The committed records; skip unless every scene is cached as recorded."""
    records = []
    for item_id in SCENES:
        record = json.loads((FIXTURES / f"{item_id}.json").read_text("utf-8"))
        cached = cache_path(paths.cache_dir(), item_id)
        if not cached.exists():
            pytest.skip(f"scene {item_id} not in data/cache (CI): run `smolf fetch`")
        if hashlib.sha256(cached.read_bytes()).hexdigest() != record["cache_sha256"]:
            pytest.skip(f"cached {item_id} is not the file the record was built from")
        records.append(record)
    return records


@pytest.fixture
def exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real publish of the two-scene run, and its real map export."""
    records = _records()
    run_id = "regression-maps"
    series_root = tmp_path / "series"
    run_dir = series_root / run_id
    (run_dir / "scenes").mkdir(parents=True)
    for record in records:
        name = f"{record['item_id_requested']}.json"
        shutil.copy(FIXTURES / name, run_dir / "scenes" / name)
    series.write_series_csv(run_dir / "series.csv", records)
    manifest = {
        "run_id": run_id,
        "status": "complete",
        "code": {"dirty": False, "git_commit": "0" * 40, "config_hash": "0" * 64},
        "counts": {"requested": 2, "ok": 2, "skipped": 0, "errors": 0},
        "inputs": RUN_INPUTS,
        "series_sha256": series.sha256_file(run_dir / "series.csv"),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pub = publish.publish_run(
        run_id=run_id, out_dir=tmp_path / "pub", series_root=series_root
    )
    # WHY only series_dir: the cache must stay the real one.
    monkeypatch.setattr(paths, "series_dir", lambda: series_root)
    return export_maps(from_dir=pub, out_dir=tmp_path / "maps")


def _hectares(path: Path) -> list[float]:
    with rasterio.open(path) as src:
        counts = np.bincount(src.read(1).ravel(), minlength=6)
    return [round(int(n) * flood_map.PIXEL_HA, 2) for n in counts]


def test_export_maps_regression_dry_season_2024_06_09(exported: Path) -> None:
    """Earth Search control: 434.35 raw -> 15.66 corrected, 418.69 removed."""
    ha = _hectares(exported / "2024-06-09_S2A_14QMH_20240609_0_L2A.tif")
    dense, interface = ha[settings.CLASS_DENSE_HYACINTH], ha[settings.CLASS_INTERFACE]
    assert round(dense + interface, 2) == 15.66
    assert ha[settings.CLASS_BED_REMOVED] == 418.69
    assert round(dense + interface + ha[settings.CLASS_BED_REMOVED], 2) == 434.35


def test_export_maps_regression_high_water_2026_10_12(exported: Path) -> None:
    """High-water control: 824.63 ha (dense 350.24, interface 474.39), none removed."""
    ha = _hectares(exported / "2026-10-12_S2B_14QMH_20261012_0_L2A.tif")
    assert ha[settings.CLASS_DENSE_HYACINTH] == 350.24
    assert ha[settings.CLASS_INTERFACE] == 474.39
    assert ha[settings.CLASS_BED_REMOVED] == 0.0


def test_export_maps_regression_latest_map_is_the_published_map(
    exported: Path, tmp_path: Path
) -> None:
    published = tmp_path / "pub" / "map" / "2026-10-12_S2B_14QMH_20261012_0_L2A.tif"
    archived = exported / "2026-10-12_S2B_14QMH_20261012_0_L2A.tif"
    assert archived.read_bytes() == published.read_bytes()
