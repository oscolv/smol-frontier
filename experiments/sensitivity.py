"""Step 9z: TAU sensitivity grid + spectral-cloud on/off check.

Runs the raw pipeline (classify + quality, no exposed-bed correction — the
same path as ``smolf gate identity --all``) over the 465 reference scenes
read from ``data/cache``, under 9 threshold configurations:

- TAU_WATER in 0.50–0.99 with TAU_HYACINTH = 0.55 (7 configs),
- TAU_HYACINTH in 0.45/0.55/0.65 with TAU_WATER = 0.80 (3 configs),

with (0.80, 0.55) as the shared baseline. Configurations run sequentially,
one override at a time; scenes within a configuration run concurrently.

Settings is a frozen dataclass read as a module global, so each override is
applied only here via ``dataclasses.replace`` plus patching the ``settings``
module attribute in every module that reads TAU_WATER or TAU_HYACINTH. Grep
audit: the only reader is ``smol_frontier/classify/gmm.py`` (``stage1`` and
``stage2`` via ``s = settings`` at call time); ``config.py`` defines the
fields. No production code path gains a parameter or changes behavior.

Then, for the 2025-07-29 haze scene and the 2025-07-24 control, the same
script re-runs classify with the spectral cloud test disabled (only here, by
patching ``masks.spectral_cloud`` to all-False) and on.

Usage::

    uv run python experiments/sensitivity.py

Writes per-scene results to ``outputs/sensitivity/`` (git-ignored) and
prints the summary tables that ``experiments/sensitivity.md`` transcribes.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np

from smol_frontier import config as config_mod
from smol_frontier.classify import gmm as gmm_mod
from smol_frontier.classify import masks as masks_mod
from smol_frontier.classify import pipeline as pipeline_mod
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.gate import load_reference
from smol_frontier.ingest import DEFAULT_CACHE_DIR, _cache_path, ingest_scene
from smol_frontier.quality import assess_quality
from smol_frontier.sources.stac import SceneMeta

TAU_WATER_GRID = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.99)
TAU_HYACINTH_GRID = (0.45, 0.55, 0.65)
BASELINE = (0.80, 0.55)
REFERENCE_GATE_CSV = PROJECT_ROOT / "outputs/identity_gate/gate_20261028T212616.csv"
CLOUD_SCENES = (
    ("2025-07-29", "S2B_14QMH_20250729_0_L2A"),
    ("2025-07-24", "S2C_14QMH_20250724_0_L2A"),
)
RECENT_FROM = "2024-11-01"

SENSITIVITY_FIELDS = (
    "config",
    "tau_water",
    "tau_hyacinth",
    "date",
    "item_id",
    "dense_ha",
    "interface_ha",
    "total_ha",
    "water_ha",
    "soil_ha",
    "valid_frac",
    "cloud_frac",
    "light",
    "regime",
    "n_nodata",
    "n_water",
    "n_dense",
    "n_interface",
    "n_soil",
)

CLOUD_FIELDS = (
    "date",
    "item_id",
    "spectral",
    "valid_frac",
    "cloud_frac",
    "total_ha",
    "dense_ha",
    "interface_ha",
    "light",
    "regime",
)


@contextlib.contextmanager
def tau_override(tau_water: float, tau_hyacinth: float):
    """Apply one TAU configuration, restoring the originals afterwards."""
    orig_config = config_mod.settings
    orig_gmm = gmm_mod.settings
    new = dataclasses.replace(
        orig_config, TAU_WATER=tau_water, TAU_HYACINTH=tau_hyacinth
    )
    config_mod.settings = new
    gmm_mod.settings = new
    try:
        # Direct proof the override is what the classifier reads: stage1 and
        # stage2 bind `s = settings` from the gmm module global at call time.
        assert gmm_mod.settings.TAU_WATER == tau_water
        assert gmm_mod.settings.TAU_HYACINTH == tau_hyacinth
        yield new
    finally:
        config_mod.settings = orig_config
        gmm_mod.settings = orig_gmm


@contextlib.contextmanager
def spectral_off():
    """Disable the spectral cloud test (all-False), restoring it afterwards."""
    orig = masks_mod.spectral_cloud

    def _off(b02, b11, b04):
        _ = (b11, b04)
        return np.zeros(np.shape(np.asanyarray(b02)), dtype=bool)

    masks_mod.spectral_cloud = _off
    try:
        probe = masks_mod.spectral_cloud(
            np.array([0.5]), np.array([0.5]), np.array([0.1])
        )
        assert not np.asanyarray(probe).any()
        yield
    finally:
        masks_mod.spectral_cloud = orig


def measure_scene(item_id: str, day: str) -> dict:
    """Ingest one scene from cache; classify + quality (no bed correction)."""
    path = _cache_path(DEFAULT_CACHE_DIR, item_id)
    if not path.exists():
        msg = f"scene {item_id} not in data/cache"
        raise FileNotFoundError(msg)
    # Cache-hit path: ingest_scene re-reads the scene metadata from the cache
    # tags and ignores these dummy fields, so this never touches the network.
    meta = SceneMeta(
        id=item_id,
        datetime=datetime.fromisoformat(day),
        source="earth-search",
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    ing = ingest_scene(meta)
    result = pipeline_mod.classify(ing)
    quality = assess_quality(result)
    ha = result.ha_per_class
    counts = {
        code: int((result.class_map == code).sum())
        for code in (
            settings.CLASS_NODATA,
            settings.CLASS_WATER,
            settings.CLASS_DENSE_HYACINTH,
            settings.CLASS_INTERFACE,
            settings.CLASS_BARE_SOIL,
        )
    }
    return {
        "date": day,
        "item_id": item_id,
        "dense_ha": ha[settings.CLASS_DENSE_HYACINTH],
        "interface_ha": ha[settings.CLASS_INTERFACE],
        "total_ha": result.hyacinth_total_ha,
        "water_ha": ha[settings.CLASS_WATER],
        "soil_ha": ha[settings.CLASS_BARE_SOIL],
        "valid_frac": result.valid_fraction,
        "cloud_frac": result.cloud_fraction,
        "light": quality.light,
        "regime": quality.regime,
        "counts": (
            counts[settings.CLASS_NODATA],
            counts[settings.CLASS_WATER],
            counts[settings.CLASS_DENSE_HYACINTH],
            counts[settings.CLASS_INTERFACE],
            counts[settings.CLASS_BARE_SOIL],
        ),
    }


def run_config(
    tau_water: float, tau_hyacinth: float, targets: list[tuple[str, str]], workers: int
) -> list[dict]:
    """Measure every scene under one TAU configuration (patches, then runs)."""
    with tau_override(tau_water, tau_hyacinth):
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(measure_scene, item_id, day): (day, item_id)
                for day, item_id in targets
            }
            for i, future in enumerate(as_completed(futures), start=1):
                rows.append(future.result())
                if i % 100 == 0:
                    print(f"  {i}/{len(targets)} scenes", flush=True)
    rows.sort(key=lambda row: (row["date"], row["item_id"]))
    return rows


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (same method as gate._percentile)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def block_stats(
    rows: list[dict], base: dict[tuple[str, str], dict], key: str
) -> tuple[float, float, float, str]:
    """Median, p95, max of |Δ key| in ha plus the max scene's date."""
    deltas = [abs(row[key] - base[(row["date"], row["item_id"])][key]) for row in rows]
    worst = max(rows, key=lambda r: abs(r[key] - base[(r["date"], r["item_id"])][key]))
    return (
        statistics.median(deltas),
        percentile(deltas, 95.0),
        max(deltas),
        worst["date"],
    )


