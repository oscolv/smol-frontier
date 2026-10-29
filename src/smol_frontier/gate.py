"""Identity gate: ours vs the author's reference series (no bed correction).

Re-ingests reference scenes by exact Earth Search item id, runs
``classify()`` + quality light + regime on the RAW classifier output, and
compares against the author's prior Sentinel-2 series
(``tests/reference/serie_*.csv``). Areas on both sides are raw — the
exposed-bed correction never runs here.

Pure logic (label mapping, row comparison, summary stats, rendering) has no
I/O and is unit-tested offline. The live run is the ``smolf gate identity``
CLI, never a pytest test.
"""

import csv
import logging
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from smol_frontier.classify import pipeline
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.ingest import ingest_scene
from smol_frontier.quality import assess_quality
from smol_frontier.sources.stac import (
    EARTH_SEARCH,
    PLANETARY_COMPUTER,
    get_scene,
    search_scenes,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CSV_FIELDS",
    "GATE_SAMPLE_CSV",
    "OUTPUT_DIR",
    "PASS_MEDIAN_HA",
    "PASS_P95_HA",
    "ComparedScene",
    "GateSummary",
    "GateTarget",
    "MeasuredScene",
    "ReferenceRow",
    "RunMeta",
    "compare",
    "compared_error",
    "gate_timestamp",
    "load_reference",
    "load_sample_targets",
    "map_light",
    "map_regime",
    "measure_scene",
    "render_rows_table",
    "render_summary",
    "render_worst_table",
    "run_gate",
    "summarize",
    "write_csv",
    "write_markdown",
]

# WHY: fixed inputs — the author's reference series and the fixed 30-scene
#   gate sample. Both were added by hand and are never edited by the gate.
# HOW: repo layout anchored at PROJECT_ROOT.
REFERENCE_CSV = (
    PROJECT_ROOT / "tests/reference/serie_2017-01-01_2026-12-31_v2026-10-22.csv"
)
GATE_SAMPLE_CSV = PROJECT_ROOT / "tests/reference/gate_scenes.csv"
OUTPUT_DIR = PROJECT_ROOT / "outputs/identity_gate"

# WHY: pass criteria for the identity gate — the re-implementation must
#   reproduce the author's total hyacinth hectares within 10 ha (median)
#   and 25 ha (p95) on same-item scenes. Light/regime are reported, not
#   gated. Fixed per project brief (step 8c).
PASS_MEDIAN_HA = 10.0
PASS_P95_HA = 25.0

# WHY: the reference series labels are in Spanish (see the AGENTS.md
#   glossary); the gate maps them to our English labels for comparison.
LIGHT_ES_TO_EN = {"BUENA": "GOOD", "DUDOSA": "DOUBTFUL", "MALA": "POOR"}
REGIME_ES_TO_EN = {"LECHO": "EXPOSED_BED", "TURBIDEZ": "TURBIDITY", "": "clean"}

CSV_FIELDS = (
    "date",
    "item_id_requested",
    "stratum",
    "item_id_used",
    "backend_used",
    "fallback",
    "error",
    "note",
    "ref_dense_ha",
    "ours_dense_ha",
    "delta_dense_ha",
    "ref_interface_ha",
    "ours_interface_ha",
    "delta_interface_ha",
    "ref_total_ha",
    "ours_total_ha",
    "delta_total_ha",
    "abs_delta_total_ha",
    "ref_water_ha",
    "ours_water_ha",
    "delta_water_ha",
    "ref_soil_ha",
    "ours_soil_ha",
    "delta_soil_ha",
    "ref_valid_frac",
    "ours_valid_frac",
    "delta_valid_frac",
    "ref_light",
    "ours_light",
    "light_match",
    "ref_regime",
    "ours_regime",
    "regime_match",
)


def map_light(label: str) -> str:
    """Map a Spanish reference light (BUENA/DUDOSA/MALA) to English."""
    try:
        return LIGHT_ES_TO_EN[label.strip()]
    except KeyError:
        msg = f"unknown reference light: {label!r}"
        raise ValueError(msg) from None


