"""Contract documents: ``latest.json`` / ``series.json`` mapping, rounding,
regime, current level, canonical JSON and schema validation."""

import json
import math
from functools import cache
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

from smol_frontier import __version__, aoi, paths, series
from smol_frontier.config import settings
from smol_frontier.hydro import levels
from smol_frontier.publish.errors import ContractInvalid, PublishRefused, _require_dict
from smol_frontier.publish.map import map_path, png_path

# WHY: contract version written into every document. Each schema accepts any
#   version with this major (pattern ^1\.[0-9]+\.[0-9]+$), so a consumer
#   validating with the schemas of the tag it pinned keeps accepting later
#   minor releases. Bump the major on any breaking change (rename/remove a
#   field, change a type, unit or meaning, add or remove an enum value), the
#   minor on additive ones (a new optional field).
SCHEMA_VERSION = "1.0.0"

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
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline.

    Raises ContractInvalid for NaN or infinity: they are not valid JSON and a
    consumer's parser would reject (or silently mangle) the file.
    """
    try:
        text = json.dumps(
            document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
    except ValueError as exc:
        msg = f"document is not valid JSON: {exc}"
        raise ContractInvalid(msg) from exc
    return text + "\n"


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


def _newest_acquisition(records: list[dict[str, object]]) -> dict[str, object]:
    """Newest scene of the run with status ok or skipped (never error).

    A skipped scene is POOR by construction (valid_frac under the minimum);
    an ok scene carries the light of its row. Records are sorted by date.
    """
    ordered = sorted(records, key=series.record_sort_key)
    seen = [record for record in ordered if record["status"] in ("ok", "skipped")]
    record = seen[-1]
    if record["status"] == "ok":
        row = _require_dict(record["row"], f"row of {record['item_id_used']}")
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
    no record. ``levels_path`` defaults to ``paths.levels_read_path()``.
    """
    path = Path(paths.levels_read_path() if levels_path is None else levels_path)
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
    row = _require_dict(record["row"], f"row of {record['item_id_used']}")
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
    record = latest_record(records)
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "smolf_version": __version__,
        **_scene_entry(record),
    }
    counts = _require_dict(manifest["counts"], "manifest counts")
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
    # bounds come from the warped PNG, so publish_run fills them in
    document["map"] = {"geotiff": map_path(record), "png": png_path(record)}
    return document


def latest_record(records: list[dict[str, object]]) -> dict[str, object]:
    """The last ``ok`` record (the scene ``latest.json`` and the map describe)."""
    ordered = sorted(records, key=series.record_sort_key)
    return [record for record in ordered if record["status"] == "ok"][-1]


def series_document(
    records: list[dict[str, object]], manifest: dict[str, object]
) -> dict:
    """``series.json``: every ok and skipped scene, sorted by date.

    Error scenes are excluded. No quality flags (C16): runs do not persist
    them and publish never recomputes them.
    """
    scenes = [
        _scene_entry(r)
        for r in sorted(records, key=series.record_sort_key)
        if r["status"] in ("ok", "skipped")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "smolf_version": __version__,
        "run_id": str(manifest["run_id"]),
        "series_sha256": manifest["series_sha256"],
        "scenes": scenes,
    }
