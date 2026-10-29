"""Resumable multi-scene series builder with a per-run manifest.

Default mode re-ingests every row of the reference series by exact Earth
Search item id (Planetary Computer fallback per scene, reusing the identity
gate's ingest-by-item code), then runs ``classify()`` + quality light +
regime + the exposed-bed correction. ``--extend`` additionally searches
Earth Search for scenes after the last reference date.

Scenes with valid_frac below MIN_VALID_FRACTION (light POOR) never enter
``series.csv``; they are recorded in the manifest as skipped. Failed scenes
become error entries and never stop the run.

Layout per run: ``outputs/series/<run_id>/`` holds one JSON per finished
scene under ``scenes/`` (written as soon as the scene finishes),
``series.csv`` (assembled at the end), and ``manifest.json`` (written at
start with status "running" and rewritten at the end as "complete", so an
interrupted run can be resumed). A series is identified by the sha256 of
its ``series.csv``, not by date.
"""

import csv
import hashlib
import json
import logging
import platform
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from smol_frontier import __version__
from smol_frontier.classify.pipeline import classify as classify_scene
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.gate import REFERENCE_CSV, load_reference, resolve_scene
from smol_frontier.hydro.bed import correct_bed
from smol_frontier.hydro.curve import DEFAULT_CURVE_CSV_PATH
from smol_frontier.hydro.flood_map import DEFAULT_FLOOD_PATH
from smol_frontier.hydro.levels import DEFAULT_LEVELS_PATH
from smol_frontier.ingest import DEFAULT_CACHE_DIR, _cache_path, ingest_scene
from smol_frontier.quality import assess_quality
from smol_frontier.sources.stac import EARTH_SEARCH, search_scenes

logger = logging.getLogger(__name__)

__all__ = [
    "OUTPUT_DIR",
    "SERIES_CSV_NAME",
    "SERIES_FIELDS",
    "FingerprintMismatch",
    "SeriesTarget",
    "build_run",
    "cache_sha256",
    "check_fingerprint",
    "config_hash",
    "current_fingerprint",
    "input_hashes",
    "list_extend_targets",
    "list_reference_targets",
    "new_run_id",
    "process_scene",
    "utc_now",
    "write_series_csv",
]

# WHY: fixed run layout — one directory per run keeps scene JSONs, the series
#   CSV, and the manifest together so a run is resumable and auditable alone.
OUTPUT_DIR = PROJECT_ROOT / "outputs/series"
SERIES_CSV_NAME = "series.csv"

# WHY: fixed column order for series.csv — raw classifier figures first, then
#   the bed-correction inputs and corrected figures (empty when not applied).
SERIES_FIELDS = (
    "date",
    "item_id_requested",
    "item_id_used",
    "backend_used",
    "fallback",
    "tile_cloud_pct",
    "light",
    "regime",
    "valid_frac",
    "cloud_frac",
    "dense_ha",
    "interface_ha",
    "hyacinth_raw_ha",
    "water_ha",
    "soil_ha",
    "pct_reservoir_raw",
    "bed_applied",
    "bed_reason",
    "level_m",
    "margin_m",
    "expected_bed_ha",
    "removed_ha",
    "dense_corrected_ha",
    "interface_corrected_ha",
    "hyacinth_corrected_ha",
    "pct_reservoir_corrected",
)

_FLOAT_4DP = frozenset({"valid_frac", "cloud_frac"})


class FingerprintMismatch(Exception):
    """Resume refused: the run's code fingerprint differs from the current one."""


@dataclass(frozen=True)
class SeriesTarget:
    """One scene to build: day + exact Earth Search item id."""

    day: date
    item_id: str


def utc_now() -> str:
    """Current UTC time as an ISO-8601 'Z' string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """Fresh run id: local timestamp, same scheme as the identity gate."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _freeze(value: object) -> object:
    """Convert settings values to JSON-safe primitives (Path/date/tuple)."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_freeze(item) for item in value]
    if isinstance(value, dict):
        return {key: _freeze(item) for key, item in value.items()}
    return value


def config_hash() -> str:
    """sha256 of a canonical JSON dump of the settings object (pure)."""
    canonical = json.dumps(
        _freeze(asdict(settings)), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    """Run a git command at the repo root, returning stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"git {' '.join(args)} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def current_fingerprint() -> dict[str, object]:
    """Code fingerprint: git commit + dirty flag + config hash."""
    return {
        "git_commit": _git("rev-parse", "HEAD").strip(),
        "dirty": bool(_git("status", "--porcelain").strip()),
        "config_hash": config_hash(),
    }


