"""Tests for the Sentinel-2 true-colour photo: stretch, guard, WebP writer
and reader (local COG stand-ins; the live test is opt-in, SMOLF_LIVE=1)."""

import os
import warnings
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import rasterize
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import mapping

from smol_frontier import aoi
from smol_frontier.classify.display import DISPLAY_CRS
from smol_frontier.config import settings
from smol_frontier.publish import truecolour
from smol_frontier.sources.stac import SceneMeta

S2 = "sentinel-2-l2a"
# WHY: the 2026-10-12 control scene on Earth Search (AGENTS.md).
LIVE_ITEM = "S2B_14QMH_20261012_0_L2A"


def _rgb(shape: tuple[int, int] = (4, 5), bands: int = 3) -> np.ndarray:
    return np.full((bands, *shape), 7, dtype=np.uint8)


def _aoi_grid(pixel_m: float) -> tuple[rasterio.Affine, tuple[int, int]]:
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", DISPLAY_CRS, *aoi.wgs84_bbox()
    )
    width = int(np.ceil((right - left) / pixel_m))
    height = int(np.ceil((top - bottom) / pixel_m))
    transform = rasterio.Affine(pixel_m, 0.0, left, 0.0, -pixel_m, top)
    return transform, (height, width)


def _reservoir(transform: rasterio.Affine, shape: tuple[int, int]) -> np.ndarray:
    geom = transform_geom(settings.UTM_CRS, DISPLAY_CRS, mapping(aoi.utm_geometry()))
    return rasterize([(geom, 1)], out_shape=shape, transform=transform).astype(bool)


def _read(path: Path) -> tuple[np.ndarray, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(path) as src:
            return src.read(), src.driver


def test_stretch_is_fixed_linear_window_without_gamma() -> None:
    reflectance = np.array([[[0.0, 0.15, 0.30, 0.45, -0.05, np.nan]]])
    assert settings.TRUE_COLOUR_MIN == 0.0
    assert settings.TRUE_COLOUR_MAX == 0.30
    stretched = truecolour.stretch(reflectance)
    assert stretched.dtype == np.uint8
    assert stretched.tolist() == [[[0, 128, 255, 255, 0, 0]]]


@pytest.mark.parametrize("collection", ["planetscope", "landsat-c2-l2", None])
def test_guard_refuses_non_sentinel2_collection(collection: str | None) -> None:
    with pytest.raises(ValueError, match="sentinel-2-l2a"):
        truecolour.guard_true_colour(_rgb(), collection)


def test_guard_refuses_float_array() -> None:
    with pytest.raises(ValueError, match="uint8"):
        truecolour.guard_true_colour(_rgb().astype(np.float32) / 255, S2)


@pytest.mark.parametrize("bands", [1, 2, 5])
def test_guard_refuses_wrong_band_count(bands: int) -> None:
    with pytest.raises(ValueError, match="3 or 4 bands"):
        truecolour.guard_true_colour(_rgb(bands=bands), S2)


def test_guard_refuses_a_2d_class_map() -> None:
    with pytest.raises(ValueError, match="3 or 4 bands"):
        truecolour.guard_true_colour(np.zeros((4, 5), dtype=np.uint8), S2)


@pytest.mark.parametrize("bands", [3, 4])
def test_guard_accepts_rgb_and_rgba(bands: int) -> None:
    array = _rgb(bands=bands)
    assert truecolour.guard_true_colour(array, S2) is array


def test_writer_refuses_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "x.webp"
    with pytest.raises(ValueError):
        truecolour.write_true_colour_webp(path, _rgb(), "planetscope")
    assert not path.exists()


@pytest.mark.parametrize("bands", [3, 4])
def test_writer_writes_webp_with_its_band_count(tmp_path: Path, bands: int) -> None:
    array = np.random.default_rng(0).integers(0, 256, (bands, 30, 40), dtype=np.uint8)
    if bands == 4:
        array[3] = 255
        array[3, :10] = 0
    path = truecolour.write_true_colour_webp(tmp_path / "x.webp", array, S2)
    decoded, driver = _read(path)
    assert driver == "WEBP"
    assert decoded.shape == (bands, 30, 40)
    assert path.read_bytes()[:4] == b"RIFF"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.webp"]
    if bands == 4:
        assert (decoded[3, :10] == 0).all()


def test_writer_is_byte_identical_across_runs(tmp_path: Path) -> None:
    array = np.random.default_rng(1).integers(0, 256, (3, 30, 40), dtype=np.uint8)
    first = truecolour.write_true_colour_webp(tmp_path / "a.webp", array, S2)
    second = truecolour.write_true_colour_webp(tmp_path / "b.webp", array, S2)
    assert first.read_bytes() == second.read_bytes()


def test_image_path_and_attribution_follow_the_scene() -> None:
    record = {"date": "2024-06-09", "item_id_used": "S2A_14QMH_20240609_0_L2A"}
    assert (
        truecolour.image_path(record)
        == "image/2024-06-09_S2A_14QMH_20240609_0_L2A.webp"
    )
    assert truecolour.attribution("2024-06-09") == "Copernicus Sentinel data 2024"
    scene = {"date": "2024-06-09", "item_id": "S2A_14QMH_20240609_0_L2A"}
    assert (
        truecolour.clipped_image_path(scene)
        == "image/clipped/2024-06-09_S2A_14QMH_20240609_0_L2A.webp"
    )


def _write_band(path: Path, value: int | Callable) -> str:
    """A UTM band over the AOI bbox (+200 m): constant DN, or ``value(transform,
    shape)`` for a DN array placed in space."""
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", settings.UTM_CRS, *aoi.wgs84_bbox()
    )
    res = 20.0
    left, top = left - 200, top + 200
    width = int((right + 200 - left) / res)
    height = int((top - (bottom - 200)) / res)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint16",
        crs=settings.UTM_CRS,
        transform=rasterio.Affine(res, 0.0, left, 0.0, -res, top),
        nodata=0,
    ) as dst:
        dn = (
            value(dst.transform, (height, width))
            if callable(value)
            else np.full((height, width), value)
        )
        dst.write(dn.astype(np.uint16), 1)
    return str(path)


