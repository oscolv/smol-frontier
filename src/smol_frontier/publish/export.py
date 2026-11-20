"""Export: downloadable files derived from a finished publish.

``export_files`` is pure: the parsed ``series.json`` and ``latest.json``
plus the ``series.json`` sha256 go in, ``{filename: bytes}`` comes out.
``export_publish`` reads a publish directory, verifies it against its own
integrity index and the packaged schemas, and swaps the files into the
output directory by rename. It reads only the published contract: no
network, no scene cache, no run directory. The exports are outside the
contract; nothing here changes the published files or their schemas.
"""

import csv
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path

from smol_frontier import paths
from smol_frontier.publish.documents import validate
from smol_frontier.publish.errors import ContractInvalid, PublishRefused
from smol_frontier.publish.writer import (
    INDEX_FILE,
    _debris,
    _resolve_out_dir,
    _restore_previous,
    _staging_prefix,
    _swap_in,
)

# WHY: allowlist — the only names `export` may ever write, and the only
#   entries an existing ``--out`` may hold (anything else is someone else's
#   data and is refused, never replaced). One set so later exports extend it.
EXPORT_FILES = frozenset({"series.csv"})

# WHY: every row says which run and which published file it came from, so a
#   CSV copied out of its folder still traces back to the contract.
#   series_sha256 is the run's internal series.csv hash (the series id), not
#   the hash of this file; series_json_sha256 links it to series.json.
PROVENANCE_COLUMNS = (
    "run_id",
    "series_sha256",
    "series_json_sha256",
    "schema_version",
    "smolf_version",
)

# WHY: the per-scene fields in the order of the per-scene table of
#   docs/contract.md (tests check both the order and that the set equals the
#   packaged per-scene schema), with the display_ha object flattened in
#   place into its three integer parts so the CSV stays one value per cell.
SCENE_COLUMNS = (
    "date",
    "item_id",
    "quality_light",
    "regime",
    "valid_frac",
    "cloud_frac",
    "dense_ha",
    "interface_ha",
    "hyacinth_raw_ha",
    "hyacinth_under_haze_ha",
    "pct_reservoir_raw",
    "bed_applied",
    "bed_reason",
    "level_m",
    "removed_ha",
    "dense_corrected_ha",
    "interface_corrected_ha",
    "hyacinth_corrected_ha",
    "pct_reservoir_corrected",
    "cover_band",
    "display_status",
    "display_ha_total",
    "display_ha_dense",
    "display_ha_interface",
)

# WHY: one header for every row, provenance first.
_SERIES_HEADER = (*PROVENANCE_COLUMNS, *SCENE_COLUMNS)

# WHY: the contract's display_ha parts, flattened as display_ha_<part>.
_DISPLAY_HA_PARTS = ("total", "dense", "interface")

# WHY: fixed decimals per numeric column, the contract's own rounding
#   (hectares, percentages and levels to 2, fractions to 4, display hectares
#   whole), so 15.7 prints 15.70 and every row lines up. Explicit per column,
#   never inferred from the value: a JSON 0 and 0.0 must print alike.
_DECIMALS = {
    "valid_frac": 4,
    "cloud_frac": 4,
    "dense_ha": 2,
    "interface_ha": 2,
    "hyacinth_raw_ha": 2,
    "hyacinth_under_haze_ha": 2,
    "pct_reservoir_raw": 2,
    "level_m": 2,
    "removed_ha": 2,
    "dense_corrected_ha": 2,
    "interface_corrected_ha": 2,
    "hyacinth_corrected_ha": 2,
    "pct_reservoir_corrected": 2,
    "display_ha_total": 0,
    "display_ha_dense": 0,
    "display_ha_interface": 0,
}

# WHY: the two published documents the export reads; maps and photos never.
_SOURCES = ("series.json", "latest.json")


def _cell(column: str, value: object) -> str:
    """One CSV cell: null empty, booleans lowercase, numbers fixed decimals."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    decimals = _DECIMALS.get(column)
    if decimals is None:
        return str(value)
    if decimals == 0:
        return f"{value:d}"
    return f"{value:.{decimals}f}"


def _scene_values(scene: dict[str, object]) -> dict[str, object]:
    """A scene entry keyed by CSV column (display_ha flattened)."""
    values = {key: value for key, value in scene.items() if key != "display_ha"}
    display = scene["display_ha"]
    for part in _DISPLAY_HA_PARTS:
        values[f"display_ha_{part}"] = None if display is None else display[part]
    return values


def _csv_bytes(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    """UTF-8 (no BOM), comma, LF, minimal RFC 4180 quoting."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _require_same_run(
    series_doc: dict[str, object], other: dict[str, object], what: str
) -> None:
    """Refuse ``other`` unless it names the run and series of ``series_doc``."""
    for field in ("run_id", "series_sha256"):
        if other[field] != series_doc[field]:
            msg = (
                f"{what} does not belong to the run of series.json "
                f"({field}: {other[field]} != {series_doc[field]})"
            )
            raise PublishRefused(msg)


