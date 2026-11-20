"""Tests for the exposed-bed correction (synthetic arrays; files mocked)."""

from datetime import date

import numpy as np
import pytest
import rasterio

import smol_frontier.hydro.bed as bed_module
from smol_frontier.config import settings
from smol_frontier.hydro.bed import (
    bed_margin,
    compute_bed_correction,
    correct_bed,
    cushion_ha,
    day_mirror,
    exposed_bed_mask,
    load_day_mirror,
)
from smol_frontier.hydro.curve import StageAreaCurve
from smol_frontier.hydro.flood_map import FLOOD_NODATA

DENSE = settings.CLASS_DENSE_HYACINTH
INTERFACE = settings.CLASS_INTERFACE
WATER = settings.CLASS_WATER


def test_full_reservoir_removes_nothing() -> None:
    class_map = np.array([[DENSE, WATER], [INTERFACE, DENSE]])
    flood = np.array([[2000.0, 2001.0], [2002.0, 2003.0]])
    out = compute_bed_correction(
        class_map,
        flood,
        2010.0,
        [2000.0, 2001.0],
        [1.0, 2.0],
        [1.0, 2.0],
        [1.0, 2.0],
        100.0,
    )
    assert out.applied is True
    assert out.removed_ha == 0.0
    assert out.hyacinth_corrected_ha == pytest.approx(0.03)
    assert out.expected_bed_ha == 0.0
    assert out.margin_m is not None and out.margin_m >= 2010.0


def test_low_level_removes_safe_bed_keeps_band() -> None:
    class_map = np.array([[DENSE, INTERFACE, DENSE, INTERFACE]])
    flood = np.array([[2000.0, 2001.0, 2002.0, 2003.0]])
    out = compute_bed_correction(
        class_map,
        flood,
        2000.5,
        [2000.0, 2001.0, 2002.0, 2003.0],
        [0.1, 0.2, 0.9, 1.0],
        [0.1, 0.2, 0.9, 1.0],
        [0.1, 0.2, 0.9, 1.0],
        100.0,
    )
    # cushion = max(29 [model clip below 2005], 0) = 29;
    # area(2000.5) = 15, so margin = stage(44) = 2001 + 24/70.
    assert out.margin_m == pytest.approx(2001 + 24 / 70)
    assert out.expected_bed_ha == pytest.approx(0.03)
    assert out.removed_ha == pytest.approx(0.02)
    assert out.hyacinth_corrected_ha == pytest.approx(0.02)
    assert out.dense_corrected_ha == pytest.approx(0.01)
    assert out.interface_corrected_ha == pytest.approx(0.01)
    assert out.hyacinth_corrected_ha + out.removed_ha == pytest.approx(0.04)
    assert out.pct_corrected == pytest.approx(0.02)
    assert out.level_m == 2000.5


@pytest.mark.parametrize("level", [1999.0, 2000.5, 2005.0])
def test_margin_never_below_level(level: float) -> None:
    out = compute_bed_correction(
        np.array([[DENSE, INTERFACE]]),
        np.array([[2000.0, 2001.0]]),
        level,
        [2000.0, 2001.0],
        [1.0, 2.0],
        [1.0, 2.0],
        [1.0, 2.0],
        100.0,
    )
    assert out.margin_m is not None and out.margin_m >= level


def test_cushion_takes_max_of_model_and_bootstrap() -> None:
    stage = np.array([2000.0, 2010.0, 2020.0])
    ones = np.array([1.0, 1.0, 1.0])
    assert cushion_ha(2000.0, stage, ones, ones) == 29.0
    assert cushion_ha(2010.0, stage, ones, np.array([1.0, 3.0, 1.0])) == pytest.approx(
        100.0
    )


def test_missing_level_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bed_module, "level_on", lambda *a, **k: None)
    out = correct_bed(
        np.zeros((2, 2), np.uint8),
        rasterio.Affine.identity(),
        "EPSG:32614",
        date(2020, 1, 1),
    )
    assert out.applied is False
    assert "no SIH level" in out.reason
    assert out.level_m is None


@pytest.mark.parametrize("aspect", ["shape", "transform", "crs"])
def test_grid_mismatch_not_applied(
    monkeypatch: pytest.MonkeyPatch, aspect: str
) -> None:
    monkeypatch.setattr(bed_module, "level_on", lambda *a, **k: 2000.5)
    flood = np.full((5, 5), 2000.0, dtype=np.float32)
    monkeypatch.setattr(
        bed_module,
        "load_flood_map",
        lambda *a, **k: (flood, rasterio.Affine.identity(), "EPSG:32614"),
    )
    monkeypatch.setattr(bed_module, "load_curve", lambda *a, **k: _fake_curve())
    transform, crs, shape = rasterio.Affine.identity(), "EPSG:32614", (5, 5)
    if aspect == "shape":
        shape = (3, 3)
    if aspect == "transform":
        transform = rasterio.Affine.translation(10.0, 0.0)
    if aspect == "crs":
        crs = "EPSG:32613"
    out = correct_bed(np.zeros(shape, np.uint8), transform, crs, date(2020, 1, 1))
    assert out.applied is False
    assert aspect in out.reason
    assert out.level_m == 2000.5


