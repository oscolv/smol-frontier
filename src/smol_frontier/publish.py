"""Publish a versioned data contract derived from one completed series run.

``publish_run`` picks a run (``--run`` or the newest complete one), refuses
runs that cannot back a reproducible contract (incomplete, dirty, no scene
with figures), maps the run's scene records to the English contract fields,
validates every document against its packaged JSON Schema, re-classifies
the latest scene from the hash-verified scene cache into the published map
(classes 0-5, class 5 = removed by the exposed-bed correction), and swaps
the output directory in atomically. The run manifest is provenance, never
contract: nothing is copied out of it verbatim.

Contract files are canonical JSON (sorted keys, 2-space indent, UTF-8,
trailing newline) with no wall-clock timestamp, so the same run yields
byte-identical files.
"""

import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from importlib import resources
from pathlib import Path

import numpy as np
import rasterio
from jsonschema import Draft202012Validator
from rasterio.crs import CRS

from smol_frontier import __version__, aoi, ingest, series
from smol_frontier.classify.geotiff import write_published_geotiff
from smol_frontier.classify.pipeline import classify as classify_scene
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.hydro import flood_map, levels
from smol_frontier.hydro.bed import exposed_bed_mask
from smol_frontier.ingest import ingest_scene
from smol_frontier.sources.stac import SceneMeta, get_scene

__all__ = [
    "ALLOWED_FILES",
    "ALLOWED_MAP_PATTERN",
    "OUTPUT_DIR",
    "SCHEMA_VERSION",
    "ContractInvalid",
    "PublishRefused",
    "PublishedMap",
    "build_map",
    "canonical_json",
    "current_level",
    "is_allowed",
    "latest_document",
    "latest_record",
    "map_path",
    "publish_run",
    "select_run",
    "series_document",
    "validate",
]

# WHY: default contract location, beside the series runs it derives from;
#   `--out` overrides it.
OUTPUT_DIR = PROJECT_ROOT / "outputs/publish"

# WHY: contract version carried by every document and pinned with `const` in
#   each schema. Bump the major on any breaking change (rename/remove a field,
#   change a type, unit or meaning), the minor on additive ones.
SCHEMA_VERSION = "1.0.0"

# WHY: allowlist — the only files `publish` may ever write, so nothing
#   unexpected (e.g. source imagery) can reach the published directory.
ALLOWED_FILES = frozenset({"latest.json", "series.json"})
# WHY: the only other files allowed — the latest scene's map, one level deep
#   under map/, named <date>_<item_id> (no path separators in the id).
ALLOWED_MAP_PATTERN = re.compile(r"map/[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+\.tif")

# WHY: the classified hectares publish re-derives must equal the run row to
#   the hundredth (the published decimals) — (class code, run row field).
_HECTARE_CHECKS = (
    (settings.CLASS_DENSE_HYACINTH, "dense_ha"),
    (settings.CLASS_INTERFACE, "interface_ha"),
    (settings.CLASS_WATER, "water_ha"),
    (settings.CLASS_BARE_SOIL, "soil_ha"),
)

# WHY: contract decimals follow series.csv — hectares, percentages and water
#   levels to 2 decimals, fractions to 4.
_DECIMALS_FRACTION = 4
_DECIMALS_DEFAULT = 2

# WHY: the run stores "clean" for an empty regime (same word as the gate CSV);
#   the contract publishes an explicit enum.
_REGIME = {
    "clean": "CLEAN",
    "": "CLEAN",
    "EXPOSED_BED": "EXPOSED_BED",
    "TURBIDITY": "TURBIDITY",
}

