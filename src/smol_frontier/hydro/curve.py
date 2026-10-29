"""Stage–area curve of the reservoir, A(h), from SIH level–volume pairs.

The SIH computes stored volume from the level with the official
elevation–area–capacity table, so the (h, V) pairs reproduce that table and
the water-surface area is A(h) = dV/dh. Method reproduced from the author's
prior research: 0.10 m median bins, official design-report anchors, isotonic
regression, then a smoothing spline with the smallest factor giving a
positive derivative; bootstrap bands by days. A later check compares the
output with the author's curve, so every step below is spec-literal.
"""

import csv
import json
import logging
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import UnivariateSpline
from sklearn.isotonic import IsotonicRegression

from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.hydro.levels import LevelRow, load_levels

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CURVE_CSV_PATH",
    "CurveBuild",
    "FittedCurve",
    "StageAreaCurve",
    "area_ha",
    "bootstrap_bands",
    "build_curve",
    "fit_curve",
    "level_volume_pairs",
    "load_curve",
    "model_errors",
    "stage_for_area",
]

# WHY: output grid size — 400 points resolve the 38 m level range at ~0.1 m.
GRID_N = 400

# WHY: derivative-positivity check resolution — 600 evenly spaced points over
#   the fit domain catch any wiggle the smoothing factor must remove.
DERIV_CHECK_N = 600

# WHY: smoothing-factor ladder — the first factor giving a strictly positive
#   derivative wins, so the fit stays as close to the data as physics allows.
S_FACTORS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0)

# WHY: merge resolution — bin keys carry float dust (k * 0.10), so duplicates
#   merge after rounding h to 3 decimals (mm, below the data's cm noise).
MERGE_DECIMALS = 3

# WHY: stages where the anchor-weight sensitivity is reported — spread over
#   the operating range up to the spillway crest.
MODEL_ERROR_STAGES = (2005, 2010, 2015, 2018)

# WHY: anchor weights bracketing production (50) by 5x each way, so the
#   model error spans "anchors barely matter" to "anchors dominate".
MODEL_ERROR_W_ANCHORS = (10.0, 200.0)

# WHY: committed stage–area curve — the exposed-bed correction reads A(h)
#   from here, never by refitting, so runs are reproducible offline.
# HOW: repo layout anchored at PROJECT_ROOT; CSV plus a .json summary.
DEFAULT_CURVE_CSV_PATH = PROJECT_ROOT / "data/hydro/stage_area_curve.csv"

CSV_COLUMNS = (
    "stage_m",
    "volume_hm3",
    "area_km2",
    "area_km2_p2_5",
    "area_km2_p97_5",
)


@dataclass(frozen=True)
class FittedCurve:
    """One V(h) fit: smoothing spline, s factor used, and fit domain."""

    spline: UnivariateSpline
    s_factor: float
    h_min: float
    h_max: float


@dataclass(frozen=True, eq=False)
class StageAreaCurve:
    """Loaded curve grids: stage, volume, area, and bootstrap bands."""

    stage_m: np.ndarray
    volume_hm3: np.ndarray
    area_km2: np.ndarray
    area_p2_5: np.ndarray
    area_p97_5: np.ndarray


@dataclass(frozen=True)
class CurveBuild:
    """Result of a curve build: report values and output paths."""

    n_pairs: int
    level_min: float
    level_max: float
    s_factor: float
    area_at_2018_km2: float
    volume_at_2018_hm3: float
    volume_at_2003_16_hm3: float
    model_errors_km2: dict[int, float]
    csv_path: Path
    json_path: Path


def level_volume_pairs(rows: Sequence[LevelRow]) -> tuple[np.ndarray, np.ndarray]:
    """Pairs for the fit: both finite, date <= CURVE_DATA_END, file order."""
    end = settings.CURVE_DATA_END
    kept = [
        (row.level_m, row.volume_hm3)
        for row in rows
        if row.date <= end
        and math.isfinite(row.level_m)
        and math.isfinite(row.volume_hm3)
    ]
    h = np.array([pair[0] for pair in kept], dtype=np.float64)
    volumes = np.array([pair[1] for pair in kept], dtype=np.float64)
    return h, volumes