def _fake_curve() -> StageAreaCurve:
    zeros = np.zeros(2)
    return StageAreaCurve(
        stage_m=np.array([2000.0, 2001.0]),
        volume_hm3=zeros,
        area_km2=np.array([1.0, 2.0]),
        area_p2_5=np.array([1.0, 2.0]),
        area_p97_5=np.array([1.0, 2.0]),
    )


def test_wrapper_applies_and_rounds_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bed_module, "level_on", lambda *a, **k: 2000.5)
    flood = np.array([[2000.0, 2001.0, 2002.0, 2003.0]], dtype=np.float32)
    monkeypatch.setattr(
        bed_module,
        "load_flood_map",
        lambda *a, **k: (flood, rasterio.Affine.identity(), "EPSG:32614"),
    )
    monkeypatch.setattr(
        bed_module,
        "load_curve",
        lambda *a, **k: StageAreaCurve(
            stage_m=np.array([2000.0, 2001.0, 2002.0, 2003.0]),
            volume_hm3=np.zeros(4),
            area_km2=np.array([0.1, 0.2, 0.9, 1.0]),
            area_p2_5=np.array([0.1, 0.2, 0.9, 1.0]),
            area_p97_5=np.array([0.1, 0.2, 0.9, 1.0]),
        ),
    )
    monkeypatch.setattr(bed_module, "reservoir_area_ha", lambda: 100.0)
    out = correct_bed(
        np.array([[DENSE, INTERFACE, DENSE, INTERFACE]], dtype=np.uint8),
        rasterio.Affine.identity(),
        "EPSG:32614",
        date(2020, 1, 1),
    )
    assert out.applied is True
    assert out.margin_m == 2001.34  # round(2001.342857..., 2)
    assert out.pct_corrected == pytest.approx(0.02)
    assert out.removed_ha == pytest.approx(0.02)


@pytest.mark.parametrize("which", ["flood", "curve"])
def test_missing_file_not_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path, which: str
) -> None:
    monkeypatch.setattr(bed_module, "level_on", lambda *a, **k: 2000.5)
    if which == "flood":
        monkeypatch.setattr(bed_module, "DEFAULT_FLOOD_PATH", tmp_path / "nope.tif")
    else:
        monkeypatch.setattr(bed_module, "DEFAULT_CURVE_CSV_PATH", tmp_path / "nope.csv")
    out = correct_bed(
        np.zeros((2, 2), np.uint8),
        rasterio.Affine.identity(),
        "EPSG:32614",
        date(2020, 1, 1),
    )
    assert out.applied is False
    assert ("flood map" if which == "flood" else "curve") in out.reason


def test_bed_mask_marks_hyacinth_above_margin_only() -> None:
    nodata = settings.CLASS_NODATA
    soil = settings.CLASS_BARE_SOIL
    class_map = np.array(
        [
            [DENSE, INTERFACE, DENSE, INTERFACE, WATER, soil, DENSE, nodata],
        ],
        dtype=np.uint8,
    )
    # mirror, band (level < f <= margin), band edge, safe bed, safe bed
    # (water), safe bed (soil), outside the flood map, safe bed (nodata class)
    flood = np.array(
        [[2000.0, 2001.0, 2002.0, 2003.0, 2003.0, 2003.0, -9999.0, 2003.0]]
    )
    mask = exposed_bed_mask(class_map, flood, 2000.5, 2002.0)
    assert mask.dtype == bool
    assert mask.tolist() == [[False, False, False, True, False, False, False, False]]


def test_bed_mask_uses_given_margin_not_level() -> None:
    class_map = np.array([[DENSE, DENSE]], dtype=np.uint8)
    flood = np.array([[2001.0, 2005.0]])
    assert exposed_bed_mask(class_map, flood, 2000.0, 2000.0).tolist() == [[True, True]]
    assert exposed_bed_mask(class_map, flood, 2000.0, 2003.0).tolist() == [
        [False, True]
    ]


def test_bed_mask_matches_compute_bed_correction_removed() -> None:
    rng = np.random.default_rng(7)
    class_map = rng.integers(0, 5, size=(40, 40)).astype(np.uint8)
    flood = rng.uniform(1998.0, 2006.0, size=(40, 40))
    flood[rng.random((40, 40)) < 0.1] = -9999.0
    stages = [1998.0, 2000.0, 2002.0, 2004.0, 2006.0]
    areas = [0.1, 0.3, 0.6, 0.9, 1.2]
    out = compute_bed_correction(
        class_map, flood, 2001.2, stages, areas, areas, areas, 100.0
    )
    assert out.margin_m is not None
    mask = exposed_bed_mask(class_map, flood, 2001.2, out.margin_m)
    assert float(mask.sum()) * 0.01 == pytest.approx(out.removed_ha)