def map_regime(label: str) -> str:
    """Map a Spanish reference regime (LECHO/TURBIDEZ/empty) to English."""
    try:
        return REGIME_ES_TO_EN[label.strip()]
    except KeyError:
        msg = f"unknown reference regime: {label!r}"
        raise ValueError(msg) from None


@dataclass(frozen=True)
class ReferenceRow:
    """One reference scene: raw hectares + labels, English-mapped."""

    date: date
    item_id: str
    dense_ha: float
    interface_ha: float
    total_ha: float
    water_ha: float
    soil_ha: float
    valid_frac: float
    light: str
    regime: str


@dataclass(frozen=True)
class GateTarget:
    """One gate scene: reference row + sample stratum ("" in --all mode)."""

    ref: ReferenceRow
    stratum: str = ""


@dataclass(frozen=True)
class MeasuredScene:
    """Our raw pipeline output for one scene (no bed correction)."""

    item_id: str
    backend: str
    fallback: bool
    note: str
    dense_ha: float
    interface_ha: float
    total_ha: float
    water_ha: float
    soil_ha: float
    valid_frac: float
    light: str
    regime: str


@dataclass(frozen=True)
class ComparedScene:
    """Reference vs ours for one scene, with deltas (ours − ref)."""

    date: str
    item_id_requested: str
    stratum: str
    item_id_used: str
    backend_used: str
    fallback: bool
    error: str
    note: str
    ref_dense_ha: float
    ours_dense_ha: float
    ref_interface_ha: float
    ours_interface_ha: float
    ref_total_ha: float
    ours_total_ha: float
    ref_water_ha: float
    ours_water_ha: float
    ref_soil_ha: float
    ours_soil_ha: float
    ref_valid_frac: float
    ours_valid_frac: float
    ref_light: str
    ours_light: str
    ref_regime: str
    ours_regime: str

    @property
    def delta_dense_ha(self) -> float:
        """Ours minus reference dense hectares."""
        return self.ours_dense_ha - self.ref_dense_ha

    @property
    def delta_interface_ha(self) -> float:
        """Ours minus reference interface hectares."""
        return self.ours_interface_ha - self.ref_interface_ha

    @property
    def delta_total_ha(self) -> float:
        """Ours minus reference total hyacinth hectares."""
        return self.ours_total_ha - self.ref_total_ha

    @property
    def abs_delta_total_ha(self) -> float:
        """Absolute total-hectare difference (the gated quantity)."""
        return abs(self.delta_total_ha)

    @property
    def delta_water_ha(self) -> float:
        """Ours minus reference water hectares."""
        return self.ours_water_ha - self.ref_water_ha

    @property
    def delta_soil_ha(self) -> float:
        """Ours minus reference soil hectares."""
        return self.ours_soil_ha - self.ref_soil_ha

    @property
    def delta_valid_frac(self) -> float:
        """Ours minus reference valid fraction."""
        return self.ours_valid_frac - self.ref_valid_frac

    @property
    def light_match(self) -> bool:
        """Whether our light equals the reference light."""
        return self.ours_light == self.ref_light

    @property
    def regime_match(self) -> bool:
        """Whether our regime equals the reference regime."""
        return self.ours_regime == self.ref_regime


def compare(
    ref: ReferenceRow, measured: MeasuredScene, stratum: str = ""
) -> ComparedScene:
    """Compare our measurement against the reference row (pure)."""
    return ComparedScene(
        date=ref.date.isoformat(),
        item_id_requested=ref.item_id,
        stratum=stratum,
        item_id_used=measured.item_id,
        backend_used=measured.backend,
        fallback=measured.fallback,
        error="",
        note=measured.note,
        ref_dense_ha=ref.dense_ha,
        ours_dense_ha=measured.dense_ha,
        ref_interface_ha=ref.interface_ha,
        ours_interface_ha=measured.interface_ha,
        ref_total_ha=ref.total_ha,
        ours_total_ha=measured.total_ha,
        ref_water_ha=ref.water_ha,
        ours_water_ha=measured.water_ha,
        ref_soil_ha=ref.soil_ha,
        ours_soil_ha=measured.soil_ha,
        ref_valid_frac=ref.valid_frac,
        ours_valid_frac=measured.valid_frac,
        ref_light=ref.light,
        ours_light=measured.light,
        ref_regime=ref.regime,
        ours_regime=measured.regime or "clean",
    )


