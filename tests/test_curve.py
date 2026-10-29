"""Tests for the stage–area curve (synthetic tables; no network, no SIH file)."""

import json
from datetime import date, timedelta

import numpy as np
import pytest

from smol_frontier.config import settings
from smol_frontier.hydro.curve import (
    DEFAULT_CURVE_CSV_PATH,
    GRID_N,
    S_FACTORS,
    StageAreaCurve,
    area_ha,
    bootstrap_bands,
    build_curve,
    fit_curve,
    level_volume_pairs,
    load_curve,
    stage_for_area,
)
from smol_frontier.hydro.levels import LevelRow

SLOPE = 8.0  # hm3/m == km2


def _line_table() -> tuple[np.ndarray, np.ndarray]:
    """Exact line, 10 points per 0.10 m bin, 2000–2020 m."""
    h = np.arange(2000.0, 2020.0 + 0.005, 0.01)
    return h, SLOPE * (h - 2000.0) + 50.0


def _on_line(*stages: float) -> tuple[tuple[float, float], ...]:
    return tuple((x, float(SLOPE * (x - 2000.0) + 50.0)) for x in stages)


def _max_slope_rel_err(h: np.ndarray, v: np.ndarray, **kwargs) -> float:
    fit = fit_curve(h, v, **kwargs)
    pts = np.linspace(fit.h_min, fit.h_max, 2000)
    return float(np.abs(fit.spline.derivative()(pts) - SLOPE).max() / SLOPE)


def test_line_recovers_slope_without_anchors() -> None:
    h, v = _line_table()
    assert _max_slope_rel_err(h, v, anchors=()) < 1e-3


def test_line_recovers_slope_with_on_line_anchors() -> None:
    h, v = _line_table()
    # Smoothing-spline edge effect, amplified by the anchor/bin weight
    # contrast (50 vs ~3): the edge-decay zone is ~3-4 m on this
    # fixture, so no separate interior bound is asserted (the anchors=()
    # slope test and the golden rebuild guard the method instead).
    # Measured max relative slope error vs end-exclusion width:
    #   +-0.0 m: 2.29e-3    +-2 m: 1.42e-3    +-4 m: 7.37e-4
    #   +-0.5 m: 2.05e-3    +-3 m: 1.05e-3    +-5 m: 4.63e-4
    #   +-1.0 m: 1.83e-3    (max always at the exclusion boundary)
    assert _max_slope_rel_err(h, v, anchors=_on_line(2005.0, 2010.0, 2015.0)) <= 5e-3


def test_default_anchors_are_the_official_ones() -> None:
    h, v = _line_table()
    assert fit_curve(h, v).h_max == 2020.0  # official top anchor
    assert fit_curve(h, v, anchors=()).h_max == settings.CURVE_FIT_H_MAX


def test_anchors_honored_on_smooth_curve() -> None:
    h = np.arange(2000.0, 2020.0 + 0.005, 0.01)
    v = 50.0 + 8.0 * (h - 2000.0) + 0.05 * (h - 2000.0) ** 2
    anchors = tuple(
        (x, float(50.0 + 8.0 * (x - 2000.0) + 0.05 * (x - 2000.0) ** 2))
        for x in (2005.0, 2012.0, 2018.0)
    )
    fit = fit_curve(h, v, anchors=anchors)
    for anchor_h, anchor_v in anchors:
        assert abs(float(fit.spline(anchor_h)) - anchor_v) < 0.5


def test_bootstrap_is_reproducible() -> None:
    h, v = _line_table()
    grid = np.linspace(2001.0, 2016.0, 50)
    first = bootstrap_bands(h, v, grid, anchors=(), n_boot=30, seed=7)
    second = bootstrap_bands(h, v, grid, anchors=(), n_boot=30, seed=7)
    assert np.array_equal(first[0], second[0])
    assert np.array_equal(first[1], second[1])
    assert first[0].shape == (50,)
    assert bool(np.all(first[0] <= first[1]))


def test_pairs_keep_order_and_drop_nan_and_late_rows() -> None:
    rows = [
        LevelRow(date(2024, 1, 3), 2001.0, 60.0),
        LevelRow(date(2024, 1, 1), 2000.0, 50.0),
        LevelRow(date(2024, 1, 2), float("nan"), 55.0),
        LevelRow(date(2024, 1, 4), 2002.0, float("nan")),
        LevelRow(date(2026, 9, 21), 2003.0, 70.0),
    ]
    h, volumes = level_volume_pairs(rows)
    assert h.tolist() == [2001.0, 2000.0]
    assert volumes.tolist() == [60.0, 50.0]


