"""Tests for the exposed-bed correction (synthetic arrays; files mocked)."""

from datetime import date

import numpy as np
import pytest
import rasterio

import smol_frontier.hydro.bed as bed_module
from smol_frontier.config import settings
from smol_frontier.hydro.bed import (
    compute_bed_correction,
    correct_bed,
    cushion_ha,
)
from smol_frontier.hydro.curve import StageAreaCurve

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