def compared_error(day: str, item_id: str, stratum: str, error: str) -> ComparedScene:
    """Build a failed-scene row (NaN figures, excluded from stats)."""
    nan = math.nan
    return ComparedScene(
        date=day,
        item_id_requested=item_id,
        stratum=stratum,
        item_id_used="",
        backend_used="",
        fallback=False,
        error=error,
        note="",
        ref_dense_ha=nan,
        ours_dense_ha=nan,
        ref_interface_ha=nan,
        ours_interface_ha=nan,
        ref_total_ha=nan,
        ours_total_ha=nan,
        ref_water_ha=nan,
        ours_water_ha=nan,
        ref_soil_ha=nan,
        ours_soil_ha=nan,
        ref_valid_frac=nan,
        ours_valid_frac=nan,
        ref_light="",
        ours_light="",
        ref_regime="",
        ours_regime="",
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy 'linear' method)."""
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


@dataclass(frozen=True)
class BlockStats:
    """Summary stats for one block of scenes (same-item or fallback)."""

    n: int
    median_abs: float
    p95_abs: float
    max_abs: float
    max_abs_scene: str
    light_match: int
    regime_match: int


@dataclass(frozen=True)
class GateSummary:
    """Gate verdict + per-block stats. Errors fail the gate."""

    same_item: BlockStats
    fallback: BlockStats
    n_errors: int
    error_scenes: list[str]
    light_mismatches: list[ComparedScene]
    regime_mismatches: list[ComparedScene]
    passed: bool
    fail_reason: str


def _block_stats(rows: list[ComparedScene]) -> BlockStats:
    """Stats over non-error rows (NaN figures when empty)."""
    if not rows:
        return BlockStats(0, math.nan, math.nan, math.nan, "", 0, 0)
    abs_deltas = [row.abs_delta_total_ha for row in rows]
    worst = max(rows, key=lambda row: row.abs_delta_total_ha)
    return BlockStats(
        n=len(rows),
        median_abs=statistics.median(abs_deltas),
        p95_abs=_percentile(abs_deltas, 95.0),
        max_abs=worst.abs_delta_total_ha,
        max_abs_scene=f"{worst.date} {worst.item_id_requested}",
        light_match=sum(row.light_match for row in rows),
        regime_match=sum(row.regime_match for row in rows),
    )


def summarize(rows: list[ComparedScene]) -> GateSummary:
    """Summarize compared rows: same-item vs fallback blocks + verdict (pure).

    Pass criteria, computed on same-item scenes only: median |Δ total| ≤
    10 ha AND p95 ≤ 25 ha. Any failed scene fails the gate. An empty
    same-item block cannot pass.
    """
    ok = [row for row in rows if not row.error]
    same = [row for row in ok if not row.fallback]
    back = [row for row in ok if row.fallback]
    errors = [
        f"{row.date} {row.item_id_requested}: {row.error}" for row in rows if row.error
    ]
    same_stats = _block_stats(same)
    if errors:
        passed, reason = False, f"{len(errors)} failed scene(s)"
    elif not same:
        passed, reason = False, "no same-item scenes"
    elif same_stats.median_abs > PASS_MEDIAN_HA or same_stats.p95_abs > PASS_P95_HA:
        passed, reason = False, "Δ total over budget"
    else:
        passed, reason = True, ""
    return GateSummary(
        same_item=same_stats,
        fallback=_block_stats(back),
        n_errors=len(errors),
        error_scenes=errors,
        light_mismatches=[row for row in ok if not row.light_match],
        regime_mismatches=[row for row in ok if not row.regime_match],
        passed=passed,
        fail_reason=reason,
    )


@dataclass(frozen=True)
class RunMeta:
    """Run context for the report header."""

    timestamp: str
    mode: str
    n_requested: int
    workers: int
    wall_seconds: float


def _fmt_ha(value: float) -> str:
    """Format hectares (NaN → 'n/a')."""
    return "n/a" if math.isnan(value) else f"{value:.2f}"


def _fmt_block(title: str, stats: BlockStats, gated: bool) -> list[str]:
    """Render one stats block (same-item or fallback)."""
    lines = [f"## {title} (n={stats.n})" + ("" if gated else " — not gated")]
    if stats.n == 0:
        lines.append("none")
        return lines
    lines += [
        f"median |Δ total|: {_fmt_ha(stats.median_abs)} ha",
        f"p95 |Δ total|: {_fmt_ha(stats.p95_abs)} ha",
        f"max |Δ total|: {_fmt_ha(stats.max_abs)} ha ({stats.max_abs_scene})",
        f"light agreement: {stats.light_match}/{stats.n}",
        f"regime agreement: {stats.regime_match}/{stats.n}",
    ]
    return lines


def render_summary(summary: GateSummary, meta: RunMeta) -> str:
    """Render the gate report header: verdict, stats, mismatches (pure)."""
    wall = time.strftime("%H:%M:%S", time.gmtime(meta.wall_seconds))
    lines = [
        f"# Identity gate — {meta.timestamp}",
        "",
        f"mode: {meta.mode} ({meta.n_requested} scenes requested)",
        f"reference: {REFERENCE_CSV.name}",
        f"workers: {meta.workers}, wall time: {wall}",
        f"fallback backend scenes: {summary.fallback.n}",
        f"failed scenes: {summary.n_errors}",
        "",
        f"## Verdict: {'PASS' if summary.passed else 'FAIL'}"
        + ("" if summary.passed else f" ({summary.fail_reason})"),
        (
            f"criteria on same-item scenes: median |Δ total| ≤ {PASS_MEDIAN_HA:.0f} ha"
            f" AND p95 ≤ {PASS_P95_HA:.0f} ha"
        ),
        "",
        *_fmt_block("Same-item scenes", summary.same_item, gated=True),
        "",
        *_fmt_block("Fallback scenes", summary.fallback, gated=False),
        "",
        f"## Light mismatches ({len(summary.light_mismatches)})",
    ]
    for row in summary.light_mismatches:
        lines.append(
            f"- {row.date} {row.item_id_requested}: ref={row.ref_light}"
            f" ours={row.ours_light} (ref_total={row.ref_total_ha:.2f}"
            f" ours_total={row.ours_total_ha:.2f} Δ={row.delta_total_ha:+.2f})"
        )
    lines.append("")
    lines.append(f"## Regime mismatches ({len(summary.regime_mismatches)})")
    for row in summary.regime_mismatches:
        lines.append(
            f"- {row.date} {row.item_id_requested}: ref={row.ref_regime}"
            f" ours={row.ours_regime} (ref_total={row.ref_total_ha:.2f}"
            f" ours_total={row.ours_total_ha:.2f} Δ={row.delta_total_ha:+.2f})"
        )
    lines.append("")
    lines.append(f"## Errors ({summary.n_errors})")
    lines += [f"- {scene}" for scene in summary.error_scenes] or ["none"]
    return "\n".join(lines) + "\n"


def render_worst_table(rows: list[ComparedScene], n: int = 15) -> str:
    """Render the top-n worst scenes by |Δ total| (pure)."""
    ok = sorted(
        (row for row in rows if not row.error),
        key=lambda row: row.abs_delta_total_ha,
        reverse=True,
    )[:n]
    lines = [f"## Worst scenes by |Δ total| (top {n})", ""]
    if not ok:
        return "\n".join([*lines, "none"]) + "\n"
    lines += [
        "| date | item | backend | ref | ours | Δ |",
        "|---|---|---|---|---|---|",
    ]
    for row in ok:
        lines.append(
            f"| {row.date} | {row.item_id_requested} | {row.backend_used} "
            f"{'(fallback)' if row.fallback else ''} | {row.ref_total_ha:.2f}"
            f" | {row.ours_total_ha:.2f} | {row.delta_total_ha:+.2f} |"
        )
    return "\n".join(lines) + "\n"


def render_rows_table(rows: list[ComparedScene]) -> str:
    """Render the full per-scene table (pure)."""
    lines = [
        "## Per-scene",
        "",
        "| date | item requested | backend used | ref | ours | Δ | light | regime |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if row.error:
            lines.append(
                f"| {row.date} | {row.item_id_requested} | ERROR | — | — | — | — | — |"
            )
        else:
            fb = " (fallback)" if row.fallback else ""
            lines.append(
                f"| {row.date} | {row.item_id_requested} | {row.backend_used}{fb}"
                f" | {row.ref_total_ha:.2f} | {row.ours_total_ha:.2f}"
                f" | {row.delta_total_ha:+.2f} | {row.ref_light}/{row.ours_light}"
                f" | {row.ref_regime}/{row.ours_regime} |"
            )
    return "\n".join(lines) + "\n"


def load_reference(path: Path | None = None) -> list[ReferenceRow]:
    """Load the author's reference series (labels mapped to English)."""
    path = REFERENCE_CSV if path is None else Path(path)
    rows: list[ReferenceRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for lineno, raw in enumerate(csv.DictReader(handle), start=2):
            try:
                rows.append(
                    ReferenceRow(
                        date=date.fromisoformat((raw["fecha"] or "").strip()),
                        item_id=(raw["item_id"] or "").strip(),
                        dense_ha=float(raw["lirio_denso_ha"]),
                        interface_ha=float(raw["interfase_ha"]),
                        total_ha=float(raw["lirio_total_ha"]),
                        water_ha=float(raw["agua_ha"]),
                        soil_ha=float(raw["suelo_ha"]),
                        valid_frac=float(raw["valid_frac"]),
                        light=map_light(raw["quality"] or ""),
                        regime=map_regime(raw["regimen"] or ""),
                    )
                )
            except (KeyError, ValueError) as exc:
                msg = f"{path}:{lineno}: bad reference row: {exc}"
                raise ValueError(msg) from exc
    return rows


def load_sample_targets(
    reference: list[ReferenceRow], path: Path | None = None
) -> tuple[list[GateTarget], list[ComparedScene]]:
    """Join the fixed 30-scene sample with the reference series.

    Returns (targets, errors): sample items missing from the reference
    become error rows instead of targets.
    """
    path = GATE_SAMPLE_CSV if path is None else Path(path)
    by_item = {row.item_id: row for row in reference}
    targets: list[GateTarget] = []
    errors: list[ComparedScene] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            day, item_id = (raw["date"] or "").strip(), (raw["item_id"] or "").strip()
            ref = by_item.get(item_id)
            if ref is None:
                errors.append(compared_error(day, item_id, "", "not in reference"))
            else:
                targets.append(
                    GateTarget(ref=ref, stratum=(raw["stratum"] or "").strip())
                )
    return targets, errors


def _tile_of_earth_search_id(item_id: str) -> str:
    """MGRS tile from an Earth Search id (S2A_14QMH_20240609_0_L2A → 14QMH)."""
    parts = item_id.split("_")
    return parts[1] if len(parts) >= 3 else ""


def _fallback_scene(day: date, item_id: str):
    """Planetary Computer scene of the same date and tile (may raise)."""
    scenes = search_scenes(day, day, 100.0, backends=(PLANETARY_COMPUTER,))
    if not scenes:
        msg = f"no {PLANETARY_COMPUTER} scene for {day}"
        raise RuntimeError(msg)
    tile = _tile_of_earth_search_id(item_id)
    for scene in scenes:
        if tile and f"T{tile}" in scene.id:
            return scene
    return scenes[0]


def _measure_scene_meta(scene) -> MeasuredScene:
    """Classify one ingested scene: raw hectares + light + regime."""
    ing = ingest_scene(scene)
    result = pipeline.classify(ing)
    quality = assess_quality(result)
    ha = result.ha_per_class
    return MeasuredScene(
        item_id=scene.id,
        backend=scene.source,
        fallback=scene.source != EARTH_SEARCH,
        note="",
        dense_ha=ha[settings.CLASS_DENSE_HYACINTH],
        interface_ha=ha[settings.CLASS_INTERFACE],
        total_ha=result.hyacinth_total_ha,
        water_ha=ha[settings.CLASS_WATER],
        soil_ha=ha[settings.CLASS_BARE_SOIL],
        valid_frac=result.valid_fraction,
        light=quality.light,
        regime=quality.regime,
    )


def measure_scene(target: GateTarget) -> ComparedScene:
    """Ingest + classify + compare one gate scene (never raises).

    Uses the reference Earth Search item id; when Earth Search fails,
    falls back to the Planetary Computer scene of the same date and tile
    and records the backend change. Any failure becomes an error row.
    """
    ref = target.ref
    try:
        try:
            scene = get_scene(EARTH_SEARCH, ref.item_id)
            note = ""
        except Exception as exc:  # noqa: BLE001 — fallback is the contract
            logger.warning(
                "earth-search failed for %s (%r); falling back to %s",
                ref.item_id,
                exc,
                PLANETARY_COMPUTER,
            )
            scene = _fallback_scene(ref.date, ref.item_id)
            note = f"earth-search failed ({exc}); used {scene.id}"
        measured = _measure_scene_meta(scene)
        return compare(ref, replace(measured, note=note), stratum=target.stratum)
    except Exception as exc:  # noqa: BLE001 — a failed scene is skipped, not fatal
        logger.warning("gate scene %s failed: %r", ref.item_id, exc)
        row = compared_error(
            ref.date.isoformat(), ref.item_id, target.stratum, str(exc)[:300]
        )
        return replace(
            row,
            ref_dense_ha=ref.dense_ha,
            ref_interface_ha=ref.interface_ha,
            ref_total_ha=ref.total_ha,
            ref_water_ha=ref.water_ha,
            ref_soil_ha=ref.soil_ha,
            ref_valid_frac=ref.valid_frac,
            ref_light=ref.light,
            ref_regime=ref.regime,
        )


def run_gate(
    targets: list[GateTarget],
    workers: int = 4,
    on_done=None,
) -> list[ComparedScene]:
    """Measure every target concurrently; scenes already cached skip fetch.

    ``on_done`` (optional) is called in the caller thread with each finished
    row, for progress output. Returns rows sorted by (date, item).
    """
    rows: list[ComparedScene] = []
    if not targets:
        return rows
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(measure_scene, target): target for target in targets}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            if on_done is not None:
                on_done(row)
    rows.sort(key=lambda row: (row.date, row.item_id_requested))
    return rows