def _median_bins(
    h: np.ndarray, volumes: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """0.10 m median bins for h <= CURVE_FIT_H_MAX; weight = sqrt(n)."""
    s = settings
    mask = h <= s.CURVE_FIT_H_MAX
    hb, vb = h[mask], volumes[mask]
    keys = np.round(hb / s.CURVE_BIN_M) * s.CURVE_BIN_M
    uniq, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
    bin_h: list[float] = []
    bin_v: list[float] = []
    bin_w: list[float] = []
    for index, key in enumerate(uniq):
        if counts[index] < s.CURVE_BIN_MIN_N:
            continue
        bin_h.append(float(key))
        bin_v.append(float(np.median(vb[inv == index])))
        bin_w.append(math.sqrt(counts[index]))
    return np.array(bin_h), np.array(bin_v), np.array(bin_w)


def _merge_duplicates(
    h: np.ndarray, volumes: np.ndarray, weights: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sort by h; merge equal-after-rounding h (weighted V, summed w)."""
    order = np.argsort(h, kind="stable")
    rounded = np.round(h[order], MERGE_DECIMALS)
    uniq, inv = np.unique(rounded, return_inverse=True)
    merged_v = np.zeros(len(uniq))
    merged_w = np.zeros(len(uniq))
    np.add.at(merged_v, inv, volumes[order] * weights[order])
    np.add.at(merged_w, inv, weights[order])
    return uniq, merged_v / merged_w, merged_w


def fit_curve(
    h: np.ndarray,
    volumes: np.ndarray,
    w_anchor: float = 50.0,
    anchors: Sequence[tuple[float, float]] | None = None,
) -> FittedCurve:
    """Fit V(h): bins + anchors, isotonic, smoothing spline.

    ``anchors`` defaults to the official design-report points from config;
    pass ``()`` for an anchor-free fit (synthetic tests) or points on a
    synthetic line/curve. Each anchor carries weight ``w_anchor``.
    """
    s = settings
    if anchors is None:
        anchors = s.CURVE_ANCHORS
    h = np.asanyarray(h, dtype=np.float64)
    volumes = np.asanyarray(volumes, dtype=np.float64)
    bin_h, bin_v, bin_w = _median_bins(h, volumes)
    anchor_h = np.array([point[0] for point in anchors], dtype=np.float64)
    anchor_v = np.array([point[1] for point in anchors], dtype=np.float64)
    anchor_w = np.full(len(anchor_h), w_anchor, dtype=np.float64)
    merged_h, merged_v, merged_w = _merge_duplicates(
        np.concatenate([bin_h, anchor_h]),
        np.concatenate([bin_v, anchor_v]),
        np.concatenate([bin_w, anchor_w]),
    )
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    v_iso = iso.fit_transform(merged_h, merged_v, sample_weight=merged_w)
    weights = merged_w / merged_w.mean()
    check = np.linspace(merged_h[0], merged_h[-1], DERIV_CHECK_N)
    for fac in S_FACTORS:
        candidate = UnivariateSpline(
            merged_h, v_iso, w=weights, k=3, s=fac * len(merged_h)
        )
        spline, s_factor = candidate, fac
        if bool(np.all(candidate.derivative()(check) > 0)):
            break
    else:
        warnings.warn(
            f"no smoothing factor in {S_FACTORS} gave a positive derivative; "
            f"kept fac={s_factor}",
            UserWarning,
            stacklevel=2,
        )
    return FittedCurve(
        spline=spline, s_factor=s_factor, h_min=merged_h[0], h_max=merged_h[-1]
    )


def bootstrap_bands(
    h: np.ndarray,
    volumes: np.ndarray,
    grid: np.ndarray,
    *,
    w_anchor: float = 50.0,
    anchors: Sequence[tuple[float, float]] | None = None,
    n_boot: int | None = None,
    seed: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """p2.5/p97.5 area bands from day-resampling bootstrap refits.

    One rng (seed fixed, so bands reproduce); a refit that raises is
    skipped — its rng draw was still consumed.
    """
    s = settings
    if n_boot is None:
        n_boot = s.CURVE_BOOTSTRAP_B
    if seed is None:
        seed = s.CURVE_BOOTSTRAP_SEED
    rng = np.random.default_rng(seed)
    count = len(h)
    derivs: list[np.ndarray] = []
    for _ in range(n_boot):
        idx = rng.integers(0, count, count)
        try:
            fit = fit_curve(h[idx], volumes[idx], w_anchor=w_anchor, anchors=anchors)
        except Exception as exc:  # noqa: BLE001 — spec: any failed refit is skipped
            logger.debug("bootstrap refit skipped: %r", exc)
            continue
        derivs.append(fit.spline.derivative()(grid))
    logger.info("bootstrap: %d/%d refits kept", len(derivs), n_boot)
    bands = np.percentile(np.array(derivs), [2.5, 97.5], axis=0)
    return bands[0], bands[1]


def model_errors(
    h: np.ndarray,
    volumes: np.ndarray,
    *,
    w_anchor: float = 50.0,
    anchors: Sequence[tuple[float, float]] | None = None,
) -> dict[int, float]:
    """Per-stage max |A − A_alt| over the bracketing anchor weights (km²)."""
    if anchors is None:
        anchors = settings.CURVE_ANCHORS
    main = fit_curve(h, volumes, w_anchor=w_anchor, anchors=anchors).spline.derivative()
    alts = [
        fit_curve(h, volumes, w_anchor=w, anchors=anchors).spline.derivative()
        for w in MODEL_ERROR_W_ANCHORS
    ]
    return {
        stage: max(abs(float(main(stage)) - float(alt(stage))) for alt in alts)
        for stage in MODEL_ERROR_STAGES
    }


def _curve_json_path(csv_path: Path) -> Path:
    """Summary sidecar next to the curve CSV."""
    return csv_path.with_name(f"{csv_path.name}.json")


def build_curve(
    levels_path: Path | str | None = None,
    curve_path: Path | str | None = None,
) -> CurveBuild:
    """Fit the curve from the SIH pairs and write the CSV + JSON summary."""
    s = settings
    h, volumes = level_volume_pairs(load_levels(levels_path))
    fit = fit_curve(h, volumes, w_anchor=s.CURVE_W_ANCHOR, anchors=s.CURVE_ANCHORS)
    grid = np.linspace(h.min(), s.CURVE_GRID_TOP, GRID_N)
    grid_v = fit.spline(grid)
    grid_a = fit.spline.derivative()(grid)
    low, high = bootstrap_bands(
        h, volumes, grid, w_anchor=s.CURVE_W_ANCHOR, anchors=s.CURVE_ANCHORS
    )
    errors = model_errors(
        h, volumes, w_anchor=s.CURVE_W_ANCHOR, anchors=s.CURVE_ANCHORS
    )
    csv_path = DEFAULT_CURVE_CSV_PATH if curve_path is None else Path(curve_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(CSV_COLUMNS) + "\n")
        for stage, vol, area, p_lo, p_hi in zip(grid, grid_v, grid_a, low, high):
            handle.write(f"{stage:.3f},{vol:.3f},{area:.4f},{p_lo:.4f},{p_hi:.4f}\n")
    summary = {
        "n_pairs": len(h),
        "level_min": round(float(h.min()), 2),
        "level_max": round(float(h.max()), 2),
        "s_factor": fit.s_factor,
        "model_error_km2": {str(stage): round(err, 4) for stage, err in errors.items()},
        "area_at_2018_km2": round(float(fit.spline.derivative()(2018.0)), 4),
        "volume_at_2018_hm3": round(float(fit.spline(2018.0)), 3),
    }
    json_path = _curve_json_path(csv_path)
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return CurveBuild(
        n_pairs=len(h),
        level_min=float(h.min()),
        level_max=float(h.max()),
        s_factor=fit.s_factor,
        area_at_2018_km2=float(fit.spline.derivative()(2018.0)),
        volume_at_2018_hm3=float(fit.spline(2018.0)),
        volume_at_2003_16_hm3=float(fit.spline(2003.16)),
        model_errors_km2=errors,
        csv_path=csv_path,
        json_path=json_path,
    )


def load_curve(path: Path | str | None = None) -> StageAreaCurve:
    """Load the curve grids from its CSV (default: data/hydro)."""
    target = DEFAULT_CURVE_CSV_PATH if path is None else Path(path)
    with target.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = tuple(next(reader))
        if header != CSV_COLUMNS:
            msg = f"unexpected curve header in {target}: {header}"
            raise ValueError(msg)
        data = list(reader)
        columns = tuple(
            np.array([float(row[i]) for row in data], dtype=np.float64)
            for i in range(len(CSV_COLUMNS))
        )
    return StageAreaCurve(
        stage_m=columns[0],
        volume_hm3=columns[1],
        area_km2=columns[2],
        area_p2_5=columns[3],
        area_p97_5=columns[4],
    )


def area_ha(h: float, path: Path | str | None = None) -> float:
    """Water-surface area in ha at level h — clipped, never extrapolated."""
    curve = load_curve(path)
    return float(np.interp(h, curve.stage_m, curve.area_km2) * 100.0)


def stage_for_area(area_ha_value: float, path: Path | str | None = None) -> float:
    """Level in m for a water-surface area in ha (area must be monotonic)."""
    curve = load_curve(path)
    areas = curve.area_km2 * 100.0
    if not bool(np.all(np.diff(areas) > 0)):
        msg = "stage_for_area requires a monotonic area grid"
        raise ValueError(msg)
    return float(np.interp(area_ha_value, areas, curve.stage_m))