def check_fingerprint(
    run_code: dict[str, object], current: dict[str, object], run_id: str
) -> None:
    """Refuse a resume when any fingerprint component differs (pure check).

    Raises FingerprintMismatch naming every component that differs.
    """
    diffs = [
        f"{key} differs (run {run_code.get(key)!r} vs current {current[key]!r})"
        for key in ("git_commit", "dirty", "config_hash")
        if run_code.get(key) != current[key]
    ]
    if diffs:
        msg = f"refusing resume of run {run_id}: " + "; ".join(diffs)
        raise FingerprintMismatch(msg)


def sha256_file(path: Path) -> str | None:
    """sha256 hex of a file, or None when it does not exist."""
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_hashes() -> dict[str, str | None]:
    """sha256 of every series input file (None when missing)."""
    return {
        "aoi_polygon": sha256_file(settings.AOI_PATH),
        "ENDHD_csv": sha256_file(DEFAULT_LEVELS_PATH),
        "stage_area_curve_csv": sha256_file(DEFAULT_CURVE_CSV_PATH),
        "flood_stage_tif": sha256_file(DEFAULT_FLOOD_PATH),
        "reference_csv": sha256_file(REFERENCE_CSV),
    }


def cache_sha256(item_id: str) -> str | None:
    """sha256 of the cached scene file, or None when not cached."""
    if not item_id:
        return None
    return sha256_file(_cache_path(DEFAULT_CACHE_DIR, item_id))


def list_reference_targets() -> list[SeriesTarget]:
    """Every reference-series row as a (day, item id) target."""
    return [SeriesTarget(day=row.date, item_id=row.item_id) for row in load_reference()]


def list_extend_targets(last_ref_day: date, today: date) -> list[SeriesTarget]:
    """Earth Search scenes after the last reference date, up to today.

    One scene per date (the catalog search already deduplicates to the
    latest processing per date and tile) with tile cloud cover under
    MAX_CLOUD_COVER_CATALOG. Empty when today holds no new date.
    """
    if today <= last_ref_day:
        return []
    scenes = search_scenes(
        last_ref_day + timedelta(days=1),
        today,
        settings.MAX_CLOUD_COVER_CATALOG,
        backends=(EARTH_SEARCH,),
    )
    return [SeriesTarget(day=item.datetime.date(), item_id=item.id) for item in scenes]


def _short(exc: Exception) -> str:
    """Error message truncated like the gate's (300 chars)."""
    return str(exc)[:300]


def _base_record(
    target: SeriesTarget,
    item_id_used: str,
    backend_used: str,
    tile_cloud_pct: float | None,
    status: str,
) -> dict[str, object]:
    """Record shared fields: ids, backend, fallback, cloud, status."""
    return {
        "date": target.day.isoformat(),
        "item_id_requested": target.item_id,
        "item_id_used": item_id_used,
        "backend_used": backend_used,
        "fallback": bool(backend_used) and backend_used != EARTH_SEARCH,
        "tile_cloud_pct": tile_cloud_pct,
        "status": status,
        "valid_frac": None,
        "error": "",
        "cache_sha256": cache_sha256(item_id_used),
        "row": None,
    }


def _error_record(
    target: SeriesTarget,
    item_id_used: str,
    backend_used: str,
    tile_cloud_pct: float | None,
    message: str,
) -> dict[str, object]:
    """Record for a failed scene (never stops the run)."""
    record = _base_record(target, item_id_used, backend_used, tile_cloud_pct, "error")
    record["error"] = message
    return record