def export_files(
    series_doc: dict[str, object],
    latest_doc: dict[str, object],
    series_json_sha256: str,
) -> dict[str, bytes]:
    """The export files, by name, of one publish's parsed documents.

    ``series.csv`` has one row per ``series.json`` scene, in its order, POOR
    scenes included, each with the provenance columns. Raises PublishRefused
    when ``latest_doc`` belongs to another run. Deterministic: no clock.
    """
    _require_same_run(series_doc, latest_doc, "latest.json")
    provenance = {
        "run_id": series_doc["run_id"],
        "series_sha256": series_doc["series_sha256"],
        "series_json_sha256": series_json_sha256,
        "schema_version": series_doc["schema_version"],
        "smolf_version": series_doc["smolf_version"],
    }
    rows = []
    for scene in series_doc["scenes"]:
        values = {**provenance, **_scene_values(scene)}
        rows.append([_cell(column, values[column]) for column in _SERIES_HEADER])
    return {"series.csv": _csv_bytes(_SERIES_HEADER, rows)}


def _read_json(path: Path) -> tuple[bytes, object]:
    """The bytes and parsed JSON of ``path``; PublishRefused if unreadable."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read {path}: {exc.strerror or exc}"
        raise PublishRefused(msg) from exc
    try:
        return data, json.loads(data)
    except ValueError as exc:
        msg = f"{path} is not valid JSON: {exc}"
        raise PublishRefused(msg) from exc


def _validated(name: str, document: object) -> dict[str, object]:
    """``document`` if it passes the packaged ``name`` schema, else refused."""
    try:
        validate(name.removesuffix(".json"), document)
    except ContractInvalid as exc:
        raise PublishRefused(str(exc)) from exc
    return document


def read_publish(
    from_dir: Path,
) -> tuple[dict[str, object], dict[str, object], str]:
    """``series.json``, ``latest.json`` and the series.json sha256, verified.

    ``publish.json`` must exist and pass its schema; each document must hash
    to its ``publish.json`` entry, pass its schema and name the index's run.
    Every failure is a PublishRefused.
    """
    _, index = _read_json(from_dir / INDEX_FILE)
    index = _validated(INDEX_FILE, index)
    expected = {entry["path"]: entry["sha256"] for entry in index["files"]}
    documents: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for name in _SOURCES:
        if name not in expected:
            msg = f"{from_dir / INDEX_FILE} does not list {name}"
            raise PublishRefused(msg)
        data, document = _read_json(from_dir / name)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[name]:
            msg = (
                f"{from_dir / name} does not match its {INDEX_FILE} entry "
                f"(sha256 {digest} != {expected[name]})"
            )
            raise PublishRefused(msg)
        documents[name] = _validated(name, document)
        hashes[name] = digest
    _require_same_run(documents["series.json"], index, INDEX_FILE)
    return documents["series.json"], documents["latest.json"], hashes["series.json"]


def _check_export_dir(out_dir: Path, given: Path) -> None:
    """Refuse an ``out_dir`` holding anything the export does not own.

    WHY: the swap renames ``out_dir`` away and deletes it, so it may only
    hold earlier exports: a missing or empty directory, or allowlisted
    regular files. A file, a symlink or a subdirectory is someone else's.
    """
    if not out_dir.exists() and not out_dir.is_symlink():
        return
    shown = given if given.absolute() == out_dir else f"{given} (-> {out_dir})"
    if not out_dir.is_dir():
        msg = f"--out {shown} exists and is not a directory"
        raise PublishRefused(msg)
    for entry in sorted(out_dir.iterdir()):
        owned = entry.name in EXPORT_FILES and entry.is_file()
        if not owned or entry.is_symlink():
            msg = f"--out {shown} holds a file the export does not own: {entry.name}"
            raise PublishRefused(msg)


def _drop_stale_staging(out_dir: Path) -> None:
    """Remove staging dirs a crashed export left, if they hold only its files."""
    for staging in _debris(out_dir, "staging"):
        if all(entry.name in EXPORT_FILES for entry in staging.iterdir()):
            shutil.rmtree(staging, ignore_errors=True)


def write_export(out_dir: Path, files: dict[str, bytes], given: Path) -> None:
    """Write allowlisted ``files`` into a sibling staging dir, then swap it in."""
    for name in files:
        if name not in EXPORT_FILES:
            msg = f"refusing to export a file outside the allowlist: {name}"
            raise ValueError(msg)
    _restore_previous(out_dir)  # a crash mid-swap may have left the old one
    _drop_stale_staging(out_dir)
    _check_export_dir(out_dir, given)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=_staging_prefix(out_dir.name), dir=out_dir.parent)
    )
    # WHY: mkdtemp creates a private (0700) dir; downloads are for others.
    staging.chmod(0o755)
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        _swap_in(staging, out_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def export_publish(from_dir: Path | None = None, out_dir: Path | None = None) -> Path:
    """Export a verified publish into ``out_dir``; return ``out_dir`` as given.

    Defaults resolve at call time: ``paths.publish_dir()`` and
    ``paths.downloads_dir()``. Every input check runs before anything is
    written; a refusal (PublishRefused, exit 2) leaves ``out_dir`` as it was.
    """
    source = paths.publish_dir() if from_dir is None else Path(from_dir)
    target = paths.downloads_dir() if out_dir is None else Path(out_dir)
    series_doc, latest_doc, series_json_sha256 = read_publish(source)
    files = export_files(series_doc, latest_doc, series_json_sha256)
    real = _resolve_out_dir(target)
    write_export(real, files, target)
    return target