def _fake_scene(tmp_path: Path, dn: dict[str, int | Callable], monkeypatch) -> list:
    calls = []
    assets = {band: _write_band(tmp_path / f"{band}.tif", v) for band, v in dn.items()}

    def fake_get_scene(backend: str, item_id: str) -> SceneMeta:
        calls.append((backend, item_id))
        return SceneMeta(
            id=item_id,
            datetime=datetime(2024, 6, 9),
            source=backend,
            processing_baseline="05.10",
            boa_offset_applied=False,
            assets=assets,
            collection=S2,
        )

    monkeypatch.setattr(truecolour, "get_scene", fake_get_scene)
    return calls


def test_reader_applies_one_b02_offset_to_every_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_scene(tmp_path, {"B04": 3000, "B03": 2500, "B02": 2000}, monkeypatch)
    transform, shape = _aoi_grid(40.0)
    photo = truecolour.read_true_colour("earth-search", "ITEM", transform, shape)
    assert calls == [("earth-search", "ITEM")]
    assert photo.collection == S2
    assert photo.reflectance.shape == (3, *shape)
    # baseline >= 04.00 and not flagged applied: -0.1 on every band
    np.testing.assert_allclose(
        [np.nanmean(band) for band in photo.reflectance], [0.2, 0.15, 0.1]
    )
    assert not np.isnan(photo.reflectance).any()


def test_reader_guard_on_b02_drops_offset_for_every_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scene(tmp_path, {"B04": 3000, "B03": 2500, "B02": 500}, monkeypatch)
    transform, shape = _aoi_grid(40.0)
    photo = truecolour.read_true_colour("earth-search", "ITEM", transform, shape)
    np.testing.assert_allclose(
        [np.nanmean(band) for band in photo.reflectance], [0.3, 0.25, 0.05]
    )


@pytest.mark.skipif(
    os.environ.get("SMOLF_LIVE") != "1",
    reason="opt-in live Earth Search test (SMOLF_LIVE=1)",
)
def test_live_true_colour_of_control_scene() -> None:
    transform, shape = _aoi_grid(10.0)
    photo = truecolour.read_true_colour("earth-search", LIVE_ITEM, transform, shape)
    assert photo.collection == S2
    assert photo.reflectance.shape == (3, *shape)
    rgb = truecolour.stretch(photo.reflectance)
    inside = _reservoir(transform, shape)
    interior = rgb[:, inside]
    print(f"\nSMOLF_LIVE: {shape}, interior mean RGB {interior.mean(axis=1)}")
    assert (interior.sum(axis=0) > 0).mean() > 0.99
    assert interior.mean() > 5


def _utm_reservoir(transform: rasterio.Affine, shape: tuple[int, int]) -> np.ndarray:
    geom = mapping(aoi.utm_geometry())
    return rasterize([(geom, 1)], out_shape=shape, transform=transform).astype(bool)


def test_reader_derives_the_offset_from_b02_inside_the_reservoir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def b02(transform: rasterio.Affine, shape: tuple[int, int]) -> np.ndarray:
        return np.where(_utm_reservoir(transform, shape), 2000, 500)

    _fake_scene(tmp_path, {"B04": 3000, "B03": 2500, "B02": b02}, monkeypatch)
    transform, shape = _aoi_grid(40.0)
    photo = truecolour.read_true_colour("earth-search", "ITEM", transform, shape)
    # 500 DN covers most of the grid but lies outside the reservoir, so the
    # -0.1 offset is kept, as ingest keeps it for the same scene
    np.testing.assert_allclose(np.nanmean(photo.reflectance[0]), 0.2)


def test_reader_places_pixels_where_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x, y = aoi.utm_geometry().centroid.coords[0]

    def bright_spot(transform: rasterio.Affine, shape: tuple[int, int]) -> np.ndarray:
        dn = np.full(shape, 2000)
        col, row = ~transform @ (x, y)
        dn[int(row) - 2 : int(row) + 3, int(col) - 2 : int(col) + 3] = 9000
        return dn

    _fake_scene(tmp_path, {"B04": bright_spot, "B03": 2000, "B02": 2000}, monkeypatch)
    transform, shape = _aoi_grid(20.0)
    photo = truecolour.read_true_colour("earth-search", "ITEM", transform, shape)
    row, col = np.unravel_index(np.nanargmax(photo.reflectance[0]), shape)
    found = transform @ (col + 0.5, row + 0.5)
    expected = transform_bounds(settings.UTM_CRS, DISPLAY_CRS, x, y, x, y)[:2]
    assert np.hypot(found[0] - expected[0], found[1] - expected[1]) < 3 * 20.0
    # bilinear: the spot's edge ramps through intermediate values
    red = photo.reflectance[0]
    assert ((red > 0.11) & (red < 0.79)).any()
