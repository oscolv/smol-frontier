"""Real-data regression for `smolf publish` (AGENTS.md control values).

Each test builds a one-scene run directory in tmp_path from a committed
scene record (tests/fixtures/publish_regression/) and publishes it for
real: hash-verified cache read, re-classification, class-5 mask, schema
validation, atomic write. Provenance of the two records differs: the
2026-10-12 one comes from a real series run (copied verbatim from run
20261030T031055, Earth Search); the 2024-06-09 one was generated once from
the cached Planetary Computer copy of the dry-season control scene with
the pipeline's own code (no series run covers it), so its values are the
AGENTS.md control values.

Skips with a clear reason when the scene is not cached (CI) or the local
cache file is not the one the record was built from (a re-fetch): a cache
miss would otherwise trigger a network re-ingest. No network.
"""

import hashlib
import json
import os
import shutil
import warnings
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.warp import transform_bounds

from smol_frontier import aoi, paths, publish
from smol_frontier.classify.display import DISPLAY_CRS
from smol_frontier.config import settings
from smol_frontier.hydro import flood_map
from smol_frontier.ingest import cache_path
from smol_frontier.publish import truecolour

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

# WHY: the display PNG covers the scene grid, not the AOI polygon, so it
#   overshoots the AOI bbox: the UTM envelope of the geographic bbox (grid
#   convergence), the 10 m snapping of the ingest window and the EPSG:3857
#   envelope of the UTM grid add 2-3 display pixels per side today.
# HOW: measured on the 2026-10-12 map (18.9-33.7 m against a 10.69 m display
#   pixel); 4 px leaves one pixel of headroom without hiding a real shift.
AOI_BBOX_TOLERANCE_PX = 4

# WHY: the real reader, kept for the opt-in live test (the fixture below
#   replaces it for every other test).
_REAL_READER = truecolour.read_true_colour


@pytest.fixture(autouse=True)
def offline_true_colour(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake(
        backend: str, item_id: str, transform: rasterio.Affine, shape: tuple
    ) -> truecolour.TrueColour:
        calls.append((backend, item_id, shape))
        return truecolour.TrueColour(np.full((3, *shape), 0.1), truecolour.COLLECTION)

    monkeypatch.setattr(truecolour, "read_true_colour", fake)
    return calls


def _publish_fixture(tmp_path: Path, item_id: str) -> tuple[dict, Path]:
    """Publish a one-scene run built from the committed record; skip if uncached."""
    source = FIXTURES / f"{item_id}.json"
    record = json.loads(source.read_text(encoding="utf-8"))
    cached = cache_path(paths.cache_dir(), item_id)
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
    assert latest["display_status"] == "FULL"
    assert latest["display_ha"] == {"total": 818, "dense": 350, "interface": 468}
    assert latest["bed_applied"] is True
    assert latest["removed_ha"] == 0.0
    assert latest["reservoir_area_ha"] == 1261.75
    assert latest["constants"]["green_max_ha"] == 378.53
    assert latest["constants"]["red_min_ha"] == 630.88
    assert latest["smolf_version"] == "0.5.0"
    assert latest["schema_version"] == "3.0.0"
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
    assert latest["display_status"] == "FULL"
    assert latest["display_ha"] == {"total": 14, "dense": 4, "interface": 10}
    assert _class5_ha(out_dir, latest) == 418.51


def test_publish_regression_png_bounds_enclose_aoi_bbox(tmp_path: Path) -> None:
    """The PNG extent contains the AOI bbox and overshoots it by <= 4 px."""
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2026_10_12)
    (south, west), (north, east) = latest["map"]["bounds"]
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", DISPLAY_CRS, west, south, east, north
    )
    aoi_left, aoi_bottom, aoi_right, aoi_top = transform_bounds(
        "EPSG:4326", DISPLAY_CRS, *aoi.wgs84_bbox()
    )
    # the PNG is deliberately not georeferenced (bounds live in latest.json)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(out_dir / latest["map"]["png"]) as src:
            height, width = src.shape
    pixel_x = (right - left) / width
    pixel_y = (top - bottom) / height
    excess_px = {
        "left": (aoi_left - left) / pixel_x,
        "bottom": (aoi_bottom - bottom) / pixel_y,
        "right": (right - aoi_right) / pixel_x,
        "top": (top - aoi_top) / pixel_y,
    }
    assert all(0 <= px <= AOI_BBOX_TOLERANCE_PX for px in excess_px.values()), excess_px


def _image_shape(out_dir: Path, rel: str) -> tuple[int, int, int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(out_dir / rel) as src:
            return (src.count, *src.shape)


def test_publish_regression_image_on_the_png_grid(
    tmp_path: Path, offline_true_colour: list[tuple]
) -> None:
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2026_10_12)
    png = _image_shape(out_dir, latest["map"]["png"])
    assert _image_shape(out_dir, latest["image"]["path"]) == (3, *png[1:])
    assert latest["image"]["bounds"] == latest["map"]["bounds"]
    assert latest["image"]["attribution"] == "Copernicus Sentinel data 2026"
    assert offline_true_colour == [("earth-search", SCENE_2026_10_12, png[1:])]


def test_publish_regression_same_month_clipped_to_the_reservoir(
    tmp_path: Path,
) -> None:
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2026_10_12)
    [year] = latest["same_month"]["years"]
    assert latest["same_month"]["month"] == 10
    assert (year["year"], year["n_scenes"]) == (2026, 1)
    assert year["scene"]["item_id"] == SCENE_2026_10_12
    png = _image_shape(out_dir, latest["map"]["png"])
    assert _image_shape(out_dir, year["image"]["path"]) == (4, *png[1:])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(out_dir / year["image"]["path"]) as src:
            alpha = src.read(4)
    # opaque inside the reservoir only: the photo's frame is the scene grid,
    #   so a margin around the polygon is transparent
    opaque = np.count_nonzero(alpha == 255) / alpha.size
    assert set(np.unique(alpha)) == {0, 255}
    assert 0.2 < opaque < 0.9


@pytest.mark.skipif(
    os.environ.get("SMOLF_LIVE") != "1",
    reason="opt-in live Earth Search test (SMOLF_LIVE=1)",
)
def test_publish_regression_live_image_well_under_300_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(truecolour, "read_true_colour", _REAL_READER)
    latest, out_dir = _publish_fixture(tmp_path, SCENE_2026_10_12)
    size = (out_dir / latest["image"]["path"]).stat().st_size
    print(f"\nSMOLF_LIVE: {latest['image']['path']} {size} bytes")
    assert size < 300_000