def _csv_value(value: object) -> str:
    """Format one CSV cell (NaN/None → '', bools → true/false)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.4f}"
    return str(value)


def write_csv(path: Path, rows: list[ComparedScene]) -> None:
    """Write one row per scene (deltas are ours − ref)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            record = {
                "date": row.date,
                "item_id_requested": row.item_id_requested,
                "stratum": row.stratum,
                "item_id_used": row.item_id_used,
                "backend_used": row.backend_used,
                "fallback": row.fallback,
                "error": row.error,
                "note": row.note,
                "ref_dense_ha": row.ref_dense_ha,
                "ours_dense_ha": row.ours_dense_ha,
                "delta_dense_ha": row.delta_dense_ha,
                "ref_interface_ha": row.ref_interface_ha,
                "ours_interface_ha": row.ours_interface_ha,
                "delta_interface_ha": row.delta_interface_ha,
                "ref_total_ha": row.ref_total_ha,
                "ours_total_ha": row.ours_total_ha,
                "delta_total_ha": row.delta_total_ha,
                "abs_delta_total_ha": row.abs_delta_total_ha,
                "ref_water_ha": row.ref_water_ha,
                "ours_water_ha": row.ours_water_ha,
                "delta_water_ha": row.delta_water_ha,
                "ref_soil_ha": row.ref_soil_ha,
                "ours_soil_ha": row.ours_soil_ha,
                "delta_soil_ha": row.delta_soil_ha,
                "ref_valid_frac": row.ref_valid_frac,
                "ours_valid_frac": row.ours_valid_frac,
                "delta_valid_frac": row.delta_valid_frac,
                "ref_light": row.ref_light,
                "ours_light": row.ours_light,
                "light_match": row.light_match if not row.error else "",
                "ref_regime": row.ref_regime,
                "ours_regime": row.ours_regime,
                "regime_match": row.regime_match if not row.error else "",
            }
            writer.writerow({key: _csv_value(val) for key, val in record.items()})


def write_markdown(path: Path, text: str) -> None:
    """Write the gate report markdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def gate_timestamp() -> str:
    """Timestamp shared by the run's CSV and markdown (local time)."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