def process_scene(target: SeriesTarget) -> dict[str, object]:
    """Build one scene record: resolve → ingest → classify → quality → bed.

    Returns a JSON-ready record (status ok | skipped | error). Never raises
    for scene failures — those become error records; only truly unexpected
    BaseExceptions (e.g. KeyboardInterrupt) propagate to interrupt the run.
    A POOR scene (valid_frac < MIN_VALID_FRACTION) is skipped before the
    bed correction and never enters series.csv.
    """
    day = target.day
    try:
        scene, _note = resolve_scene(day, target.item_id)
    except Exception as exc:  # noqa: BLE001 — failed scene, not a fatal error
        logger.warning("series scene %s failed to resolve: %r", target.item_id, exc)
        return _error_record(target, "", "", None, _short(exc))
    tile_cloud_pct = scene.cloud_cover
    try:
        ing = ingest_scene(scene)
        result = classify_scene(ing)
        quality = assess_quality(result)
    except Exception as exc:  # noqa: BLE001 — failed scene, not a fatal error
        logger.warning("series scene %s failed to classify: %r", target.item_id, exc)
        return _error_record(
            target, scene.id, scene.source, tile_cloud_pct, _short(exc)
        )
    if result.valid_fraction < settings.MIN_VALID_FRACTION:
        record = _base_record(target, scene.id, scene.source, tile_cloud_pct, "skipped")
        record["valid_frac"] = result.valid_fraction
        return record
    try:
        bed = correct_bed(result.class_map, ing.transform, ing.crs, day)
    except Exception as exc:  # noqa: BLE001 — failed scene, not a fatal error
        logger.warning("series scene %s failed bed correction: %r", target.item_id, exc)
        return _error_record(
            target, scene.id, scene.source, tile_cloud_pct, _short(exc)
        )
    ha = result.ha_per_class
    record = _base_record(target, scene.id, scene.source, tile_cloud_pct, "ok")
    record["valid_frac"] = result.valid_fraction
    # WHY "clean": the gate writes "clean" for an empty regime, so the series
    #   uses the same word and both CSVs compare directly.
    record["row"] = {
        "date": day.isoformat(),
        "item_id_requested": target.item_id,
        "item_id_used": scene.id,
        "backend_used": scene.source,
        "fallback": scene.source != EARTH_SEARCH,
        "tile_cloud_pct": tile_cloud_pct,
        "light": quality.light,
        "regime": quality.regime or "clean",
        "valid_frac": result.valid_fraction,
        "cloud_frac": result.cloud_fraction,
        "dense_ha": ha[settings.CLASS_DENSE_HYACINTH],
        "interface_ha": ha[settings.CLASS_INTERFACE],
        "hyacinth_raw_ha": result.hyacinth_total_ha,
        "water_ha": ha[settings.CLASS_WATER],
        "soil_ha": ha[settings.CLASS_BARE_SOIL],
        "pct_reservoir_raw": result.pct_reservoir,
        "bed_applied": bed.applied,
        "bed_reason": bed.reason,
        "level_m": bed.level_m,
        "margin_m": bed.margin_m,
        "expected_bed_ha": bed.expected_bed_ha,
        "removed_ha": bed.removed_ha,
        "dense_corrected_ha": bed.dense_corrected_ha,
        "interface_corrected_ha": bed.interface_corrected_ha,
        "hyacinth_corrected_ha": bed.hyacinth_corrected_ha,
        "pct_reservoir_corrected": bed.pct_corrected,
    }
    return record


def _scene_stem(item_id: str) -> str:
    """Scene JSON stem for an item id (slash-safe like the ingest cache)."""
    return item_id.replace("/", "_")


def _write_scene_json(scenes_dir: Path, record: dict[str, object]) -> Path:
    """Write one scene JSON at once (canonical: sorted keys, fixed indent)."""
    path = scenes_dir / f"{_scene_stem(str(record['item_id_requested']))}.json"
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _load_records(scenes_dir: Path) -> list[dict[str, object]]:
    """Load every scene JSON, sorted by (date, item id)."""
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in scenes_dir.glob("*.json")
    ]
    records.sort(key=lambda rec: (str(rec["date"]), str(rec["item_id_requested"])))
    return records


def _cell(column: str, value: object) -> str:
    """Format one CSV cell (None → '', bools → true/false, fixed decimals)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}" if column in _FLOAT_4DP else f"{value:.2f}"
    return str(value)


def write_series_csv(path: Path, ok_records: list[dict[str, object]]) -> None:
    """Write series.csv: one row per ok scene, fixed columns and formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SERIES_FIELDS))
        writer.writeheader()
        for record in ok_records:
            row = record["row"]
            assert isinstance(row, dict)
            writer.writerow({col: _cell(col, row.get(col)) for col in SERIES_FIELDS})