def test_bed_mask_margin_function_matches_compute_bed_correction() -> None:
    stages = [2000.0, 2001.0, 2002.0, 2003.0]
    areas = [0.1, 0.2, 0.9, 1.0]
    out = compute_bed_correction(
        np.array([[DENSE]]),
        np.array([[2000.0]]),
        2000.5,
        stages,
        areas,
        areas,
        areas,
        100.0,
    )
    assert bed_margin(2000.5, stages, areas, areas, areas) == out.margin_m
    assert bed_margin(2000.5, stages, areas, areas, areas) == pytest.approx(
        2001 + 24 / 70
    )


def test_day_mirror_boundary_above_and_nodata() -> None:
    flood = np.array(
        [[2000.0, 2001.0, 2001.5, FLOOD_NODATA]],
        dtype=np.float64,
    )
    out = day_mirror(flood, 2001.0)
    assert out.dtype == bool
    # flood == margin is inside; above is outside; nodata is outside.
    assert out.tolist() == [[True, True, False, False]]


def _patch_mirror_inputs(monkeypatch: pytest.MonkeyPatch, level) -> np.ndarray:
    monkeypatch.setattr(bed_module, "level_on", lambda *a, **k: level)
    flood = np.array([[2000.0, 2001.0, 2002.0, 2003.0]], dtype=np.float32)
    monkeypatch.setattr(
        bed_module,
        "load_flood_map",
        lambda *a, **k: (flood, rasterio.Affine.identity(), "EPSG:32614"),
    )
    monkeypatch.setattr(
        bed_module,
        "load_curve",
        lambda *a, **k: StageAreaCurve(
            stage_m=np.array([2000.0, 2001.0, 2002.0, 2003.0]),
            volume_hm3=np.zeros(4),
            area_km2=np.array([0.1, 0.2, 0.9, 1.0]),
            area_p2_5=np.array([0.1, 0.2, 0.9, 1.0]),
            area_p97_5=np.array([0.1, 0.2, 0.9, 1.0]),
        ),
    )
    return flood


def test_load_day_mirror_returns_expected_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flood = _patch_mirror_inputs(monkeypatch, 2000.5)
    out = load_day_mirror(
        date(2020, 1, 1), rasterio.Affine.identity(), "EPSG:32614", (1, 4)
    )
    # margin 2001.342857...: the first two pixels are under water.
    assert out is not None
    assert out.tolist() == [[True, True, False, False]]
    assert out.tolist() == day_mirror(flood, 2001.342857142857).tolist()


@pytest.mark.parametrize("level", [None, float("nan")])
def test_load_day_mirror_none_without_level(
    monkeypatch: pytest.MonkeyPatch, level
) -> None:
    _patch_mirror_inputs(monkeypatch, level)
    assert (
        load_day_mirror(
            date(2020, 1, 1), rasterio.Affine.identity(), "EPSG:32614", (1, 4)
        )
        is None
    )


@pytest.mark.parametrize("aspect", ["shape", "transform", "crs"])
def test_load_day_mirror_none_on_grid_mismatch(
    monkeypatch: pytest.MonkeyPatch, aspect: str
) -> None:
    _patch_mirror_inputs(monkeypatch, 2000.5)
    transform, crs, shape = rasterio.Affine.identity(), "EPSG:32614", (1, 4)
    if aspect == "shape":
        shape = (2, 2)
    if aspect == "transform":
        transform = rasterio.Affine.translation(10.0, 0.0)
    if aspect == "crs":
        crs = "EPSG:32613"
    assert load_day_mirror(date(2020, 1, 1), transform, crs, shape) is None


def test_load_day_mirror_none_on_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_mirror_inputs(monkeypatch, 2000.5)
    monkeypatch.setattr(bed_module, "DEFAULT_FLOOD_PATH", tmp_path / "nope.tif")
    assert (
        load_day_mirror(
            date(2020, 1, 1), rasterio.Affine.identity(), "EPSG:32614", (1, 4)
        )
        is None
    )


def test_load_day_mirror_none_on_missing_curve(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_mirror_inputs(monkeypatch, 2000.5)
    monkeypatch.setattr(bed_module, "DEFAULT_CURVE_CSV_PATH", tmp_path / "nope.csv")
    assert (
        load_day_mirror(
            date(2020, 1, 1), rasterio.Affine.identity(), "EPSG:32614", (1, 4)
        )
        is None
    )