# WHY: per-scene fields copied from the run row under the same (series.csv)
#   name, with their contract decimals (None: not a number).
_SCENE_FIELDS = (
    ("valid_frac", _DECIMALS_FRACTION),
    ("cloud_frac", _DECIMALS_FRACTION),
    ("dense_ha", _DECIMALS_DEFAULT),
    ("interface_ha", _DECIMALS_DEFAULT),
    ("hyacinth_raw_ha", _DECIMALS_DEFAULT),
    ("pct_reservoir_raw", _DECIMALS_DEFAULT),
    ("bed_applied", None),
    ("bed_reason", None),
    ("level_m", _DECIMALS_DEFAULT),
    ("removed_ha", _DECIMALS_DEFAULT),
    ("dense_corrected_ha", _DECIMALS_DEFAULT),
    ("interface_corrected_ha", _DECIMALS_DEFAULT),
    ("hyacinth_corrected_ha", _DECIMALS_DEFAULT),
    ("pct_reservoir_corrected", _DECIMALS_DEFAULT),
)

# WHY: figures that exist only when the exposed-bed correction was applied;
#   otherwise they are published as null and never replaced by raw figures.
_CORRECTED_FIELDS = (
    "dense_corrected_ha",
    "interface_corrected_ha",
    "hyacinth_corrected_ha",
    "pct_reservoir_corrected",
)


class PublishRefused(Exception):
    """Precondition refusal (exit 2): the run cannot back a contract."""


class ContractInvalid(Exception):
    """smolf's own output failed its schema (exit 1): a bug, never published."""


@dataclass(frozen=True, eq=False)
class PublishedMap:
    """The published class map (0-5) on the scene's native grid."""

    class_map: np.ndarray
    transform: rasterio.Affine
    crs: str


def is_allowed(name: str) -> bool:
    """True for a relative path `publish` may write (the C6 allowlist)."""
    return name in ALLOWED_FILES or ALLOWED_MAP_PATTERN.fullmatch(name) is not None


def map_path(record: dict[str, object]) -> str:
    """Relative path of a scene's map GeoTIFF: ``map/<date>_<item_id>.tif``."""
    return f"map/{record['date']}_{record['item_id_used']}.tif"


@cache
def _validator(name: str) -> Draft202012Validator:
    """Validator for the packaged ``<name>.schema.json``."""
    text = (
        resources.files("smol_frontier")
        .joinpath("schemas", f"{name}.schema.json")
        .read_text(encoding="utf-8")
    )
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(name: str, document: dict[str, object]) -> None:
    """Validate a contract document; raise ContractInvalid listing every error."""
    errors = sorted(
        _validator(name).iter_errors(document), key=lambda err: list(err.path)
    )
    if errors:
        detail = "; ".join(
            f"{'/'.join(map(str, err.path)) or '<root>'}: {err.message}"
            for err in errors
        )
        msg = f"{name}.json fails its schema: {detail}"
        raise ContractInvalid(msg)


