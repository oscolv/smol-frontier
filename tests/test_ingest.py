"""Tests for scene ingest (synthetic rasters, no network)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio import Affine
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import box
from typer.testing import CliRunner

from smol_frontier.cli import app
from smol_frontier.ingest import BAND_ORDER, IngestedScene, ingest_scene
from smol_frontier.sources.radiometry import dn_to_reflectance
from smol_frontier.sources.stac import SceneMeta

CRS = "EPSG:32614"
T10 = from_origin(440000, 2226000, 10, 10)
T20 = from_origin(440000, 2226000, 20, 20)


def _write(path: Path, array: np.ndarray, transform: Affine) -> str:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype=array.dtype,
        crs=CRS,
        transform=transform,
    ) as dst:
        dst.write(array, 1)
    return str(path)


@pytest.fixture()
def synthetic(tmp_path: Path):
    """20x20 (10 m) + 10x10 (20 m) sources; ingest window is the central 6x6."""
    hrefs = {}
    rows, cols = np.mgrid[0:20, 0:20]
    for band, base in (("B02", 1500), ("B03", 1200), ("B04", 1100), ("B08", 2500)):
        hrefs[band] = _write(
            tmp_path / f"{band}.tif",
            (base + (rows + cols) % 500).astype(np.uint16),
            T10,
        )
    step = np.where(np.mgrid[0:10, 0:10][1] < 5, 1000, 3000).astype(np.uint16)
    hrefs["B8A"] = _write(tmp_path / "B8A.tif", step, T20)
    hrefs["B11"] = _write(tmp_path / "B11.tif", np.full((10, 10), 1800, np.uint16), T20)
    scl = np.where(np.mgrid[0:10, 0:10][0] < 5, 4, 5).astype(np.uint8)
    hrefs["SCL"] = _write(tmp_path / "SCL.tif", scl, T20)
    scene = SceneMeta(
        id="TEST_SCENE",
        datetime=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        source="test",
        processing_baseline="05.11",
        boa_offset_applied=False,
        assets=hrefs,
    )
    # Window E 440070-440130 / N 2225870-2225930, inset 1 m for float safety.
    bbox = transform_bounds(CRS, "EPSG:4326", 440071, 2225871, 440129, 2225929)
    aoi_geom = box(440070, 2225870, 440100, 2225930)  # output cols 0-2
    cache_dir = tmp_path / "cache"
    return scene, bbox, aoi_geom, cache_dir


def test_ingest_grid_shapes_and_dtypes(synthetic) -> None:
    scene, bbox, aoi_geom, cache_dir = synthetic
    ing = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    assert tuple(ing.bands) == BAND_ORDER
    for band in BAND_ORDER:
        assert ing.bands[band].shape == (6, 6)
        assert ing.bands[band].dtype == np.uint16
    assert ing.crs == CRS
    assert ing.transform == Affine(10, 0, 440070, 0, -10, 2225930)
    assert ing.inside.shape == (6, 6)
    assert ing.inside[:, :3].all()
    assert not ing.inside[:, 3:].any()


def test_outside_polygon_masked_to_nodata(synthetic) -> None:
    scene, bbox, aoi_geom, cache_dir = synthetic
    ing = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    for band in BAND_ORDER:
        assert (ing.bands[band][:, 3:] == 0).all()
    assert (ing.bands["B02"][:, :3] > 0).all()


def test_resampling_modes(synthetic) -> None:
    scene, bbox, aoi_geom, cache_dir = synthetic
    ing = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    assert (ing.bands["B11"][:, :3] == 1800).all()  # bilinear keeps constants
    b8a = set(np.unique(ing.bands["B8A"][:, :3]))
    assert 1000 in b8a and len(b8a) > 1  # bilinear interpolates the step
    assert set(np.unique(ing.bands["SCL"][:, :3])) == {4, 5}  # nearest: values only


def test_single_offset_for_all_bands(synthetic) -> None:
    scene, bbox, aoi_geom, cache_dir = synthetic
    ing = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    assert ing.offset == pytest.approx(-0.1)
    for band in ("B02", "B03", "B04", "B08", "B8A", "B11"):
        actual = ing.reflectance(band)
        expected = dn_to_reflectance(ing.bands[band], ing.offset)
        assert np.allclose(actual, expected, equal_nan=True)


def test_cache_roundtrip_identical(synthetic) -> None:
    scene, bbox, aoi_geom, cache_dir = synthetic
    miss = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    with rasterio.open(cache_dir / "TEST_SCENE.tif") as cached:
        assert "offset" not in cached.tags()
    for path in scene.assets.values():  # prove the hit never touches sources
        Path(path).unlink()
    hit = ingest_scene(scene, bbox=bbox, aoi_geom=aoi_geom, cache_dir=cache_dir)
    assert hit.scene == miss.scene
    for band in BAND_ORDER:
        assert np.array_equal(hit.bands[band], miss.bands[band])
    assert np.array_equal(hit.inside, miss.inside)
    assert hit.transform == miss.transform
    assert hit.crs == miss.crs
    assert hit.offset == miss.offset


def _fake_ingested() -> IngestedScene:
    scene = SceneMeta(
        id="S2_FAKE",
        datetime=datetime(2026, 10, 12, 17, 0, tzinfo=UTC),
        source="earth-search",
        processing_baseline="05.11",
        boa_offset_applied=False,
        assets={},
    )
    bands = dict.fromkeys(BAND_ORDER, np.zeros((2, 2), dtype=np.uint16))
    bands["B02"] = np.array([[1500, 2000], [1000, 0]], dtype=np.uint16)
    bands["B03"] = np.array([[1500, 2000], [1000, 3000]], dtype=np.uint16)
    bands["SCL"] = np.array([[6, 6], [6, 4]], dtype=np.uint16)
    inside = np.array([[True, True], [True, False]])
    return IngestedScene(scene, bands, inside, Affine.identity(), CRS, -0.1)


def test_cli_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    ing = _fake_ingested()
    monkeypatch.setattr("smol_frontier.cli.search_scenes", lambda *a, **k: [ing.scene])
    monkeypatch.setattr("smol_frontier.cli.ingest_scene", lambda scene: ing)
    result = CliRunner().invoke(app, ["fetch", "2026-10-12"])
    assert result.exit_code == 0, result.output
    assert "scene: S2_FAKE" in result.output
    assert "backend: earth-search" in result.output
    assert "baseline: 05.11" in result.output
    assert "offset: -0.1" in result.output
    assert "valid_fraction: 1.0000" in result.output
    assert "median_b03_water: 0.0500" in result.output


def test_cli_fetch_no_scenes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("smol_frontier.cli.search_scenes", lambda *a, **k: [])
    result = CliRunner().invoke(app, ["fetch", "2026-10-12"])
    assert result.exit_code == 1
    assert "no scenes found" in result.output


def test_cli_fetch_bad_date() -> None:
    result = CliRunner().invoke(app, ["fetch", "not-a-date"])
    assert result.exit_code != 0


def test_cli_fetch_uses_real_cache_dir(synthetic, monkeypatch) -> None:
    """End-to-end wiring: search -> ingest -> cache file on disk."""
    scene, bbox, aoi_geom, cache_dir = synthetic
    import smol_frontier.ingest as ingest_mod

    monkeypatch.setattr("smol_frontier.cli.search_scenes", lambda *a, **k: [scene])
    monkeypatch.setattr(ingest_mod, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr("smol_frontier.ingest.wgs84_bbox", lambda: bbox)
    monkeypatch.setattr("smol_frontier.ingest.utm_geometry", lambda: aoi_geom)
    result = CliRunner().invoke(app, ["fetch", "2026-10-12"])
    assert result.exit_code == 0, result.output
    assert (cache_dir / "TEST_SCENE.tif").exists()
    shutil.rmtree(cache_dir, ignore_errors=True)