CURVE_HEADER = "stage_m,volume_hm3,area_km2,area_km2_p2_5,area_km2_p97_5"


def _write_curve(path, stages, volumes, areas) -> None:
    lines = [CURVE_HEADER]
    for s, v, a in zip(stages, volumes, areas):
        lines.append(f"{s:.3f},{v:.3f},{a:.4f},{a - 1:.4f},{a + 1:.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_area_ha_clips_outside_range(tmp_path) -> None:
    target = tmp_path / "curve.csv"
    _write_curve(
        target,
        [2000.0, 2001.0, 2002.0],
        [50.0, 60.0, 71.0],
        [10.0, 11.0, 12.0],
    )
    assert area_ha(2001.0, target) == pytest.approx(1100.0)
    assert area_ha(1999.0, target) == pytest.approx(1000.0)
    assert area_ha(2003.0, target) == pytest.approx(1200.0)


def test_stage_for_area_inverts_area_ha(tmp_path) -> None:
    target = tmp_path / "curve.csv"
    _write_curve(
        target,
        [2000.0, 2001.0, 2002.0],
        [50.0, 60.0, 71.0],
        [10.0, 11.0, 12.0],
    )
    for stage in (2000.0, 2000.5, 2001.7, 2002.0):
        assert stage_for_area(area_ha(stage, target), target) == pytest.approx(
            stage, rel=1e-9
        )


def test_stage_for_area_rejects_non_monotonic(tmp_path) -> None:
    target = tmp_path / "curve.csv"
    _write_curve(
        target, [2000.0, 2001.0, 2002.0], [50.0, 60.0, 71.0], [10.0, 12.0, 11.0]
    )
    with pytest.raises(ValueError, match="monotonic"):
        stage_for_area(1100.0, target)


def test_load_curve_rejects_bad_header(tmp_path) -> None:
    target = tmp_path / "curve.csv"
    target.write_text("nope\n1,2,3,4,5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        load_curve(target)


def _synthetic_sih(days: int = 600) -> bytes:
    slope = (182.0 - 52.1) / (2018.0 - 2003.16)
    lines = ["preamble", "Estacion,Fecha,Nivel(m),VolumenAlm(Mm3)"]
    start = date(2024, 1, 1)
    for i in range(days):
        level = 2000.0 + (i % 600) * 0.01
        volume = slope * (level - 2003.16) + 52.1
        lines.append(
            f"ENDHD,{(start + timedelta(days=i)).isoformat()},{level:.2f},{volume:.3f}"
        )
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def test_build_writes_curve_and_summary(tmp_path) -> None:
    levels_path = tmp_path / "ENDHD.csv"
    levels_path.write_bytes(_synthetic_sih())
    csv_path = tmp_path / "stage_area_curve.csv"
    report = build_curve(levels_path, csv_path)
    assert report.n_pairs == 600
    assert report.s_factor in S_FACTORS
    assert sorted(report.model_errors_km2) == [2005, 2010, 2015, 2018]
    rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == CURVE_HEADER
    assert len(rows) == GRID_N + 1
    curve = load_curve(csv_path)
    assert isinstance(curve, StageAreaCurve)
    assert len(curve.stage_m) == GRID_N
    assert bool(np.all(np.isfinite(curve.area_km2)))
    assert bool(np.all(np.isfinite(curve.volume_hm3)))
    assert bool(np.all(np.diff(curve.stage_m) > 0))
    assert bool(np.all(curve.area_p2_5 <= curve.area_p97_5))
    summary = json.loads((tmp_path / "stage_area_curve.csv.json").read_text())
    assert summary["n_pairs"] == 600
    assert summary["s_factor"] == report.s_factor
    assert sorted(summary["model_error_km2"]) == ["2005", "2010", "2015", "2018"]
    assert summary["area_at_2018_km2"] == round(report.area_at_2018_km2, 4)
    assert summary["volume_at_2018_hm3"] == round(report.volume_at_2018_hm3, 3)


def test_rebuild_reproduces_committed_curve(tmp_path) -> None:
    """Golden test: rebuilding from ENDHD.csv matches the committed CSV byte-for-byte.

    No network (reads the local SIH copy). ~2 s; kept in the default run as
    the regression guard for later steps.
    """
    target = tmp_path / "stage_area_curve.csv"
    report = build_curve(curve_path=target)
    assert report.n_pairs == 12226
    assert target.read_bytes() == DEFAULT_CURVE_CSV_PATH.read_bytes()