def _manifest_entry(record: dict[str, object]) -> dict[str, object]:
    """Per-scene manifest entry: the record without its series row."""
    return {key: value for key, value in record.items() if key != "row"}


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Write the run manifest (canonical JSON)."""
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _running_manifest(
    run_id: str,
    mode: str,
    targets: list[SeriesTarget],
    command: Sequence[str],
    started_utc: str,
    fingerprint: dict[str, object],
) -> dict[str, object]:
    """Manifest for a run in progress (no series sha yet)."""
    return {
        "run_id": run_id,
        "status": "running",
        "started_utc": started_utc,
        "finished_utc": None,
        "command": list(command),
        "mode": mode,
        "code": dict(fingerprint),
        "environment": {
            "package_version": __version__,
            "python_version": platform.python_version(),
            "uv_lock_sha256": sha256_file(PROJECT_ROOT / "uv.lock"),
        },
        "inputs": input_hashes(),
        "requested": [
            {"date": target.day.isoformat(), "item_id": target.item_id}
            for target in targets
        ],
        "scenes": [],
        "counts": {
            "requested": len(targets),
            "ok": 0,
            "skipped": 0,
            "errors": 0,
        },
        "series_csv": SERIES_CSV_NAME,
        "series_sha256": None,
    }


def _run_one(
    fn: Callable[[SeriesTarget], dict[str, object]], target: SeriesTarget
) -> dict[str, object]:
    """Run the per-scene function; a raising scene becomes an error record."""
    try:
        return fn(target)
    except Exception as exc:  # noqa: BLE001 — a failed scene never stops the run
        logger.warning("series scene %s raised: %r", target.item_id, exc)
        return _error_record(target, "", "", None, _short(exc))


def build_run(
    *,
    run_id: str,
    extend: bool = False,
    resume: bool = False,
    workers: int = 4,
    command: Sequence[str] | None = None,
    targets: list[SeriesTarget] | None = None,
    process_fn: Callable[[SeriesTarget], dict[str, object]] | None = None,
    output_dir: Path | None = None,
    today: date | None = None,
    on_done: Callable[[dict[str, object], int, int], None] | None = None,
) -> dict[str, object]:
    """Build (or resume) a series run; return the final manifest.

    Fresh runs write the manifest with status "running" first, then one
    scene JSON per finished scene, then series.csv and the "complete"
    manifest. Resumes reuse the scene JSONs already present and process
    only the missing targets — after the code fingerprint check.
    Raises FingerprintMismatch (resume with changed code/config),
    FileNotFoundError (resume of an unknown run), FileExistsError (fresh
    build over an existing run id), ValueError (workers < 1).
    """
    if workers < 1:
        msg = "--workers must be >= 1"
        raise ValueError(msg)
    fn = process_scene if process_fn is None else process_fn
    # WHY late binding: tests point OUTPUT_DIR at a tmp dir, so the default
    #   resolves at call time, not at import time.
    out_root = OUTPUT_DIR if output_dir is None else Path(output_dir)
    run_dir = out_root / run_id
    scenes_dir = run_dir / "scenes"
    manifest_path = run_dir / "manifest.json"
    fingerprint = current_fingerprint()
    if resume:
        if not manifest_path.exists():
            msg = f"unknown run: {run_id}"
            raise FileNotFoundError(msg)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check_fingerprint(manifest["code"], fingerprint, run_id)
        mode = str(manifest["mode"])
        targets = [
            SeriesTarget(date.fromisoformat(entry["date"]), entry["item_id"])
            for entry in manifest["requested"]
        ]
        started_utc = str(manifest["started_utc"])
        have = {path.stem for path in scenes_dir.glob("*.json")}
    else:
        if manifest_path.exists():
            msg = f"run exists: {run_id} (use --resume)"
            raise FileExistsError(msg)
        mode = "reference+extend" if extend else "reference"
        if targets is None:
            targets = list_reference_targets()
            if extend:
                last_day = max(target.day for target in targets)
                targets = targets + list_extend_targets(last_day, today or date.today())
        started_utc = utc_now()
        have = set()
        scenes_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(
            manifest_path,
            _running_manifest(
                run_id, mode, targets, command or sys.argv, started_utc, fingerprint
            ),
        )
    pending = [target for target in targets if _scene_stem(target.item_id) not in have]
    total = len(targets)
    done = total - len(pending)
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, fn, target): target for target in pending}
            try:
                for future in as_completed(futures):
                    record = future.result()
                    _write_scene_json(scenes_dir, record)
                    done += 1
                    if on_done is not None:
                        on_done(record, done, total)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    records = _load_records(scenes_dir)
    ok_records = [record for record in records if record["status"] == "ok"]
    series_path = run_dir / SERIES_CSV_NAME
    write_series_csv(series_path, ok_records)
    if resume:
        # WHY: a resumed run keeps the original invocation, code, and inputs —
        #   the fingerprint check above already proved the code matches.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = _running_manifest(
            run_id, mode, targets, command or sys.argv, started_utc, fingerprint
        )
    manifest["status"] = "complete"
    manifest["finished_utc"] = utc_now()
    manifest["scenes"] = [_manifest_entry(record) for record in records]
    manifest["counts"] = {
        "requested": total,
        "ok": sum(record["status"] == "ok" for record in records),
        "skipped": sum(record["status"] == "skipped" for record in records),
        "errors": sum(record["status"] == "error" for record in records),
    }
    manifest["series_sha256"] = sha256_file(series_path)
    _write_manifest(manifest_path, manifest)
    return manifest