def changed_labels(
    rows: list[dict], base: dict[tuple[str, str], dict]
) -> tuple[int, int, int]:
    """Scenes whose light / regime / either changed vs baseline."""
    n_light = n_regime = n_either = 0
    for row in rows:
        other = base[(row["date"], row["item_id"])]
        light_changed = row["light"] != other["light"]
        regime_changed = row["regime"] != other["regime"]
        n_light += light_changed
        n_regime += regime_changed
        n_either += light_changed or regime_changed
    return n_light, n_regime, n_either


def changed_pixels(rows: list[dict], base: dict[tuple[str, str], dict]) -> int:
    """Scenes whose class pixel counts differ from baseline at all."""
    return sum(
        1
        for row in rows
        if row["counts"] != base[(row["date"], row["item_id"])]["counts"]
    )


def load_gate_totals(path: Path) -> dict[tuple[str, str], float]:
    """Ours-total-ha per (date, item) from a gate CSV."""
    totals = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            totals[(raw["date"], raw["item_id_requested"])] = float(
                raw["ours_total_ha"]
            )
    return totals


def main() -> None:
    """Run the TAU grid and the cloud on/off check; write CSVs; print tables."""
    parser = argparse.ArgumentParser(description="Step 9z sensitivity experiments.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "outputs/sensitivity"
    )
    args = parser.parse_args()
    if args.workers < 1:
        msg = "--workers must be >= 1"
        raise SystemExit(msg)

    reference = load_reference()
    targets = [(row.date.isoformat(), row.item_id) for row in reference]
    assert len(targets) == 465, f"expected 465 reference scenes, got {len(targets)}"

    configs = [(BASELINE[0], BASELINE[1])]
    configs += [(w, BASELINE[1]) for w in TAU_WATER_GRID if w != BASELINE[0]]
    configs += [(BASELINE[0], h) for h in TAU_HYACINTH_GRID if h != BASELINE[1]]
    assert len(configs) == 9, f"expected 9 configurations, got {len(configs)}"

    results: dict[tuple[float, float], list[dict]] = {}
    for tau_water, tau_hyacinth in configs:
        print(f"config TAU_WATER={tau_water} TAU_HYACINTH={tau_hyacinth}", flush=True)
        results[(tau_water, tau_hyacinth)] = run_config(
            tau_water, tau_hyacinth, targets, args.workers
        )
    assert config_mod.settings.TAU_WATER == BASELINE[0]
    assert gmm_mod.settings.TAU_HYACINTH == BASELINE[1]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sens_path = args.output_dir / "sensitivity.csv"
    with sens_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SENSITIVITY_FIELDS))
        writer.writeheader()
        for (tau_water, tau_hyacinth), rows in results.items():
            label = f"w{tau_water:g}_h{tau_hyacinth:g}"
            for row in rows:
                writer.writerow(
                    {
                        "config": label,
                        "tau_water": repr(tau_water),
                        "tau_hyacinth": repr(tau_hyacinth),
                        "date": row["date"],
                        "item_id": row["item_id"],
                        "dense_ha": repr(row["dense_ha"]),
                        "interface_ha": repr(row["interface_ha"]),
                        "total_ha": repr(row["total_ha"]),
                        "water_ha": repr(row["water_ha"]),
                        "soil_ha": repr(row["soil_ha"]),
                        "valid_frac": repr(row["valid_frac"]),
                        "cloud_frac": repr(row["cloud_frac"]),
                        "light": row["light"],
                        "regime": row["regime"] or "clean",
                        "n_nodata": row["counts"][0],
                        "n_water": row["counts"][1],
                        "n_dense": row["counts"][2],
                        "n_interface": row["counts"][3],
                        "n_soil": row["counts"][4],
                    }
                )
    print(f"wrote {sens_path}")

    baseline_rows = results[BASELINE]
    base = {(row["date"], row["item_id"]): row for row in baseline_rows}

    # Control 1: baseline reproduces the reference gate run.
    gate_totals = load_gate_totals(REFERENCE_GATE_CSV)
    assert set(gate_totals) == set(base), "gate CSV scenes != reference scenes"
    control_deltas = [
        abs(row["total_ha"] - gate_totals[(row["date"], row["item_id"])])
        for row in baseline_rows
    ]
    control_max = max(control_deltas)
    print(
        f"control 1: baseline vs {REFERENCE_GATE_CSV.name}: "
        f"max |Δ total| = {control_max:.4f} ha "
        f"(rounds to {control_max:.2f} ha)"
    )

    # Control 2 + stats per non-baseline configuration.
    for (tau_water, tau_hyacinth), rows in results.items():
        if (tau_water, tau_hyacinth) == BASELINE:
            continue
        recent = [row for row in rows if row["date"] >= RECENT_FROM]
        n_px = changed_pixels(rows, base)
        print(
            f"config w={tau_water:g} h={tau_hyacinth:g}: "
            f"{n_px}/{len(rows)} scenes with changed pixel counts"
        )
        for title, subset in (("all", rows), (f">={RECENT_FROM}", recent)):
            for key in ("total_ha", "dense_ha", "interface_ha"):
                med, p95, mx, day = block_stats(subset, base, key)
                print(
                    f"  {title} {key}: median={med:.4f} p95={p95:.4f} "
                    f"max={mx:.4f} on {day}"
                )
            n_light, n_regime, n_either = changed_labels(subset, base)
            print(
                f"  {title} labels changed: light={n_light} "
                f"regime={n_regime} either={n_either}"
            )
    recent_n = sum(1 for day, _ in targets if day >= RECENT_FROM)
    print(f"recent subset: {recent_n} scenes from {RECENT_FROM} onward")

    # Spectral-cloud on/off for the haze scene and the control.
    cloud_rows: list[dict] = []
    spectral_orig = masks_mod.spectral_cloud
    for day, item_id in CLOUD_SCENES:
        on = measure_scene(item_id, day)
        with spectral_off():
            off = measure_scene(item_id, day)
        assert masks_mod.spectral_cloud is spectral_orig
        for label, row in (("on", on), ("off", off)):
            cloud_rows.append(
                {
                    "date": day,
                    "item_id": item_id,
                    "spectral": label,
                    "valid_frac": row["valid_frac"],
                    "cloud_frac": row["cloud_frac"],
                    "total_ha": row["total_ha"],
                    "dense_ha": row["dense_ha"],
                    "interface_ha": row["interface_ha"],
                    "light": row["light"],
                    "regime": row["regime"] or "clean",
                }
            )
        print(
            f"{day} spectral on: valid={on['valid_frac']:.4f} "
            f"cloud={on['cloud_frac']:.4f} total={on['total_ha']:.2f} "
            f"light={on['light']} regime={on['regime'] or 'clean'}"
        )
        print(
            f"{day} spectral off: valid={off['valid_frac']:.4f} "
            f"cloud={off['cloud_frac']:.4f} total={off['total_ha']:.2f} "
            f"light={off['light']} regime={off['regime'] or 'clean'}"
        )
    cloud_path = args.output_dir / "cloud.csv"
    with cloud_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CLOUD_FIELDS))
        writer.writeheader()
        for row in cloud_rows:
            writer.writerow(
                {
                    **row,
                    "valid_frac": repr(row["valid_frac"]),
                    "cloud_frac": repr(row["cloud_frac"]),
                    "total_ha": repr(row["total_ha"]),
                    "dense_ha": repr(row["dense_ha"]),
                    "interface_ha": repr(row["interface_ha"]),
                }
            )
    print(f"wrote {cloud_path}")


if __name__ == "__main__":
    main()