def canonical_json(document: dict[str, object]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _round(value: object, decimals: int | None) -> object:
    """Round a number like series.csv; pass None and non-numbers through."""
    if decimals is None or value is None or isinstance(value, bool):
        return value
    return round(float(value), decimals)


def _cover_band(pct_published: object) -> str | None:
    """GREEN/YELLOW/RED on the published (rounded) percentage; None stays None."""
    if pct_published is None:
        return None
    return settings.traffic_light(float(pct_published)).upper()


def _read_manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def select_run(series_root: Path, run_id: str | None) -> Path:
    """Run directory to publish: ``run_id``, or the newest complete run.

    Raises PublishRefused for an unknown run, no complete run, a run that is
    not complete, or a run built from a dirty tree.
    """
    if run_id is None:
        complete = [
            path.parent
            for path in sorted(series_root.glob("*/manifest.json"))
            if _read_manifest(path.parent).get("status") == "complete"
        ]
        if not complete:
            msg = f"no complete run under {series_root}"
            raise PublishRefused(msg)
        run_dir = complete[-1]
    else:
        run_dir = series_root / run_id
        if not (run_dir / "manifest.json").is_file():
            msg = f"unknown run: {run_id}"
            raise PublishRefused(msg)
    manifest = _read_manifest(run_dir)
    if manifest.get("status") != "complete":
        msg = f"run {run_dir.name} is not complete (status {manifest.get('status')})"
        raise PublishRefused(msg)
    code = manifest.get("code")
    if not isinstance(code, dict) or code.get("dirty") is not False:
        msg = f"run {run_dir.name} was built from a dirty tree (code.dirty is true)"
        raise PublishRefused(msg)
    return run_dir


def _newest_acquisition(records: list[dict[str, object]]) -> dict[str, object]:
    """Newest scene of the run with status ok or skipped (never error).

    A skipped scene is POOR by construction (valid_frac under the minimum);
    an ok scene carries the light of its row. Records are sorted by date.
    """
    seen = [record for record in records if record["status"] in ("ok", "skipped")]
    record = seen[-1]
    if record["status"] == "ok":
        row = record["row"]
        assert isinstance(row, dict)
        light = row["light"]
    else:
        light = "POOR"
    return {
        "date": record["date"],
        "item_id": record["item_id_used"],
        "quality_light": light,
        "valid_frac": _round(record["valid_frac"], _DECIMALS_FRACTION),
    }


def current_level(levels_path: Path | None = None) -> dict[str, object] | None:
    """Last day with a level in the SIH record, or None when there is none.

    Independent of the wall clock and of the run. A missing file counts as
    no record. ``levels_path`` defaults to the committed level record.
    """
    path = Path(levels.DEFAULT_LEVELS_PATH if levels_path is None else levels_path)
    if not path.is_file():
        return None
    rows = [row for row in levels.load_levels(path) if not math.isnan(row.level_m)]
    if not rows:
        return None
    last = max(rows, key=lambda row: row.date)
    return {
        "date": last.date.isoformat(),
        "level_m": round(last.level_m, _DECIMALS_DEFAULT),
        "levels_sha256": series.sha256_file(path),
    }


def _scene_entry(record: dict[str, object]) -> dict[str, object]:
    """Per-scene contract fields (C11) of an ``ok`` or ``skipped`` record.

    Shared by ``latest.json`` and ``series.json``. A skipped (POOR) scene has
    no row: it carries date, item_id, quality_light, valid_frac and null for
    every other field.
    """
    if record["status"] == "skipped":
        entry: dict[str, object] = {
            "date": record["date"],
            "item_id": record["item_id_used"],
            "quality_light": "POOR",
            "regime": None,
            "cover_band": None,
        }
        for field, decimals in _SCENE_FIELDS:
            entry[field] = None
        entry["valid_frac"] = _round(record["valid_frac"], _DECIMALS_FRACTION)
        return entry
    row = record["row"]
    assert isinstance(row, dict)
    entry = {
        "date": row["date"],
        "item_id": record["item_id_used"],
        "quality_light": row["light"],
        "regime": _REGIME.get(str(row["regime"]), row["regime"]),
    }
    for field, decimals in _SCENE_FIELDS:
        entry[field] = _round(row[field], decimals)
    if not row["bed_applied"]:
        for field in _CORRECTED_FIELDS:
            entry[field] = None
    entry["cover_band"] = _cover_band(entry["pct_reservoir_corrected"])
    return entry


def latest_document(
    records: list[dict[str, object]],
    manifest: dict[str, object],
    levels_path: Path | None = None,
) -> dict:
    """``latest.json`` for the last ``ok`` record (records sorted by date).

    Carries the run context from the manifest and the current water level.
    Raises PublishRefused when no record has figures.
    """
    run_id = str(manifest["run_id"])
    ok = [record for record in records if record["status"] == "ok"]
    if not ok:
        msg = f"run {run_id} has no scene with figures (no ok scene)"
        raise PublishRefused(msg)
    record = ok[-1]
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "smolf_version": __version__,
        **_scene_entry(record),
    }
    counts = manifest["counts"]
    assert isinstance(counts, dict)
    document["newest_acquisition"] = _newest_acquisition(records)
    document["run_counts"] = {key: counts[key] for key in ("ok", "skipped", "errors")}
    document["run_id"] = run_id
    document["series_sha256"] = manifest["series_sha256"]
    document["reservoir_area_ha"] = round(aoi.reservoir_area_ha(), _DECIMALS_DEFAULT)
    document["constants"] = {
        "frac_publishable": settings.FRAC_PUBLISHABLE,
        "green_max_pct": settings.GREEN_MAX_PCT,
        "red_min_pct": settings.RED_MIN_PCT,
    }
    document["current_level"] = current_level(levels_path)
    document["map"] = {"geotiff": map_path(record)}
    return document


