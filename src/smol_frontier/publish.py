"""Publish a versioned data contract derived from one completed series run.

``publish_run`` picks a run (``--run`` or the newest complete one), refuses
runs that cannot back a reproducible contract (incomplete, dirty, no scene
with figures), maps the run's scene records to the English contract fields,
validates every document against its packaged JSON Schema, and swaps the
output directory in atomically. The run manifest is provenance, never
contract: nothing is copied out of it verbatim.

Contract files are canonical JSON (sorted keys, 2-space indent, UTF-8,
trailing newline) with no wall-clock timestamp, so the same run yields
byte-identical files.
"""

import json
import math
import shutil
import tempfile
from functools import cache
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

from smol_frontier import __version__, aoi, series
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.hydro import levels

__all__ = [
    "ALLOWED_FILES",
    "OUTPUT_DIR",
    "SCHEMA_VERSION",
    "ContractInvalid",
    "PublishRefused",
    "canonical_json",
    "current_level",
    "latest_document",
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
    return document


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


def _write_contract(out_dir: Path, documents: dict[str, str]) -> None:
    """Write allowlisted files into a sibling staging dir, then swap it in."""
    for name in documents:
        if name not in ALLOWED_FILES:
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
    latest = latest_document(records, _read_manifest(run_dir), levels_path)
    series_doc = series_document(records, _read_manifest(run_dir))
    validate("latest", latest)
    validate("series", series_doc)
    _write_contract(
        target,
        {
            "latest.json": canonical_json(latest),
            "series.json": canonical_json(series_doc),
        },
    )
    return target
