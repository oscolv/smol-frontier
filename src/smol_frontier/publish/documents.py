"""Contract documents: ``latest.json`` / ``series.json`` mapping, rounding,
regime, current level, canonical JSON and schema validation."""

import json
import math
from functools import cache
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from smol_frontier import __version__, aoi, paths, series
from smol_frontier.config import settings
from smol_frontier.hydro import levels
from smol_frontier.publish.aggregates import (
    display_ha,
    display_status,
    hectare_cutoff,
    monthly_medians,
)
from smol_frontier.publish.errors import ContractInvalid, PublishRefused, _require_dict
from smol_frontier.publish.map import map_path, png_path
from smol_frontier.publish.truecolour import attribution, image_path

# WHY: contract version written into every document. Each schema accepts any
#   version with this major (pattern ^3\.[0-9]+\.[0-9]+$ plus "not" a
#   pattern \n: a bare $ lets a trailing newline through in Python's re,
#   and a lookahead would not compile in RE2-based validators), so a
#   consumer validating with the schemas of the tag it pinned keeps
#   accepting later minor releases. Bump the major on any breaking change
#   (rename/remove a field, change a type, unit or meaning, add or remove an
#   enum value), the minor on additive ones (a new optional field).
#   2.0.0: removed_ha is null whenever bed_applied is false, and the
#   schema_version pattern rejects a trailing newline (issue #38).
#   2.1.0: latest.json gains the optional reservoir_outline (issue #42).
#   3.0.0: every scene carries display_status, and reservoir_outline is
#   required (issue #45, spec #44); every scene carries display_ha and
#   constants gain green_max_ha / red_min_ha (issue #46); latest.json
#   carries image, a Sentinel-2 true-colour photo (issue #48); series.json
#   carries monthly, the per-month median of corrected hectares (issue #47).
SCHEMA_VERSION = "3.0.0"

# WHY: contract decimals follow series.csv — hectares, percentages and water
#   levels to 2 decimals, fractions to 4.
_DECIMALS_FRACTION = 4
_DECIMALS_DEFAULT = 2
# WHY: 6 decimals of a degree are ~0.1 m at Endhó, two orders below the
#   10 m pixel the GSW polygon was traced on, and keep latest.json small.
_DECIMALS_OUTLINE = 6

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
    "removed_ha",
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
    no row: it carries date, item_id, quality_light, valid_frac,
    display_status (HIDDEN) and null for every other field.
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
        entry["display_status"] = display_status(entry, settings.FRAC_PUBLISHABLE)
        entry["display_ha"] = None
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
    entry["display_status"] = display_status(entry, settings.FRAC_PUBLISHABLE)
    entry["display_ha"] = display_ha(entry)
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
    area_ha = round(aoi.reservoir_area_ha(), _DECIMALS_DEFAULT)
    document["reservoir_area_ha"] = area_ha
    document["reservoir_outline"] = reservoir_outline()
    document["constants"] = {
        "frac_publishable": settings.FRAC_PUBLISHABLE,
        "green_max_pct": settings.GREEN_MAX_PCT,
        "red_min_pct": settings.RED_MIN_PCT,
        "green_max_ha": hectare_cutoff(area_ha, settings.GREEN_MAX_PCT),
        "red_min_ha": hectare_cutoff(area_ha, settings.RED_MIN_PCT),
    }
    document["current_level"] = current_level(levels_path)
    # bounds come from the warped PNG, so publish_run fills them in
    document["map"] = {"geotiff": map_path(record), "png": png_path(record)}
    document["image"] = {
        "path": image_path(record),
        "attribution": attribution(record["date"]),
    }
    return document


def reservoir_outline() -> dict[str, object]:
    """The reservoir polygon as an RFC 7946 MultiPolygon geometry.

    EPSG:4326, [longitude, latitude], 6 decimals; exterior rings
    counter-clockwise and holes clockwise (the RFC's right-hand rule), so a
    web map draws it as published without reprojecting or reorienting.
    """
    geometry = aoi.wgs84_geometry()
    if isinstance(geometry, Polygon):
        geometry = MultiPolygon([geometry])
    return {
        "type": "MultiPolygon",
        "coordinates": [
            _polygon_rings(orient(polygon, sign=1.0)) for polygon in geometry.geoms
        ],
    }


def _polygon_rings(polygon: Polygon) -> list[list[list[float]]]:
    """Exterior then interior rings of ``polygon``, coordinates rounded."""
    return [
        [
            [round(x, _DECIMALS_OUTLINE), round(y, _DECIMALS_OUTLINE)]
            for x, y in ring.coords
        ]
        for ring in (polygon.exterior, *polygon.interiors)
    ]


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
        "monthly": monthly_medians(scenes),
    }