def latest_record(records: list[dict[str, object]]) -> dict[str, object]:
    """The last ``ok`` record (the scene ``latest.json`` and the map describe)."""
    return [record for record in records if record["status"] == "ok"][-1]


def _cached_scene(record: dict[str, object]) -> ingest.IngestedScene:
    """The scene exactly as the run cached it, verified by sha256.

    A scene missing from the cache is re-ingested by its exact item id and
    backend (no search, no fallback: another item could never match the
    recorded hash). Either way the cache file must hash to the record's
    ``cache_sha256`` before it is read.
    """
    item_id = str(record["item_id_used"])
    backend = str(record["backend_used"])
    cache_dir = ingest.DEFAULT_CACHE_DIR
    path = ingest._cache_path(cache_dir, item_id)
    if not path.is_file():
        try:
            ingest_scene(get_scene(backend, item_id), cache_dir=cache_dir)
        except Exception as exc:  # WHY broad: any re-ingest failure refuses
            msg = f"scene {item_id} is not cached and re-ingest failed: {exc}"[:300]
            raise PublishRefused(msg) from exc
    expected = record.get("cache_sha256")
    actual = series.sha256_file(path)
    if actual is None or actual != expected:
        msg = (
            f"cache sha256 of {item_id} ({actual}) differs from the scene "
            f"record ({expected})"
        )
        raise PublishRefused(msg)
    # WHY a stub: on a cache hit ingest_scene rebuilds the scene metadata
    #   from the cache tags; only the id (cache key) is used from here.
    stub = SceneMeta(
        id=item_id,
        datetime=datetime.fromisoformat(str(record["date"])),
        source=backend,
        processing_baseline=None,
        boa_offset_applied=False,
        assets={},
    )
    return ingest_scene(stub, cache_dir=cache_dir)


def _flood_stage_for(
    manifest: dict[str, object], ing: ingest.IngestedScene, shape: tuple[int, ...]
) -> np.ndarray:
    """The flood-stage map the run used, on the scene's grid (else refuse)."""
    inputs = manifest.get("inputs")
    expected = inputs.get("flood_stage_tif") if isinstance(inputs, dict) else None
    path = flood_map.DEFAULT_FLOOD_PATH
    actual = series.sha256_file(path)
    if expected is None or actual != expected:
        msg = (
            f"flood-stage map sha256 ({actual}) differs from the run manifest "
            f"inputs ({expected})"
        )
        raise PublishRefused(msg)
    flood, transform, crs = flood_map.load_flood_map(path)
    if (
        flood.shape != shape
        or transform != ing.transform
        or CRS.from_string(crs) != CRS.from_string(ing.crs)
    ):
        msg = "flood-stage map grid differs from the scene grid"
        raise PublishRefused(msg)
    return flood


def build_map(record: dict[str, object], manifest: dict[str, object]) -> PublishedMap:
    """Re-classify the record's scene and overlay class 5 (C17-C19).

    Refuses (PublishRefused) when the cache hash, the per-class hectares,
    the flood-stage map hash or the removed hectares disagree with the run.
    Class 5 uses the run row's level and margin, never a fresh lookup, and
    only when the correction was applied.
    """
    row = record["row"]
    assert isinstance(row, dict)
    item_id = record["item_id_used"]
    ing = _cached_scene(record)
    result = classify_scene(ing)
    mismatches = [
        f"{field} {round(result.ha_per_class[code], 2)} != run "
        f"{round(float(row[field]), 2)}"
        for code, field in _HECTARE_CHECKS
        if round(result.ha_per_class[code], 2) != round(float(row[field]), 2)
    ]
    if mismatches:
        msg = f"re-classified {item_id} differs from the run: " + "; ".join(mismatches)
        raise PublishRefused(msg)
    # WHY no dtype cast: the guarded writer must see the classifier's own
    #   dtype, so a non-uint8 map is rejected instead of silently truncated.
    published = np.array(result.class_map, copy=True)
    if row["bed_applied"]:
        flood = _flood_stage_for(manifest, ing, published.shape)
        removed = exposed_bed_mask(
            published, flood, float(row["level_m"]), float(row["margin_m"])
        )
        removed_ha = round(float(removed.sum()) * flood_map.PIXEL_HA, 2)
        if removed_ha != round(float(row["removed_ha"]), 2):
            msg = (
                f"class-5 area of {item_id} {removed_ha} ha differs from the run "
                f"removed_ha {round(float(row['removed_ha']), 2)}"
            )
            raise PublishRefused(msg)
        published[removed] = settings.CLASS_BED_REMOVED
    return PublishedMap(published, ing.transform, ing.crs)


def series_document(
    records: list[dict[str, object]], manifest: dict[str, object]
) -> dict:
    """``series.json``: every ok and skipped scene, sorted by date.

    Error scenes are excluded. No quality flags (C16): runs do not persist
    them and publish never recomputes them.
    """
    scenes = sorted(
        (_scene_entry(r) for r in records if r["status"] in ("ok", "skipped")),
        key=lambda entry: (str(entry["date"]), str(entry["item_id"])),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "smolf_version": __version__,
        "run_id": str(manifest["run_id"]),
        "series_sha256": manifest["series_sha256"],
        "scenes": scenes,
    }


def _swap_in(staging: Path, out_dir: Path) -> None:
    """Replace ``out_dir`` with ``staging`` by rename; restore it on failure."""
    if not out_dir.exists():
        staging.rename(out_dir)
        return
    previous = out_dir.with_name(f".{out_dir.name}.previous-{staging.name}")
    out_dir.rename(previous)
    try:
        staging.rename(out_dir)
    except BaseException:
        previous.rename(out_dir)
        raise
    shutil.rmtree(previous)


def _write_contract(
    out_dir: Path,
    documents: dict[str, str],
    maps: dict[str, PublishedMap] | None = None,
) -> None:
    """Write allowlisted files into a sibling staging dir, then swap it in.

    Maps go through the guarded raster writer only (uint8, classes 0-5).
    """
    maps = {} if maps is None else maps
    for name in [*documents, *maps]:
        if not is_allowed(name):
            msg = f"refusing to publish a file outside the allowlist: {name}"
            raise ValueError(msg)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.staging-", dir=out_dir.parent)
    )
    # WHY: mkdtemp creates a private (0700) dir; the contract is meant to be
    #   read by others, so it gets ordinary directory permissions.
    staging.chmod(0o755)
    try:
        for name, text in documents.items():
            (staging / name).write_text(text, encoding="utf-8")
        for name, layer in maps.items():
            target = staging / name
            target.parent.mkdir(mode=0o755, exist_ok=True)
            write_published_geotiff(target, layer.class_map, layer.transform, layer.crs)
        _swap_in(staging, out_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish_run(
    run_id: str | None = None,
    out_dir: Path | None = None,
    series_root: Path | None = None,
    levels_path: Path | None = None,
) -> Path:
    """Publish the contract for one run; return the output directory.

    Every document is validated before anything is written. Raises
    PublishRefused (exit 2) or ContractInvalid (exit 1); on either, the
    previous output directory is left untouched.
    """
    # WHY late binding: tests point series.OUTPUT_DIR / OUTPUT_DIR at tmp dirs.
    root = series.OUTPUT_DIR if series_root is None else Path(series_root)
    target = OUTPUT_DIR if out_dir is None else Path(out_dir)
    run_dir = select_run(root, run_id)
    records = series.load_records(run_dir / "scenes")
    manifest = _read_manifest(run_dir)
    latest = latest_document(records, manifest, levels_path)
    series_doc = series_document(records, manifest)
    validate("latest", latest)
    validate("series", series_doc)
    record = latest_record(records)
    layer = build_map(record, manifest)
    _write_contract(
        target,
        {
            "latest.json": canonical_json(latest),
            "series.json": canonical_json(series_doc),
        },
        {map_path(record): layer},
    )
    return target
